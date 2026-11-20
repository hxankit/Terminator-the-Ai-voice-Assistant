"""Instagram DMs through instagrapi (Instagram's private API) - unofficial, at the user's explicit request.

The password is used once to sign in (plus any 2FA / security code Instagram asks for) and is never stored;
only Instagram's session settings are kept, encrypted. People are resolved ONLY among the user's DM threads
and the accounts they follow (never Instagram-wide search), so a message can't go to a stranger by mistake.
"""
import asyncio, difflib, json, logging, threading, time
from instagrapi import Client
from instagrapi.exceptions import BadPassword, ChallengeRequired, LoginRequired, TwoFactorRequired
from ..db import db, now
from ..security import decrypt, encrypt

log = logging.getLogger("instagram")
LOGINS: dict[str, dict] = {}        # user_id -> in-progress sign-in (password only lives here, in memory)
CLIENTS: dict[str, Client] = {}     # user_id -> logged-in client (session restored from the encrypted settings)


class InstagramError(Exception):
    pass


async def _doc(user):
    return await db().integrations.find_one({"user_id": user["_id"], "kind": "instagram"})


# ------------------------------------------------------------------ sign-in (background thread + code prompts)
async def start_login(user: dict, username: str, password: str) -> dict:
    uid = str(user["_id"])
    st = {"state": "signing_in", "username": username.strip().lstrip("@"), "password": password, "code": None,
          "event": threading.Event(), "error": None, "kind": None, "started": time.time()}
    LOGINS[uid] = st

    def wait_for_code(state: str, kind: str | None = None) -> str:
        st.update(state=state, kind=kind)
        st["event"].clear()
        if not st["event"].wait(300):
            raise InstagramError("No code was entered within 5 minutes. Try connecting again.")
        st["state"] = "signing_in"
        return (st.pop("code", None) or "").strip()

    def run():
        cl = Client()
        cl.delay_range = [1, 3]                             # act at human speed
        cl.challenge_code_handler = lambda username, choice: wait_for_code("needs_code", str(getattr(choice, "name", choice)))
        try:
            try:
                cl.login(st["username"], st["password"])
            except TwoFactorRequired:
                cl.login(st["username"], st["password"], verification_code=wait_for_code("needs_2fa"))
            return cl
        except BadPassword:
            raise InstagramError("Instagram says the password is wrong.")
        except ChallengeRequired:
            raise InstagramError("Instagram wants extra verification. Open the Instagram app, confirm it was you, then connect again.")

    async def finish():
        try:
            cl = await asyncio.to_thread(run)
            settings = cl.get_settings()
            await db().integrations.update_one(
                {"user_id": user["_id"], "kind": "instagram"},
                {"$set": {"username": st["username"], "settings_enc": encrypt(json.dumps(settings)), "updated_at": now()}},
                upsert=True)
            CLIENTS[uid] = cl
            LOGINS.pop(uid, None)
        except Exception as e:
            log.info("instagram login failed: %s", e)
            st.update(state="error", error=str(e) if isinstance(e, InstagramError) else f"Sign-in failed: {e}")
        finally:
            st.pop("password", None)                      # never kept after the attempt
    asyncio.create_task(finish())
    await asyncio.sleep(0.2)
    return await status(user)


def submit_code(user: dict, code: str) -> bool:
    st = LOGINS.get(str(user["_id"]))
    if not st or st["state"] not in ("needs_2fa", "needs_code"):
        return False
    st["code"] = code
    st["event"].set()
    return True


async def status(user: dict) -> dict:
    st = LOGINS.get(str(user["_id"]))
    if st:
        if st["state"] == "error":
            return {"state": "error", "error": st["error"]}
        return {"state": st["state"], "kind": st.get("kind"), "username": st["username"]}
    doc = await _doc(user)
    return {"state": "connected", "username": doc["username"]} if doc and doc.get("settings_enc") else {"state": "not_connected"}


async def connected(user: dict) -> bool:
    return (await status(user))["state"] == "connected"


