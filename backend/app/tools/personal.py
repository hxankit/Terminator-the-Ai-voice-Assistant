"""Memory and task/reminder tools."""
from datetime import datetime
from bson import ObjectId
from pydantic import BaseModel, Field
from typing import Literal
from .. import memory
from ..db import db, now
from .base import tool, ToolError, ToolContext


class MemSave(BaseModel):
    content: str = Field(min_length=3, max_length=1000)
    kind: Literal["long_term", "preference", "project"] = "long_term"


@tool("memory_save", "Save a durable fact or preference about the user. Only use when the user asks you to "
      "remember something or states a lasting preference. Never save secrets.", MemSave, risk="write")
async def memory_save(a: MemSave, ctx: ToolContext):
    if a.kind == "project" and not ctx.project_id:
        raise ToolError("No active project for a project memory")
    try:
        m = await memory.add(ctx.user["_id"], a.content, a.kind, ctx.project_id, source="assistant")
    except ValueError as e:
        raise ToolError(str(e))
    return {"saved": True, "id": str(m["_id"])}


class MemSearch(BaseModel):
    query: str = Field(max_length=300)


@tool("memory_search", "Search the user's saved memories.", MemSearch)
async def memory_search(a: MemSearch, ctx: ToolContext):
    ms = await memory.relevant(ctx.user["_id"], a.query, ctx.project_id)
    return [{"kind": m["kind"], "content": m["content"]} for m in ms] or "No matching memories."


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    notes: str = Field("", max_length=4000)
    due_at: datetime | None = Field(None, description="ISO 8601 datetime with timezone, for reminders/deadlines")
    priority: Literal["low", "normal", "high"] = "normal"


@tool("task_create", "Create a task or reminder for the user.", TaskCreate, risk="write")
async def task_create(a: TaskCreate, ctx: ToolContext):
    doc = {**a.model_dump(), "user_id": ctx.user["_id"], "status": "open", "created_at": now(),
           "project_id": ObjectId(ctx.project_id) if ctx.project_id else None}
    r = await db().tasks.insert_one(doc)
    return {"created": True, "id": str(r.inserted_id), "title": a.title, "due_at": a.due_at}


class TaskList(BaseModel):
    status: Literal["open", "done", "all"] = "open"


@tool("task_list", "List the user's tasks and reminders, soonest due first.", TaskList)
async def task_list(a: TaskList, ctx: ToolContext):
    q = {"user_id": ctx.user["_id"]} | ({} if a.status == "all" else {"status": a.status})
    return [{"id": str(t["_id"]), "title": t["title"], "status": t["status"], "due_at": t.get("due_at"),
             "priority": t.get("priority")} async for t in db().tasks.find(q).sort([("due_at", 1)]).limit(100)]


class TaskUpdate(BaseModel):
    task_id: str
    status: Literal["open", "done"] | None = None
    title: str | None = Field(None, max_length=300)
    due_at: datetime | None = None


@tool("task_update", "Update or complete a task.", TaskUpdate, risk="write")
async def task_update(a: TaskUpdate, ctx: ToolContext):
    if not ObjectId.is_valid(a.task_id):
        raise ToolError("invalid task id")
    upd = {k: v for k, v in a.model_dump(exclude={"task_id"}).items() if v is not None}
    r = await db().tasks.update_one({"_id": ObjectId(a.task_id), "user_id": ctx.user["_id"]}, {"$set": upd})
    if not r.matched_count:
        raise ToolError("task not found")
    return {"updated": True}


class AskArgs(BaseModel):
    question: str = Field(min_length=3, max_length=1000)
    options: list[str] = Field(default_factory=list, max_length=6, description="Optional suggested answers")


@tool("ask_user", "Pause and ask the user a clarifying question when essential information is missing and "
      "cannot be reasonably assumed. The user's reply is returned as the result. Use sparingly.", AskArgs, risk="question")
async def ask_user(a: AskArgs, ctx: ToolContext):  # never executed directly: answered via resolve_pending
    raise ToolError("ask_user is resolved by the user's answer")
