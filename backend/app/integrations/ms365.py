"""Microsoft 365 through Microsoft Graph (work/school accounts): Teams chats, Outlook mail, calendar.

Sign-in uses the OAuth device-code flow with the user's OWN Entra app registration (public client, no secret):
Terminator shows a code, the user approves at microsoft.com/devicelogin, and MSAL's token cache is stored
encrypted (Fernet, like provider API keys). Every call is made as the signed-in user, with delegated permissions.
"""
import asyncio, logging, time
from datetime import datetime, timedelta, timezone
import httpx
import msal
from ..config import get_settings
from ..db import db, now
from ..security import decrypt, encrypt

log = logging.getLogger("ms365")
SCOPES = ["User.Read", "People.Read", "User.ReadBasic.All", "Chat.ReadWrite", "ChatMessage.Send",
          "Mail.Read", "Mail.Send", "Calendars.ReadWrite"]
FLOWS: dict[str, dict] = {}            # user_id -> {"code", "uri", "expires_at", "error"} while signing in


class NotConnected(Exception):
    pass


class GraphError(Exception):
    pass


async def _doc(user) -> dict | None:
    return await db().integrations.find_one({"user_id": user["_id"], "kind": "ms365"})


def _app(doc: dict, cache: msal.SerializableTokenCache) -> msal.PublicClientApplication:
    authority = f"{get_settings().ms_authority_base.rstrip('/')}/{doc.get('tenant_id') or 'organizations'}"
    return msal.PublicClientApplication(doc["client_id"], authority=authority, token_cache=cache)


def _load_cache(doc: dict) -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    if doc.get("cache_enc"):
        cache.deserialize(decrypt(doc["cache_enc"]))
    return cache


async def _save_cache(user, cache: msal.SerializableTokenCache, **extra):
    upd = {"updated_at": now(), **extra}
    if cache.has_state_changed:
        upd["cache_enc"] = encrypt(cache.serialize())
    await db().integrations.update_one({"user_id": user["_id"], "kind": "ms365"}, {"$set": upd})


# ------------------------------------------------------------------ sign-in
async def start_sign_in(user: dict, client_id: str, tenant_id: str) -> dict:
    """Begin the device-code flow; finishes in the background once the user approves in their browser."""
    await db().integrations.update_one({"user_id": user["_id"], "kind": "ms365"},
                                       {"$set": {"client_id": client_id.strip(), "tenant_id": tenant_id.strip() or "organizations",
                                                 "cache_enc": "", "account": None, "updated_at": now()}}, upsert=True)
    doc = await _doc(user)
    cache = msal.SerializableTokenCache()
    app = await asyncio.to_thread(_app, doc, cache)
    flow = await asyncio.to_thread(app.initiate_device_flow, scopes=SCOPES)
    if "user_code" not in flow:
        raise GraphError(flow.get("error_description") or "Microsoft didn't start the sign-in. Check the client and tenant IDs.")
    uid = str(user["_id"])
    FLOWS[uid] = {"code": flow["user_code"], "uri": flow["verification_uri"],
                  "expires_at": time.time() + int(flow.get("expires_in", 900)), "error": None}

    async def finish():
        result = await asyncio.to_thread(app.acquire_token_by_device_flow, flow)   # blocks until approved/expired
        if "access_token" not in result:
            FLOWS[uid] = {**FLOWS.get(uid, {}), "error": result.get("error_description") or result.get("error", "Sign-in failed")}
            return
        claims = result.get("id_token_claims") or {}
        account = {"name": claims.get("name"), "email": claims.get("preferred_username"), "oid": claims.get("oid")}
        await _save_cache(user, cache, account=account)
        FLOWS.pop(uid, None)
        log.info("Microsoft 365 connected for %s", account.get("email"))
    asyncio.create_task(finish())
    return {"user_code": flow["user_code"], "verification_uri": flow["verification_uri"], "message": flow.get("message")}


async def status(user: dict) -> dict:
    uid = str(user["_id"])
    if (f := FLOWS.get(uid)) and not f.get("error") and time.time() < f["expires_at"]:
        return {"state": "signing_in", "user_code": f["code"], "verification_uri": f["uri"]}
    doc = await _doc(user)
    if f and f.get("error"):
        return {"state": "error", "error": f["error"], "client_id": (doc or {}).get("client_id")}
    if not doc:
        return {"state": "not_configured"}
    if doc.get("account"):
        return {"state": "connected", "account": doc["account"], "client_id": doc["client_id"], "tenant_id": doc.get("tenant_id")}
    return {"state": "signed_out", "client_id": doc.get("client_id"), "tenant_id": doc.get("tenant_id")}


