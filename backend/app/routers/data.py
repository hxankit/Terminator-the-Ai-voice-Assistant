"""Memory, projects, documents (knowledge base), tasks, tool runs/approvals, files, agents, usage."""
import asyncio
from pathlib import Path
from typing import Literal
from bson import ObjectId
from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from .. import memory as mem
from .. import contacts as contacts_mod
from .. import environment as env_mod
from .. import orchestrator as orch
from ..agents.registry import AGENTS
from ..config import get_settings
from ..db import db, now, oid, ser
from ..deps import User
from ..files import safe_name, user_dir
from ..rag.extract import SUPPORTED
from ..rag.service import index_document, retrieve
from ..tools.base import REGISTRY

router = APIRouter(prefix="/api", tags=["data"])


# ---------- memory ----------
class MemIn(BaseModel):
    content: str = Field(min_length=1, max_length=2000)
    kind: Literal["long_term", "project", "preference"] = "long_term"
    project_id: str | None = None


@router.get("/memories")
async def list_memories(user: dict = User, kind: str | None = None, q: str = ""):
    flt: dict = {"user_id": user["_id"]}
    if kind:
        flt["kind"] = kind
    if q.strip():
        flt["$text"] = {"$search": q}
    return [ser(m) async for m in db().memories.find(flt).sort("updated_at", -1).limit(500)]


@router.post("/memories")
async def create_memory(body: MemIn, user: dict = User):
    try:
        return ser(await mem.add(user["_id"], body.content, body.kind, body.project_id, source="user"))
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.put("/memories/{mid}")
async def update_memory(mid: str, body: MemIn, user: dict = User):
    if mem.looks_sensitive(body.content):
        raise HTTPException(422, "Refusing to store something that looks like a secret")
    r = await db().memories.update_one({"_id": oid(mid), "user_id": user["_id"]},
                                       {"$set": {"content": body.content, "kind": body.kind, "updated_at": now()}})
    if not r.matched_count:
        raise HTTPException(404, "Memory not found")
    return {"ok": True}


@router.delete("/memories/{mid}")
async def delete_memory(mid: str, user: dict = User):
    r = await db().memories.delete_one({"_id": oid(mid), "user_id": user["_id"]})
    if not r.deleted_count:
        raise HTTPException(404, "Memory not found")
    return {"ok": True}


# ---------- projects ----------
class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field("", max_length=4000)


@router.get("/projects")
async def list_projects(user: dict = User):
    return [ser(p) async for p in db().projects.find({"user_id": user["_id"]}).sort("name", 1)]


@router.post("/projects")
async def create_project(body: ProjectIn, user: dict = User):
    doc = {"user_id": user["_id"], **body.model_dump(), "created_at": now()}
    doc["_id"] = (await db().projects.insert_one(doc)).inserted_id
    return ser(doc)


@router.put("/projects/{pid}")
async def update_project(pid: str, body: ProjectIn, user: dict = User):
    await db().projects.update_one({"_id": oid(pid), "user_id": user["_id"]}, {"$set": body.model_dump()})
    return {"ok": True}


@router.delete("/projects/{pid}")
async def delete_project(pid: str, user: dict = User):
    p = oid(pid)
    await db().projects.delete_one({"_id": p, "user_id": user["_id"]})
    await db().conversations.update_many({"project_id": p, "user_id": user["_id"]}, {"$set": {"project_id": None}})
    await db().memories.delete_many({"project_id": p, "user_id": user["_id"]})
    return {"ok": True}


# ---------- documents / knowledge base ----------
@router.post("/documents")
async def upload_document(file: UploadFile = File(...), user: dict = User):
    s = get_settings()
    name = safe_name(file.filename or "upload")
    ext = Path(name).suffix.lower()
    if ext not in SUPPORTED:
        raise HTTPException(415, f"Unsupported type {ext}. Supported: {', '.join(sorted(SUPPORTED))}")
    data = await file.read(s.max_upload_mb * 1024 * 1024 + 1)
    if len(data) > s.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"File exceeds {s.max_upload_mb} MB")
    if not data:
        raise HTTPException(422, "Empty file")
    if ext == ".pdf" and not data.startswith(b"%PDF"):
        raise HTTPException(415, "File is not a valid PDF")
    if ext in (".docx", ".xlsx") and not data.startswith(b"PK"):
        raise HTTPException(415, "File is not a valid Office document")
    did = ObjectId()
    path = user_dir(user["_id"], "uploads") / f"{did}{ext}"
    path.write_bytes(data)
    doc = {"_id": did, "user_id": user["_id"], "filename": name, "path": str(path), "size": len(data),
           "status": "processing", "created_at": now()}
    await db().documents.insert_one(doc)
    result = await index_document(user, did, path)   # synchronous so the UI knows when it's usable
    return ser({**doc, **result})


