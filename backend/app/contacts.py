"""Contacts: phone normalization, vCard import, and name resolution for messaging."""
import difflib, re
from .db import db, now


class ContactError(Exception):
    pass


def normalize_phone(raw: str, default_cc: str = "") -> str:
    """Return international digits (no '+'), e.g. '+91 98765-43210' -> '919876543210'."""
    s = raw.strip()
    plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if digits.startswith("00"):
        digits, plus = digits[2:], True
    if not digits or len(digits) < 6:
        raise ContactError(f"'{raw}' doesn't look like a phone number")
    if plus or len(digits) > 11:
        return digits
    if default_cc:
        return default_cc + digits.lstrip("0")
    raise ContactError(f"'{raw}' has no country code. Write it as +<country code><number>, "
                       "or set your default country code in Settings.")


def parse_vcards(text: str) -> list[dict]:
    """Minimal vCard 2.1/3.0/4.0 parser: FN/N, TEL, EMAIL. Handles line folding and item1.TEL style."""
    text = re.sub(r"\r?\n[ \t]", "", text)  # unfold
    cards, cur = [], None
    for line in text.splitlines():
        if line.upper().startswith("BEGIN:VCARD"):
            cur = {"name": "", "n": "", "phones": [], "emails": []}
        elif line.upper().startswith("END:VCARD") and cur is not None:
            name = cur["name"] or " ".join(p for p in reversed(cur["n"].split(";")[:2]) if p).strip()
            if name and (cur["phones"] or cur["emails"]):
                cards.append({"name": name, "phones": cur["phones"], "emails": cur["emails"]})
            cur = None
        elif cur is not None and ":" in line:
            key, _, value = line.partition(":")
            prop = key.split(";")[0].split(".")[-1].upper()
            value = value.strip()
            if prop == "FN":
                cur["name"] = value
            elif prop == "N":
                cur["n"] = value
            elif prop == "TEL" and value:
                cur["phones"].append(value.removeprefix("tel:"))
            elif prop == "EMAIL" and value:
                cur["emails"].append(value)
    return cards


async def resolve(user_id, query: str) -> list[dict]:
    """Contacts matching a spoken/typed name: exact name or alias first, then prefix, contains, fuzzy."""
    q = query.strip().lower()
    if not q:
        return []
    contacts = [c async for c in db().contacts.find({"user_id": user_id}).limit(5000)]
    def names(c):
        return [c["name"].lower()] + [a.lower() for a in c.get("aliases", [])]
    for test in (lambda n: n == q, lambda n: n.startswith(q) or q.startswith(n + " "),
                 lambda n: q in n.split() or q in n):
        hits = [c for c in contacts if any(test(n) for n in names(c))]
        if hits:
            return hits
    scored = sorted(((max(difflib.SequenceMatcher(None, q, n).ratio() for n in names(c)), c) for c in contacts),
                    key=lambda x: -x[0])
    return [c for s, c in scored if s >= 0.75][:5]


async def upsert(user_id, name: str, phones: list[str], emails: list[str] | None = None,
                 aliases: list[str] | None = None) -> bool:
    """Insert or merge a contact by name. Returns True if new."""
    existing = await db().contacts.find_one({"user_id": user_id, "name": name})
    if existing:
        await db().contacts.update_one({"_id": existing["_id"]}, {
            "$addToSet": {"phones": {"$each": phones}, "emails": {"$each": emails or []}}, "$set": {"updated_at": now()}})
        return False
    await db().contacts.insert_one({"user_id": user_id, "name": name, "phones": phones, "emails": emails or [],
                                    "aliases": aliases or [], "created_at": now(), "updated_at": now()})
    return True