async def disconnect(user: dict):
    FLOWS.pop(str(user["_id"]), None)
    await db().integrations.update_one({"user_id": user["_id"], "kind": "ms365"},
                                       {"$set": {"cache_enc": "", "account": None, "updated_at": now()}})


async def connected(user: dict) -> bool:
    doc = await _doc(user)
    return bool(doc and doc.get("account"))


async def token(user: dict) -> str:
    doc = await _doc(user)
    if not doc or not doc.get("account"):
        raise NotConnected("Microsoft 365 isn't connected. Sign in under Settings → Microsoft 365.")
    cache = _load_cache(doc)
    app = _app(doc, cache)
    accounts = await asyncio.to_thread(app.get_accounts)
    result = await asyncio.to_thread(app.acquire_token_silent, SCOPES, accounts[0]) if accounts else None
    if not result or "access_token" not in result:
        await db().integrations.update_one({"_id": doc["_id"]}, {"$set": {"account": None}})
        raise NotConnected("Your Microsoft 365 sign-in expired. Sign in again under Settings → Microsoft 365.")
    await _save_cache(user, cache)
    return result["access_token"]


# ------------------------------------------------------------------ Graph calls
async def graph(user: dict, method: str, path: str, **kw) -> dict:
    tok = await token(user)
    headers = {"Authorization": f"Bearer {tok}", **kw.pop("headers", {})}
    async with httpx.AsyncClient(timeout=30) as c:
        try:
            r = await c.request(method, get_settings().graph_base_url.rstrip("/") + path, headers=headers, **kw)
        except httpx.HTTPError as e:
            raise GraphError(f"Couldn't reach Microsoft Graph ({type(e).__name__}).")
    if r.status_code == 204 or not r.content:
        return {}
    data = r.json() if "json" in r.headers.get("content-type", "") else {"raw": r.text}
    if r.status_code >= 400:
        msg = (data.get("error") or {}).get("message", r.text[:200]) if isinstance(data, dict) else r.text[:200]
        if r.status_code in (401, 403):
            msg += " (the app registration may be missing a permission, or your IT admin must grant consent)"
        raise GraphError(f"Microsoft Graph error {r.status_code}: {msg}")
    return data


async def find_people(user: dict, name: str, top: int = 5) -> list[dict]:
    """Colleagues by name: your most relevant people first, then the organization directory."""
    q = name.strip().replace('"', "")
    out: dict[str, dict] = {}
    try:
        data = await graph(user, "GET", "/me/people", params={"$search": f'"{q}"', "$top": top})
        for p in data.get("value", []):
            email = (p.get("scoredEmailAddresses") or [{}])[0].get("address") or p.get("userPrincipalName")
            if email and p.get("personType", {}).get("class", "Person") == "Person":
                out[email.lower()] = {"name": p.get("displayName") or email, "email": email, "id": p.get("id")}
    except GraphError as e:
        log.info("people search failed: %s", e)
    if not out:
        data = await graph(user, "GET", "/users", params={"$search": f'"displayName:{q}"', "$top": top,
                                                          "$select": "id,displayName,mail,userPrincipalName"},
                           headers={"ConsistencyLevel": "eventual"})
        for u in data.get("value", []):
            email = u.get("mail") or u.get("userPrincipalName")
            if email:
                out[email.lower()] = {"name": u.get("displayName") or email, "email": email, "id": u.get("id")}
    return list(out.values())


async def resolve_person(user: dict, who: str) -> dict:
    if "@" in who:
        return {"name": who, "email": who.strip(), "id": None}
    hits = await find_people(user, who)
    if not hits:
        raise GraphError(f"I couldn't find anyone called '{who}' in your organization.")
    exact = [h for h in hits if h["name"].lower() == who.strip().lower()]
    if len(exact) == 1:
        return exact[0]
    if len(hits) > 1 and not exact:
        first = who.strip().lower().split()[0]
        close = [h for h in hits if h["name"].lower().startswith(first)]
        if len(close) != 1:
            raise GraphError("Several people match: " + ", ".join(f"{h['name']} ({h['email']})" for h in hits[:5]) + ". Which one?")
        return close[0]
    return hits[0]