@router.get("/documents")
async def list_documents(user: dict = User):
    return [ser(d) async for d in db().documents.find({"user_id": user["_id"]}, {"path": 0}).sort("created_at", -1).limit(500)]


@router.post("/documents/{did}/reindex")
async def reindex_document(did: str, user: dict = User):
    from ..rag.service import reindex
    d = await db().documents.find_one({"_id": oid(did), "user_id": user["_id"]})
    if not d:
        raise HTTPException(404, "Document not found")
    return {**ser({k: v for k, v in d.items() if k != "path"}), **await reindex(user, d)}


@router.delete("/documents/{did}")
async def delete_document(did: str, user: dict = User):
    d = await db().documents.find_one({"_id": oid(did), "user_id": user["_id"]})
    if not d:
        raise HTTPException(404, "Document not found")
    await db().chunks.delete_many({"document_id": d["_id"]})
    await db().documents.delete_one({"_id": d["_id"]})
    Path(d["path"]).unlink(missing_ok=True)
    return {"ok": True}


class KBQuery(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    k: int = Field(6, ge=1, le=20)


@router.post("/kb/search")
async def kb_search(body: KBQuery, user: dict = User):
    return await retrieve(user, body.query, body.k)


# ---------- tasks ----------
class TaskIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    notes: str = Field("", max_length=4000)
    due_at: str | None = None
    priority: Literal["low", "normal", "high"] = "normal"
    status: Literal["open", "done"] = "open"


def _due(v):
    from datetime import datetime
    if not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(422, "due_at must be ISO 8601")


@router.get("/tasks")
async def list_tasks(user: dict = User, status: str = "open"):
    flt = {"user_id": user["_id"]} | ({} if status == "all" else {"status": status})
    return [ser(t) async for t in db().tasks.find(flt).sort([("status", 1), ("due_at", 1)]).limit(500)]


@router.post("/tasks")
async def create_task(body: TaskIn, user: dict = User):
    doc = {**body.model_dump(), "due_at": _due(body.due_at), "user_id": user["_id"], "created_at": now()}
    doc["_id"] = (await db().tasks.insert_one(doc)).inserted_id
    return ser(doc)


@router.put("/tasks/{tid}")
async def update_task(tid: str, body: TaskIn, user: dict = User):
    r = await db().tasks.update_one({"_id": oid(tid), "user_id": user["_id"]},
                                    {"$set": {**body.model_dump(), "due_at": _due(body.due_at)}})
    if not r.matched_count:
        raise HTTPException(404, "Task not found")
    return {"ok": True}


@router.delete("/tasks/{tid}")
async def delete_task(tid: str, user: dict = User):
    await db().tasks.delete_one({"_id": oid(tid), "user_id": user["_id"]})
    return {"ok": True}


# ---------- tools, approvals, agents ----------
@router.get("/agents")
async def list_agents(user: dict = User):
    return [{"key": a.key, "name": a.name, "description": a.description,
             "tools": [{"name": t, "risk": REGISTRY[t].risk, "available": not REGISTRY[t].unavailable(),
                        "reason": REGISTRY[t].unavailable()} for t in a.tools if t in REGISTRY]} for a in AGENTS.values()]


@router.get("/tools/runs")
async def list_runs(user: dict = User, limit: int = 100):
    return [ser(r) async for r in db().tool_runs.find({"user_id": user["_id"]}).sort("created_at", -1).limit(min(limit, 500))]


class Decision(BaseModel):
    approve: bool | None = None
    answer: str | None = Field(None, max_length=4000)


@router.post("/tools/runs/{rid}/decision")
async def decide(rid: str, body: Decision, user: dict = User):
    """Approve/deny a pending action, or answer an ask_user question."""
    try:
        return await orch.resolve_approval(user, oid(rid), body.approve, body.answer)
    except ValueError as e:
        raise HTTPException(404 if "No pending" in str(e) else 422, str(e))


# ---------- notifications (assistant-initiated messages) ----------
@router.get("/notifications")
async def list_notifications(user: dict = User, unread: bool = False):
    flt = {"user_id": user["_id"]} | ({"read": False} if unread else {})
    return [ser(n) async for n in db().notifications.find(flt).sort("created_at", -1).limit(100)]


@router.post("/notifications/read")
async def mark_read(user: dict = User):
    await db().notifications.update_many({"user_id": user["_id"], "read": False}, {"$set": {"read": True}})
    return {"ok": True}


# ---------- generated files ----------
@router.get("/files/{fid}")
async def download(fid: str, user: dict = User):
    f = await db().generated_files.find_one({"_id": oid(fid), "user_id": user["_id"]})
    if not f or not Path(f["path"]).exists():
        raise HTTPException(404, "File not found")
    return FileResponse(f["path"], media_type=f["mime"], filename=f["filename"])


# ---------- usage ----------
@router.get("/usage")
async def usage(user: dict = User, days: int = 30):
    from datetime import timedelta
    since = now() - timedelta(days=max(1, min(days, 365)))
    rows = [r async for r in await db().usage.aggregate([
        {"$match": {"user_id": user["_id"], "created_at": {"$gte": since}}},
        {"$group": {"_id": "$model", "calls": {"$sum": 1}, "prompt_tokens": {"$sum": "$prompt_tokens"},
                    "completion_tokens": {"$sum": "$completion_tokens"}, "avg_ms": {"$avg": "$duration_ms"},
                    "avg_ttft_ms": {"$avg": "$ttft_ms"}}}])]
    return [{"model": r.pop("_id"), **r} for r in rows]


# ---------- contacts ----------
class ContactIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    phones: list[str] = Field(default_factory=list, max_length=10)
    emails: list[str] = Field(default_factory=list, max_length=10)
    aliases: list[str] = Field(default_factory=list, max_length=10, description="Other names, e.g. 'Mom'")
    notes: str = Field("", max_length=1000)


def _clean_contact(body: ContactIn, user: dict) -> dict:
    cc = (user.get("prefs") or {}).get("default_country_code", "")
    phones = []
    for p in body.phones:
        if p.strip():
            try:
                phones.append("+" + contacts_mod.normalize_phone(p, cc))
            except contacts_mod.ContactError as e:
                raise HTTPException(422, str(e))
    return {"name": body.name.strip(), "phones": phones, "emails": [e.strip() for e in body.emails if e.strip()],
            "aliases": [a.strip() for a in body.aliases if a.strip()], "notes": body.notes}


@router.get("/contacts")
async def list_contacts(user: dict = User, q: str = ""):
    if q.strip():
        return [ser(c) for c in await contacts_mod.resolve(user["_id"], q)]
    return [ser(c) async for c in db().contacts.find({"user_id": user["_id"]}).sort("name", 1).limit(5000)]


@router.post("/contacts")
async def create_contact(body: ContactIn, user: dict = User):
    doc = {**_clean_contact(body, user), "user_id": user["_id"], "created_at": now(), "updated_at": now()}
    doc["_id"] = (await db().contacts.insert_one(doc)).inserted_id
    return ser(doc)


@router.put("/contacts/{cid}")
async def update_contact(cid: str, body: ContactIn, user: dict = User):
    r = await db().contacts.update_one({"_id": oid(cid), "user_id": user["_id"]},
                                       {"$set": {**_clean_contact(body, user), "updated_at": now()}})
    if not r.matched_count:
        raise HTTPException(404, "Contact not found")
    return {"ok": True}


@router.delete("/contacts/{cid}")
async def delete_contact(cid: str, user: dict = User):
    await db().contacts.delete_one({"_id": oid(cid), "user_id": user["_id"]})
    return {"ok": True}


@router.post("/contacts/import")
async def import_contacts(file: UploadFile = File(...), user: dict = User):
    """Import a .vcf (vCard) export from your phone or Google Contacts."""
    data = await file.read(5 * 1024 * 1024 + 1)
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(413, "File exceeds 5 MB")
    cards = contacts_mod.parse_vcards(data.decode("utf-8", errors="replace"))
    if not cards:
        raise HTTPException(422, "No contacts found. Export contacts as a .vcf (vCard) file.")
    cc = (user.get("prefs") or {}).get("default_country_code", "")
    new = merged = skipped = 0
    for c in cards:
        phones = []
        for p in c["phones"]:
            try:
                phones.append("+" + contacts_mod.normalize_phone(p, cc))
            except contacts_mod.ContactError:
                pass
        if not phones and not c["emails"]:
            skipped += 1
            continue
        if await contacts_mod.upsert(user["_id"], c["name"], phones, c["emails"]):
            new += 1
        else:
            merged += 1
    return {"imported": new, "merged": merged, "skipped": skipped, "total": len(cards)}


# ---------- saved environments (open tabs, projects, apps) ----------
def _env_out(d: dict) -> dict:
    return {"id": str(d["_id"]), "reason": d["reason"], "created_at": d["created_at"], "summary": d.get("summary", ""),
            "tabs": sum(len(w) for b in d.get("browsers", []) for w in b["windows"])}


@router.get("/environments")
async def list_environments(user: dict = User):
    return [_env_out(d) async for d in db().env_snapshots.find({"user_id": user["_id"]}).sort("created_at", -1).limit(30)]


@router.post("/environments/{eid}/restore")
async def restore_environment_api(eid: str, user: dict = User):
    from ..desktop import unavailable
    if reason := unavailable():
        raise HTTPException(409, f"Desktop control unavailable: {reason}")
    d = await db().env_snapshots.find_one({"_id": oid(eid), "user_id": user["_id"]})
    if not d:
        raise HTTPException(404, "Snapshot not found")
    return {"restored": env_mod.restore(d)}


@router.delete("/environments/{eid}")
async def delete_environment(eid: str, user: dict = User):
    await db().env_snapshots.delete_one({"_id": oid(eid), "user_id": user["_id"]})
    return {"ok": True}


# ---------- WhatsApp bridge ----------
@router.get("/integrations/whatsapp")
async def whatsapp_status(user: dict = User):
    from ..integrations import whatsapp as wa
    st = await wa.status()
    if st["state"] == "connected":
        st["chats"] = len(await asyncio.to_thread(wa.recent_chats, 1000))
    return st


# ---------- Microsoft 365 (Teams, Outlook) ----------
class MsConnect(BaseModel):
    client_id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$", description="Application (client) ID of your app registration")
    tenant_id: str = Field("organizations", max_length=64, description="Directory (tenant) ID, or 'organizations'")


@router.get("/integrations/ms365")
async def ms365_status(user: dict = User):
    from ..integrations import ms365
    return await ms365.status(user)


@router.post("/integrations/ms365/connect")
async def ms365_connect(body: MsConnect, user: dict = User):
    from ..integrations import ms365
    try:
        return await ms365.start_sign_in(user, body.client_id, body.tenant_id)
    except ms365.GraphError as e:
        raise HTTPException(422, str(e))


@router.post("/integrations/ms365/disconnect")
async def ms365_disconnect(user: dict = User):
    from ..integrations import ms365
    await ms365.disconnect(user)
    return {"ok": True}


# ---------- Gmail (app password) ----------
class GmailConnect(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    app_password: str = Field(min_length=16, max_length=40)


@router.get("/integrations/gmail")
async def gmail_status(user: dict = User):
    from ..integrations import gmail
    return await gmail.status(user)


@router.post("/integrations/gmail/connect")
async def gmail_connect(body: GmailConnect, user: dict = User):
    from ..integrations import gmail
    try:
        return await gmail.connect(user, body.email, body.app_password)
    except gmail.GmailError as e:
        raise HTTPException(422, str(e))


@router.post("/integrations/gmail/disconnect")
async def gmail_disconnect(user: dict = User):
    from ..integrations import gmail
    await gmail.disconnect(user)
    return {"ok": True}


# ---------- Instagram (unofficial; password used once, never stored) ----------
class InstaConnect(BaseModel):
    username: str = Field(min_length=1, max_length=60)
    password: str = Field(min_length=1, max_length=200)


class InstaCode(BaseModel):
    code: str = Field(min_length=4, max_length=12)


@router.get("/integrations/instagram")
async def instagram_status(user: dict = User):
    from ..integrations import instagram
    return await instagram.status(user)


@router.post("/integrations/instagram/connect")
async def instagram_connect(body: InstaConnect, user: dict = User):
    from ..integrations import instagram
    return await instagram.start_login(user, body.username, body.password)


@router.post("/integrations/instagram/code")
async def instagram_code(body: InstaCode, user: dict = User):
    from ..integrations import instagram
    if not instagram.submit_code(user, body.code):
        raise HTTPException(409, "Instagram isn't waiting for a code right now")
    return {"ok": True}


@router.post("/integrations/instagram/disconnect")
async def instagram_disconnect(user: dict = User):
    from ..integrations import instagram
    await instagram.disconnect(user)
    return {"ok": True}
