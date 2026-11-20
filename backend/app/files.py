"""Storage for uploaded and generated files (per-user directories under DATA_DIR)."""
import re
from pathlib import Path
from bson import ObjectId
from .config import get_settings
from .db import db, now

MIME = {".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".csv": "text/csv",
        ".png": "image/png", ".json": "application/json", ".txt": "text/plain", ".md": "text/markdown",
        ".pdf": "application/pdf", ".html": "text/html", ".svg": "image/svg+xml"}


def safe_name(name: str) -> str:
    name = re.sub(r"[^\w.\- ]", "_", Path(name).name).strip() or "file"
    return name[:120]


def user_dir(user_id, sub: str) -> Path:
    p = get_settings().data_dir / sub / str(user_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


async def save_generated(user: dict, filename: str, data: bytes, conversation_id: str | None = None) -> dict:
    fid = ObjectId()
    name = safe_name(filename)
    path = user_dir(user["_id"], "generated") / f"{fid}{Path(name).suffix.lower()}"
    path.write_bytes(data)
    doc = {"_id": fid, "user_id": user["_id"], "filename": name, "path": str(path), "size": len(data),
           "mime": MIME.get(path.suffix, "application/octet-stream"), "conversation_id": conversation_id, "created_at": now()}
    await db().generated_files.insert_one(doc)
    return {"file_id": str(fid), "filename": name, "download_url": f"/api/files/{fid}"}
