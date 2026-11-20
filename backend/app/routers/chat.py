"""Conversations + streaming chat (Server-Sent Events)."""
import asyncio, json, re
from bson import ObjectId
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from ..config import get_settings
from ..db import db, now, oid, ser
from ..deps import User, ChatUser
from ..llm.registry import resolve
from .. import orchestrator as orch

router = APIRouter(prefix="/api", tags=["chat"])


async def _conv(user, cid: str) -> dict:
    c = await db().conversations.find_one({"_id": oid(cid), "user_id": user["_id"]})
    if not c:
        raise HTTPException(404, "Conversation not found")
    return c


class NewConv(BaseModel):
    title: str = Field("New chat", max_length=120)
    project_id: str | None = None


@router.post("/conversations")
async def create_conversation(body: NewConv, user: dict = User):
    doc = {"user_id": user["_id"], "title": body.title, "created_at": now(), "updated_at": now(),
           "project_id": oid(body.project_id) if body.project_id else None, "pinned": False}
    doc["_id"] = (await db().conversations.insert_one(doc)).inserted_id
    return ser(doc)


@router.get("/conversations")
async def list_conversations(user: dict = User, q: str = "", project_id: str | None = None,
                             limit: int = 30, before: str | None = None):
    limit = max(1, min(limit, 100))
    flt: dict = {"user_id": user["_id"]}
    if project_id:
        flt["project_id"] = oid(project_id)
    if before:
        flt["updated_at"] = {"$lt": (await _conv(user, before))["updated_at"]}
    if q.strip():
        rx = {"$regex": re.escape(q.strip()), "$options": "i"}
        hit_ids = await db().messages.distinct("conversation_id", {"user_id": user["_id"], "content": rx})
        flt["$or"] = [{"title": rx}, {"_id": {"$in": hit_ids[:500]}}]
    items = [ser(c) async for c in db().conversations.find(flt, {"title": 1, "updated_at": 1, "project_id": 1,
                                                                   "pinned": 1, "last_agent": 1})
             .sort([("pinned", -1), ("updated_at", -1)]).limit(limit)]
    return {"items": items, "has_more": len(items) == limit}


@router.get("/conversations/{cid}")
async def get_conversation(cid: str, user: dict = User):
    c = await _conv(user, cid)
    msgs = [ser(m) async for m in db().messages.find({"conversation_id": c["_id"]}, {"llm_content": 0})
            .sort([("created_at", 1), ("_id", 1)]).limit(1000)]
    pending = [ser(r) async for r in db().tool_runs.find({"conversation_id": cid, "status": {"$in": ["awaiting_approval", "awaiting_input"]}})]
    return {**ser(c), "messages": msgs, "pending_approvals": pending, "active": cid in orch.ACTIVE}


class PatchConv(BaseModel):
    title: str | None = Field(None, max_length=120)
    pinned: bool | None = None
    project_id: str | None = None


@router.patch("/conversations/{cid}")
async def patch_conversation(cid: str, body: PatchConv, user: dict = User):
    c = await _conv(user, cid)
    upd = body.model_dump(exclude_unset=True)
    if "project_id" in upd:
        upd["project_id"] = oid(upd["project_id"]) if upd["project_id"] else None
    if "title" in upd:
        upd["title_set"] = True
    await db().conversations.update_one({"_id": c["_id"]}, {"$set": upd})
    return {"ok": True}


@router.delete("/conversations/{cid}")
async def delete_conversation(cid: str, user: dict = User):
    c = await _conv(user, cid)
    await db().messages.delete_many({"conversation_id": c["_id"]})
    await db().conversations.delete_one({"_id": c["_id"]})
    return {"ok": True}


class ChatBody(BaseModel):
    message: str | None = Field(None, max_length=100_000)
    attachments: list[str] = Field(default_factory=list, max_length=10)
    provider_id: str | None = None
    model: str | None = None
    agent: str | None = None
    use_kb: bool = False
    mode: str = Field("send", pattern="^(send|continue|regenerate)$")


def _sse(ev: dict) -> str:
    return f"event: {ev['type']}\ndata: {json.dumps(ev, default=str)}\n\n"


async def start_turn(user: dict, cid: str, body: ChatBody) -> tuple[asyncio.Queue, asyncio.Task, asyncio.Event]:
    """Validate and launch a turn. Shared by the SSE endpoint and the WebSocket."""
    conv = await _conv(user, cid)
    d = db()
    if cid in orch.ACTIVE:
        raise HTTPException(409, "A response is already being generated in this conversation")
    has_pending = await d.messages.count_documents({"conversation_id": conv["_id"], "status": "pending"})
    if has_pending:
        raise HTTPException(409, "Answer or resolve the pending request first")
    if body.mode == "send" and not (body.message or "").strip():
        raise HTTPException(422, "Message is empty")
    spec = await resolve(user, body.provider_id, body.model)
    if body.mode == "regenerate":
        last_user = await d.messages.find_one({"conversation_id": conv["_id"], "role": "user"},
                                              sort=[("created_at", -1), ("_id", -1)])
        if not last_user:
            raise HTTPException(400, "Nothing to regenerate")
        await d.messages.delete_many({"conversation_id": conv["_id"], "_id": {"$gt": last_user["_id"]}})
    attachments = [oid(a) for a in body.attachments]
    stop = asyncio.Event()
    orch.ACTIVE[cid] = stop
    q: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(orch.run_turn(
        user, conv, spec, q, text=body.message if body.mode == "send" else None, attachments=attachments,
        agent_key=body.agent, use_kb=body.use_kb, stop=stop))
    watchdog = asyncio.get_running_loop().call_later(get_settings().turn_timeout_s, stop.set)
    def _done(_):
        watchdog.cancel()
        if orch.ACTIVE.get(cid) is stop:
            orch.ACTIVE.pop(cid, None)
    task.add_done_callback(_done)
    return q, task, stop


@router.post("/conversations/{cid}/chat")
async def chat(cid: str, body: ChatBody, request: Request, user: dict = ChatUser):
    q, task, stop = await start_turn(user, cid, body)

    async def gen():
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), 15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if ev is None:
                    break
                yield _sse(ev)
        finally:
            if not task.done():
                stop.set()   # client went away: stop generating, partial output is persisted

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/conversations/{cid}/stop")
async def stop_generation(cid: str, user: dict = User):
    await _conv(user, cid)
    ev = orch.ACTIVE.get(cid)
    if ev:
        ev.set()
    return {"stopped": bool(ev)}
