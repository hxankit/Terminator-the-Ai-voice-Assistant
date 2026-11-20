"""WebSocket endpoint (two-way realtime channel) and voice transcription."""
import asyncio, json, logging
import httpx, jwt
from fastapi import APIRouter, HTTPException, UploadFile, File, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from ..config import get_settings
from ..db import db, oid
from ..deps import User
from ..llm.registry import resolve
from ..realtime import hub
from ..security import COOKIE, limiter, user_from_token
from .. import orchestrator as orch
from .chat import ChatBody, start_turn

router = APIRouter(tags=["realtime"])
import starlette.websockets as _sw
_DISCONNECTED = tuple(c for c in (getattr(_sw, "WebSocketDisconnected", None),) if c)   # newer Starlette
log = logging.getLogger("ws")

# Client -> server messages:
#   {"type":"chat", "conversation_id", ...ChatBody fields}   start / continue / regenerate a turn
#   {"type":"stop", "conversation_id"}
#   {"type":"decision", "run_id", "approve"?: bool, "answer"?: str}
#   {"type":"ping"}
# Server -> client: every orchestrator event tagged with conversation_id, plus
#   {"type":"notification", item}, {"type":"resolved", run_id, ...}, {"type":"error", message, conversation_id?}, {"type":"pong"}


async def _ws_user(ws: WebSocket) -> dict | None:
    # explicit credentials (Authorization header or ?token=) win over the browser's session cookie
    auth = ws.headers.get("authorization", "")
    token = (auth[7:] if auth.lower().startswith("bearer ") else None) or ws.query_params.get("token") or ws.cookies.get(COOKIE)
    return await user_from_token(token)


def _origin_ok(ws: WebSocket) -> bool:
    """Block cross-site WebSocket hijacking: browsers always send Origin; it must be ours."""
    origin = ws.headers.get("origin")
    if not origin:
        return True  # non-browser client (still needs a valid token)
    host = ws.headers.get("host", "")
    allowed = {o.strip() for o in get_settings().cors_origins.split(",")}
    return origin in allowed or origin.split("://", 1)[-1] == host


@router.websocket("/api/ws")
async def websocket(ws: WebSocket):
    if not _origin_ok(ws):
        await ws.close(code=4403)   # reject cross-site handshakes outright (HTTP 403)
        return
    user = await _ws_user(ws)
    await ws.accept()
    if not user:
        # accept-then-close so browsers receive code 4401 and stop reconnecting
        # (a pre-handshake rejection only surfaces as a generic 1006)
        await ws.close(code=4401, reason="Not authenticated")
        return
    uid = str(user["_id"])
    hub.add(uid, ws)
    pumps: set[asyncio.Task] = set()

    async def pump(cid: str, q: asyncio.Queue):
        # broadcast to all of this user's sockets: other tabs / a reloaded page / the voice service can follow along
        while (ev := await q.get()) is not None:
            await hub.push(uid, {**ev, "conversation_id": cid})
        await hub.push(uid, {"type": "end", "conversation_id": cid})

    try:
        unread = await db().notifications.count_documents({"user_id": user["_id"], "read": False})
        await hub.send(ws, {"type": "hello", "unread": unread})
        while True:
            msg = await ws.receive_json()
            kind = msg.get("type")
            cid = msg.get("conversation_id")
            try:
                if kind == "ping":
                    await hub.send(ws, {"type": "pong"})
                elif kind == "chat":
                    limiter.check(f"u:{uid}:ws-chat", 30)
                    body = ChatBody.model_validate({k: v for k, v in msg.items() if k not in ("type", "conversation_id")})
                    q, _, _ = await start_turn(user, cid, body)
                    t = asyncio.create_task(pump(cid, q))
                    pumps.add(t); t.add_done_callback(pumps.discard)
                elif kind == "stop":
                    if ev := orch.ACTIVE.get(cid):
                        ev.set()
                elif kind == "decision":
                    r = await orch.resolve_approval(user, oid(msg["run_id"]), msg.get("approve"), msg.get("answer"))
                    await hub.send(ws, {"type": "resolved", "run_id": msg["run_id"], **r})
                else:
                    await hub.send(ws, {"type": "error", "message": f"Unknown message type {kind!r}"})
            except HTTPException as e:
                await hub.send(ws, {"type": "error", "message": e.detail, "conversation_id": cid, "status": e.status_code})
            except ValidationError as e:
                await hub.send(ws, {"type": "error", "message": f"Invalid message: {e.errors()[0]['msg']}", "conversation_id": cid})
            except (ValueError, KeyError) as e:
                await hub.send(ws, {"type": "error", "message": str(e), "conversation_id": cid})
    except (WebSocketDisconnect, json.JSONDecodeError, *_DISCONNECTED):
        pass
    except Exception:
        log.exception("websocket error")
    finally:
        hub.remove(uid, ws)
        # turns keep running to completion and persist; a reconnecting client reloads the conversation


@router.post("/api/voice/transcribe")
async def transcribe(file: UploadFile = File(...), user: dict = User, model: str = "whisper-1"):
    """Server-side speech-to-text via the provider's OpenAI-compatible /audio/transcriptions endpoint.
    Used when the browser has no built-in speech recognition. Requires a provider that serves it
    (OpenAI, Groq, a local faster-whisper/speaches server, ...)."""
    data = await file.read(25 * 1024 * 1024 + 1)
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(413, "Audio exceeds 25 MB")
    spec = await resolve(user)
    headers = {"Authorization": f"Bearer {spec.api_key}"} if spec.api_key else {}
    try:
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(spec.base_url.rstrip("/") + "/audio/transcriptions", headers=headers,
                             files={"file": (file.filename or "audio.webm", data, file.content_type or "audio/webm")},
                             data={"model": model})
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Transcription provider unreachable: {e}")
    if r.status_code == 404:
        raise HTTPException(501, "Your provider has no /audio/transcriptions endpoint. Use Chrome/Edge (built-in "
                                 "speech recognition) or configure a Whisper-compatible provider.")
    if r.status_code >= 400:
        raise HTTPException(502, f"Transcription failed ({r.status_code}): {r.text[:200]}")
    return {"text": r.json().get("text", "")}
