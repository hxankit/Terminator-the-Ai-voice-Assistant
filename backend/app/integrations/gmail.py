"""Gmail through IMAP/SMTP with a Google *app password* (Google Account → Security → 2-Step Verification →
App passwords). The app password is stored encrypted (Fernet) and can be revoked anytime in the Google Account."""
import asyncio, email, imaplib, re, smtplib
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr
from email.header import decode_header, make_header
from ..db import db, now
from ..security import decrypt, encrypt

IMAP_HOST, SMTP_HOST = "imap.gmail.com", "smtp.gmail.com"


class GmailError(Exception):
    pass


# factories so tests can substitute fakes
def _imap():
    return imaplib.IMAP4_SSL(IMAP_HOST, 993, timeout=20)


def _smtp():
    return smtplib.SMTP_SSL(SMTP_HOST, 465, timeout=20)


async def _doc(user):
    return await db().integrations.find_one({"user_id": user["_id"], "kind": "gmail"})


def _explain(e: Exception) -> str:
    s = str(e)
    if "Application-specific password required" in s or "InvalidSecondFactor" in s:
        return "Google wants an app password, not your normal password (Google Account → Security → App passwords)."
    if "Invalid credentials" in s or "AUTHENTICATIONFAILED" in s or "Username and Password not accepted" in s:
        return "Google rejected the email or app password. Check both (the app password is 16 letters)."
    return f"Couldn't talk to Gmail: {s[:200]}"


def _verify(address: str, password: str):
    try:
        m = _imap(); m.login(address, password); m.logout()
        s = _smtp(); s.login(address, password); s.quit()
    except (imaplib.IMAP4.error, smtplib.SMTPException, OSError) as e:
        raise GmailError(_explain(e))


async def connect(user: dict, address: str, app_password: str) -> dict:
    password = re.sub(r"\s+", "", app_password)              # Google shows it as 4 groups of 4
    await asyncio.to_thread(_verify, address.strip(), password)
    await db().integrations.update_one({"user_id": user["_id"], "kind": "gmail"},
                                       {"$set": {"email": address.strip(), "password_enc": encrypt(password), "updated_at": now()}},
                                       upsert=True)
    return {"state": "connected", "email": address.strip()}


async def status(user: dict) -> dict:
    doc = await _doc(user)
    return {"state": "connected", "email": doc["email"]} if doc and doc.get("password_enc") else {"state": "not_connected"}


async def connected(user: dict) -> bool:
    return (await status(user))["state"] == "connected"


async def disconnect(user: dict):
    await db().integrations.delete_one({"user_id": user["_id"], "kind": "gmail"})


async def _creds(user) -> tuple[str, str]:
    doc = await _doc(user)
    if not doc or not doc.get("password_enc"):
        raise GmailError("Gmail isn't connected. Add it under Settings → Gmail.")
    return doc["email"], decrypt(doc["password_enc"])


def _dec(v) -> str:
    try:
        return str(make_header(decode_header(v or "")))
    except Exception:
        return v or ""


def _search(address: str, password: str, raw: str, limit: int, folder: str = '"[Gmail]/All Mail"') -> list[dict]:
    m = _imap()
    try:
        m.login(address, password)
        m.select(folder, readonly=True)
        typ, data = m.uid("SEARCH", None, "X-GM-RAW", '"' + raw.replace('"', "") + '"')
        uids = (data[0] or b"").split()[-limit:]
        out = []
        for uid in reversed(uids):
            typ, msg = m.uid("FETCH", uid, "(FLAGS BODY.PEEK[]<0.20000>)")
            raw_bytes = next((part[1] for part in msg if isinstance(part, tuple)), b"")
            flags = b" ".join(part if isinstance(part, bytes) else part[0] for part in msg)
            em = email.message_from_bytes(raw_bytes)
            text = ""
            for part in (em.walk() if em.is_multipart() else [em]):
                if part.get_content_type() == "text/plain":
                    try:
                        text = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                    except Exception:
                        text = str(part.get_payload())[:500]
                    break
            out.append({"from": _dec(em.get("From")), "to": _dec(em.get("To")), "subject": _dec(em.get("Subject")),
                        "date": em.get("Date"), "unread": b"\\Seen" not in flags,
                        "preview": re.sub(r"\s+", " ", text)[:300]})
        return out
    except (imaplib.IMAP4.error, OSError) as e:
        raise GmailError(_explain(e))
    finally:
        try:
            m.logout()
        except Exception:
            pass


async def list_mail(user: dict, query: str = "", unread_only: bool = False, limit: int = 10) -> list[dict]:
    address, password = await _creds(user)
    raw = " ".join(x for x in [query.strip(), "is:unread" if unread_only else "", "in:inbox" if not query else ""] if x)
    return await asyncio.to_thread(_search, address, password, raw or "in:inbox", limit)


async def find_address(user: dict, name: str) -> tuple[str, str]:
    """(display name, address) for someone you've emailed with, by name."""
    if "@" in name:
        return name.strip(), name.strip()
    address, password = await _creds(user)
    msgs = await asyncio.to_thread(_search, address, password, f"from:({name}) OR to:({name})", 25)
    wanted = name.lower().split()
    found: dict[str, str] = {}
    for mm in msgs:
        for disp, addr in getaddresses([mm["from"], mm["to"]]):
            if addr and addr.lower() != address.lower() and all(w in (disp + " " + addr).lower() for w in wanted):
                found.setdefault(addr.lower(), disp or addr)
    if not found:
        raise GmailError(f"I couldn't find an email address for '{name}' in your Gmail. Tell me the address.")
    if len(found) > 1:
        raise GmailError("Several people match: " + ", ".join(f"{d} <{a}>" for a, d in list(found.items())[:5]) + ". Which one?")
    addr, disp = next(iter(found.items()))
    return disp, addr


async def send(user: dict, to_name: str, to_addr: str, subject: str, body: str) -> dict:
    address, password = await _creds(user)
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = address, to_addr, subject
    msg.set_content(body)

    def _send():
        try:
            s = _smtp(); s.login(address, password); s.send_message(msg); s.quit()
        except (smtplib.SMTPException, OSError) as e:
            raise GmailError(_explain(e))
    await asyncio.to_thread(_send)
    return {"sent": True, "to": to_name, "email": to_addr, "subject": subject, "account": "gmail"}