async def disconnect(user: dict):
    uid = str(user["_id"])
    LOGINS.pop(uid, None)
    CLIENTS.pop(uid, None)
    await db().integrations.delete_one({"user_id": user["_id"], "kind": "instagram"})


async def _client(user: dict) -> Client:
    uid = str(user["_id"])
    if uid in CLIENTS:
        return CLIENTS[uid]
    doc = await _doc(user)
    if not doc or not doc.get("settings_enc"):
        raise InstagramError("Instagram isn't connected. Sign in under Settings → Instagram.")
    cl = Client()
    cl.delay_range = [1, 3]
    cl.set_settings(json.loads(decrypt(doc["settings_enc"])))
    CLIENTS[uid] = cl
    return cl


async def _call(user: dict, fn, *args, **kw):
    cl = await _client(user)
    try:
        return await asyncio.to_thread(getattr(cl, fn), *args, **kw)
    except LoginRequired:
        CLIENTS.pop(str(user["_id"]), None)
        raise InstagramError("Your Instagram session expired. Sign in again under Settings → Instagram.")
    except Exception as e:
        raise InstagramError(f"Instagram error: {type(e).__name__}: {str(e)[:200]}")


# ------------------------------------------------------------------ people & messages
def _score(q: str, user) -> float:
    names = [(user.username or "").lower(), (user.full_name or "").lower()]
    best = 0.0
    for n in names:
        if not n:
            continue
        best = max(best, 1.0 if n == q else 0.9 if n.startswith(q) else 0.85 if set(q.split()) <= set(n.split())
                   else 0.75 if q in n else difflib.SequenceMatcher(None, q, n).ratio())
    return best


async def find_person(user: dict, who: str) -> dict:
    q = who.strip().lstrip("@").lower()
    cands: dict[str, tuple[float, object]] = {}
    for t in await _call(user, "direct_threads", 30):
        for u in t.users:
            s = _score(q, u)
            if s >= 0.75 and s > cands.get(str(u.pk), (0,))[0]:
                cands[str(u.pk)] = (s, u)
    if not cands:
        me = await _call(user, "user_id_from_username", (await _doc(user))["username"])
        following = await _call(user, "user_following", me, amount=500)
        for pk, u in following.items():
            s = _score(q, u)
            if s >= 0.75:
                cands[str(pk)] = (s, u)
    if not cands:
        raise InstagramError(f"'{who}' isn't in your Instagram DMs or the accounts you follow.")
    ranked = sorted(cands.values(), key=lambda x: -x[0])
    if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
        raise InstagramError("Several people match: " + ", ".join(f"{u.full_name or u.username} (@{u.username})"
                                                                  for _, u in ranked[:5]) + ". Which one?")
    u = ranked[0][1]
    return {"pk": str(u.pk), "username": u.username, "name": u.full_name or u.username}


async def send(user: dict, who: str, text: str) -> dict:
    p = await find_person(user, who)
    await _call(user, "direct_send", text, user_ids=[int(p["pk"])])
    return {"sent": True, "to": p["name"], "username": p["username"]}


async def threads(user: dict, limit: int = 10) -> list[dict]:
    out = []
    for t in await _call(user, "direct_threads", limit):
        last = t.messages[0] if t.messages else None
        out.append({"with": t.thread_title or ", ".join(u.full_name or u.username for u in t.users),
                    "last_message": (last.text or f"[{last.item_type}]") if last else "",
                    "when": str(last.timestamp) if last else ""})
    return out


async def read(user: dict, who: str, limit: int = 15) -> dict:
    p = await find_person(user, who)
    for t in await _call(user, "direct_threads", 30):
        if any(str(u.pk) == p["pk"] for u in t.users):
            msgs = await _call(user, "direct_messages", t.id, limit)
            return {"with": p["name"], "messages": [
                {"from": p["name"] if str(m.user_id) == p["pk"] else "You", "text": m.text or f"[{m.item_type}]",
                 "time": str(m.timestamp)} for m in reversed(msgs)]}
    return {"with": p["name"], "messages": []}
