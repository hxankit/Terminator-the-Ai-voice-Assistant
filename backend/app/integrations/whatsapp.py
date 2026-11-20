"""WhatsApp through the whatsapp-mcp Go bridge (integrations/whatsapp-mcp, whatsmeow linked device).

The bridge keeps WhatsApp connected, stores chats/messages in store/messages.db, and exposes
POST 127.0.0.1:8080/api/send. Terminator talks to it directly (no MCP client needed) and reads the
bridge's SQLite stores read-only to find people by the names saved on the user's phone.
"""
import difflib, re, sqlite3, time
from pathlib import Path
import httpx
from ..config import get_settings


def store_dir() -> Path:
    return Path(get_settings().whatsapp_store_dir)


def linked() -> bool:
    """Has a phone ever been linked (whatsmeow stores the device after a successful QR scan)?"""
    rows = _read("whatsapp.db", "SELECT count(*) FROM whatsmeow_device")
    return bool(rows and rows[0][0])


async def status() -> dict:
    """connected | waiting_for_scan (+qr) | starting (not linked, new code coming) | offline (linked, bridge down) | not_set_up"""
    try:
        async with httpx.AsyncClient(timeout=2) as c:
            r = await c.get(get_settings().whatsapp_bridge_url.rstrip("/") + "/api/send")
        if r.status_code in (400, 405):                      # the send endpoint only answers to POST: it's up
            return {"state": "connected"}
    except httpx.HTTPError:
        pass
    qr = store_dir() / "qr.txt"
    if qr.exists() and time.time() - qr.stat().st_mtime < 75:
        return {"state": "waiting_for_scan", "qr": qr.read_text().strip()}
    if linked():
        return {"state": "offline"}
    if (store_dir() / "whatsapp.db").exists():
        return {"state": "starting"}
    return {"state": "not_set_up"}


def _read(db: str, sql: str, params: tuple = ()) -> list[tuple]:
    path = store_dir() / db
    if not path.exists():
        return []
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    try:
        return con.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()


def find_contacts(name: str) -> list[dict]:
    """People and groups whose WhatsApp/phone-book name matches, best first."""
    q = re.sub(r"\s+", " ", name.lower()).strip()
    if not q:
        return []
    people: dict[str, set[str]] = {}
    for jid, *names in _read("whatsapp.db", "SELECT their_jid, full_name, first_name, push_name, business_name FROM whatsmeow_contacts"):
        people.setdefault(jid, set()).update(n for n in names if n)
    for jid, cname in _read("messages.db", "SELECT jid, name FROM chats WHERE name IS NOT NULL AND name != ''"):
        people.setdefault(jid, set()).add(cname)
    scored = []
    for jid, names in people.items():
        best = 0.0
        for n in names:
            nl = n.lower()
            s = 1.0 if nl == q else 0.9 if nl.startswith(q) else 0.85 if set(q.split()) <= set(nl.split()) else \
                0.75 if q in nl else difflib.SequenceMatcher(None, q, nl).ratio()
            best = max(best, s)
        if best >= 0.75:
            label = max(names, key=len)
            scored.append((best, label, jid))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [{"name": n, "jid": j, "group": j.endswith("@g.us"), "score": round(s, 2)} for s, n, j in scored[:8]]


async def send(recipient: str, message: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(get_settings().whatsapp_bridge_url.rstrip("/") + "/api/send",
                         json={"recipient": recipient, "message": message})
    try:
        data = r.json()
    except ValueError:
        data = {"success": False, "message": r.text[:200]}
    return {"success": bool(data.get("success")) and r.status_code < 400, "message": data.get("message", "")}


def recent_chats(limit: int = 10) -> list[dict]:
    rows = _read("messages.db", "SELECT jid, name, last_message_time FROM chats ORDER BY last_message_time DESC LIMIT ?", (limit,))
    return [{"name": n or j.split("@")[0], "jid": j, "last_message": str(t)} for j, n, t in rows]


def recent_messages(jid: str, limit: int = 15) -> list[dict]:
    rows = _read("messages.db", "SELECT sender, content, timestamp, is_from_me, media_type FROM messages WHERE chat_jid = ? "
                                "ORDER BY timestamp DESC LIMIT ?", (jid, limit))
    names = {j: n for j, n in _read("messages.db", "SELECT jid, name FROM chats")}
    out = []
    for sender, content, ts, me, media in reversed(rows):
        who = "You" if me else names.get(f"{sender}@s.whatsapp.net") or names.get(sender) or sender
        out.append({"from": who, "text": content or (f"[{media}]" if media else ""), "time": str(ts)})
    return out