async def teams_send(user: dict, who: str, text: str) -> dict:
    person = await resolve_person(user, who)
    me = await graph(user, "GET", "/me", params={"$select": "id"})
    member = lambda ref: {"@odata.type": "#microsoft.graph.aadUserConversationMember", "roles": ["owner"],
                          "user@odata.bind": f"https://graph.microsoft.com/v1.0/users('{ref}')"}
    chat = await graph(user, "POST", "/chats", json={"chatType": "oneOnOne",                 # returns the existing 1:1
                                                     "members": [member(me["id"]), member(person["id"] or person["email"])]})
    await graph(user, "POST", f"/chats/{chat['id']}/messages", json={"body": {"contentType": "text", "content": text}})
    return {"sent": True, "to": person["name"], "email": person["email"]}


async def teams_recent(user: dict, top: int = 10) -> list[dict]:
    data = await graph(user, "GET", "/me/chats", params={"$expand": "lastMessagePreview,members", "$top": top,
                                                         "$orderby": "lastMessagePreview/createdDateTime desc"})
    out = []
    for c in data.get("value", []):
        prev = c.get("lastMessagePreview") or {}
        names = [m.get("displayName") for m in c.get("members", []) if m.get("displayName")]
        out.append({"chat": c.get("topic") or ", ".join(names[:4]) or c.get("chatType"), "id": c["id"],
                    "last_from": ((prev.get("from") or {}).get("user") or {}).get("displayName"),
                    "last_message": _strip_html((prev.get("body") or {}).get("content", ""))[:300],
                    "when": prev.get("createdDateTime")})
    return out


async def email_send(user: dict, to: str, subject: str, body: str) -> dict:
    person = await resolve_person(user, to)
    await graph(user, "POST", "/me/sendMail", json={"message": {
        "subject": subject, "body": {"contentType": "Text", "content": body},
        "toRecipients": [{"emailAddress": {"address": person["email"]}}]}, "saveToSentItems": True})
    return {"sent": True, "to": person["name"], "email": person["email"], "subject": subject}


async def email_list(user: dict, query: str = "", unread_only: bool = False, top: int = 10) -> list[dict]:
    params = {"$top": top, "$select": "subject,from,receivedDateTime,bodyPreview,isRead,webLink"}
    if query:
        params["$search"] = f'"{query.replace(chr(34), "")}"'
    else:
        params["$orderby"] = "receivedDateTime desc"
        if unread_only:
            params["$filter"] = "isRead eq false"
    data = await graph(user, "GET", "/me/messages", params=params)
    return [{"from": ((m.get("from") or {}).get("emailAddress") or {}).get("name"), "subject": m.get("subject"),
             "received": m.get("receivedDateTime"), "unread": not m.get("isRead", True),
             "preview": (m.get("bodyPreview") or "")[:300], "link": m.get("webLink")} for m in data.get("value", [])]


async def calendar(user: dict, start: datetime, end: datetime) -> list[dict]:
    data = await graph(user, "GET", "/me/calendarView", params={
        "startDateTime": start.astimezone(timezone.utc).isoformat(), "endDateTime": end.astimezone(timezone.utc).isoformat(),
        "$orderby": "start/dateTime", "$top": 50, "$select": "subject,start,end,location,organizer,isOnlineMeeting,onlineMeeting"},
        headers={"Prefer": f'outlook.timezone="UTC"'})
    out = []
    for e in data.get("value", []):
        s = datetime.fromisoformat(e["start"]["dateTime"][:19]).replace(tzinfo=timezone.utc).astimezone()
        f = datetime.fromisoformat(e["end"]["dateTime"][:19]).replace(tzinfo=timezone.utc).astimezone()
        out.append({"subject": e.get("subject"), "start": s.strftime("%a %-d %b %-I:%M %p"), "end": f.strftime("%-I:%M %p"),
                    "location": (e.get("location") or {}).get("displayName") or None,
                    "organizer": ((e.get("organizer") or {}).get("emailAddress") or {}).get("name"),
                    "online": bool(e.get("isOnlineMeeting"))})
    return out


def day_range(day: str) -> tuple[datetime, datetime]:
    base = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    if day == "tomorrow":
        base += timedelta(days=1)
    if day == "week":
        return base, base + timedelta(days=7)
    return base, base + timedelta(days=1)


def _strip_html(s: str) -> str:
    import re
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()
