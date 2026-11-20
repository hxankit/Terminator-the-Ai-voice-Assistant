"""MongoDB access (async PyMongo) and index definitions."""
from datetime import datetime, timezone
from bson import ObjectId
from pymongo import AsyncMongoClient, ASCENDING, DESCENDING, TEXT
from .config import get_settings

_client: AsyncMongoClient | None = None


def now() -> datetime:
    return datetime.now(timezone.utc)


def db():
    global _client
    if _client is None:
        _client = AsyncMongoClient(get_settings().mongo_uri, tz_aware=True)
    return _client[get_settings().mongo_db]


async def close():
    global _client
    if _client is not None:
        await _client.close()
        _client = None


def oid(value: str) -> ObjectId:
    from fastapi import HTTPException
    try:
        return ObjectId(value)
    except Exception:
        raise HTTPException(400, "Invalid id")


def ser(doc: dict | None) -> dict | None:
    """Serialize a Mongo doc for JSON: _id -> id, ObjectIds -> str, drop secrets."""
    if doc is None:
        return None
    out = {}
    for k, v in doc.items():
        if k in ("password_hash", "api_key_enc", "embedding"):
            continue
        key = "id" if k == "_id" else k
        out[key] = str(v) if isinstance(v, ObjectId) else v
    if "token_hash" in out:
        out.pop("token_hash")
    if "api_key_enc" in doc:
        out["has_api_key"] = bool(doc["api_key_enc"])
    return out


async def ensure_indexes():
    d = db()
    await d.users.create_index("email", unique=True)
    await d.conversations.create_index([("user_id", ASCENDING), ("updated_at", DESCENDING)])
    await d.conversations.create_index([("title", TEXT)])
    await d.messages.create_index([("conversation_id", ASCENDING), ("created_at", ASCENDING)])
    await d.messages.create_index([("user_id", ASCENDING), ("content", TEXT)])
    await d.memories.create_index([("user_id", ASCENDING), ("kind", ASCENDING), ("project_id", ASCENDING)])
    await d.memories.create_index([("content", TEXT)])
    await d.providers.create_index("user_id")
    await d.projects.create_index("user_id")
    await d.documents.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
    await d.chunks.create_index([("user_id", ASCENDING), ("document_id", ASCENDING)])
    await d.chunks.create_index([("text", TEXT)])
    await d.tasks.create_index([("user_id", ASCENDING), ("status", ASCENDING), ("due_at", ASCENDING)])
    await d.tool_runs.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
    await d.usage.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
    await d.agent_configs.create_index([("user_id", ASCENDING), ("agent", ASCENDING)], unique=True)
    await d.contacts.create_index([("user_id", ASCENDING), ("name", ASCENDING)])
    await d.device_tokens.create_index("token_hash", unique=True)
    await d.device_tokens.create_index("user_id")
    await d.notifications.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
    await d.env_snapshots.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
    await d.integrations.create_index([("user_id", ASCENDING), ("kind", ASCENDING)], unique=True)
