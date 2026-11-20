"""Memory service.
  short-term  : recent messages of the current conversation (handled by orchestrator)
  long_term   : durable facts about the user / context across sessions
  project     : knowledge scoped to a project_id
  preference  : how the user wants the assistant to behave
Memories are only created explicitly (user/UI or the memory_save tool) or by a selective extractor
that ignores small talk and anything that looks like a secret."""
import json, logging, re
from bson import ObjectId
from .db import db, now
from .llm import provider

log = logging.getLogger("memory")
KINDS = ("long_term", "project", "preference")
SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{16,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|xox[bp]-[A-Za-z0-9-]+|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY|password\s*[:=]\s*\S+|\b(?:\d[ -]?){13,19}\b)", re.I)


def looks_sensitive(text: str) -> bool:
    return bool(SECRET_RE.search(text))


async def add(user_id, content: str, kind: str = "long_term", project_id=None, source: str = "user") -> dict:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    content = content.strip()
    if not content:
        raise ValueError("empty memory")
    if looks_sensitive(content):
        raise ValueError("Refusing to store something that looks like a secret or credential")
    d = db()
    dup = await d.memories.find_one({"user_id": user_id, "kind": kind, "content": content})
    if dup:
        return dup
    doc = {"user_id": user_id, "kind": kind, "content": content[:2000],
           "project_id": ObjectId(project_id) if project_id and ObjectId.is_valid(str(project_id)) else None,
           "source": source, "created_at": now(), "updated_at": now(), "uses": 0}
    doc["_id"] = (await d.memories.insert_one(doc)).inserted_id
    return doc


async def relevant(user_id, query: str, project_id=None, limit: int = 8) -> list[dict]:
    """Preferences always; project memories for active project; long-term via text match + most recent."""
    d = db()
    out: dict = {}
    async for m in d.memories.find({"user_id": user_id, "kind": "preference"}).sort("updated_at", -1).limit(15):
        out[m["_id"]] = m
    scope = {"user_id": user_id, "$or": [{"kind": "long_term"}] +
             ([{"kind": "project", "project_id": ObjectId(project_id)}] if project_id else [])}
    if query.strip():
        try:
            async for m in d.memories.find({**scope, "$text": {"$search": query[:300]}},
                                           {"score": {"$meta": "textScore"}, "content": 1, "kind": 1}
                                           ).sort([("score", {"$meta": "textScore"})]).limit(limit):
                out[m["_id"]] = m
        except Exception as e:
            log.warning("memory text search failed: %s", e)
    async for m in d.memories.find(scope).sort("updated_at", -1).limit(4):
        out.setdefault(m["_id"], m)
    if out:
        await d.memories.update_many({"_id": {"$in": list(out)}}, {"$inc": {"uses": 1}})
    return list(out.values())


def format_for_prompt(mems: list[dict]) -> str:
    if not mems:
        return ""
    label = {"preference": "Preference", "long_term": "Fact", "project": "Project"}
    lines = [f"- [{label[m['kind']]}] {m['content']}" for m in mems]
    return ("Things you know about this user from saved memory (use when relevant; never claim to remember "
            "anything not listed here):\n" + "\n".join(lines))


MEMORY_CUES = re.compile(
    r"\b(remember|don'?t forget|note that|keep in mind|from now on|always|never|i prefer|i like|i love|i hate|"
    r"i don'?t like|my name|call me|i am a|i'?m a|i work|i live|my (wife|husband|son|daughter|mom|dad|boss|team|"
    r"company|birthday|email|project)|i use|we use|our stack|i'?m (learning|building|working))\b", re.I)


def worth_extracting(user_msg: str) -> bool:
    """Cheap gate before the LLM extraction call: most messages contain nothing durable. On local CPU models
    every extra LLM call also evicts the prompt cache, making the next reply much slower."""
    return bool(MEMORY_CUES.search(user_msg or ""))


EXTRACT_PROMPT = """You decide what is worth saving to an assistant's long-term memory.
From the exchange below, extract ONLY durable, useful facts: stable user preferences (style, tools, stack),
facts about the user's role/projects, important decisions, or explicit "remember this" requests.
Do NOT save: small talk, one-off questions, things only relevant to this conversation, secrets,
passwords, keys, financial or health details. Most exchanges contain nothing worth saving.
Respond with ONLY a JSON array, e.g. [{"kind":"preference","content":"Prefers TypeScript"}] or [].
kind is one of: preference, long_term."""


async def extract_and_save(spec, user_id, user_msg: str, assistant_msg: str) -> list[dict]:
    try:
        raw = await provider.complete(spec, [
            {"role": "system", "content": EXTRACT_PROMPT},
            {"role": "user", "content": f"USER: {user_msg[:3000]}\n\nASSISTANT: {assistant_msg[:2000]}"}])
        m = re.search(r"\[.*\]", raw, re.S)
        items = json.loads(m.group(0)) if m else []
    except Exception as e:
        log.info("memory extraction skipped: %s", e)
        return []
    saved = []
    for it in items[:3]:
        if isinstance(it, dict) and it.get("kind") in ("preference", "long_term") and isinstance(it.get("content"), str):
            try:
                saved.append(await add(user_id, it["content"], it["kind"], source="auto"))
            except ValueError:
                pass
    return saved
