"""Realtime hub: per-user WebSocket connections, server push, notifications, reminder scheduler."""
import asyncio, json, logging
from collections import defaultdict
from fastapi import WebSocket
from . import desktop
from .db import db, now, ser

log = logging.getLogger("realtime")


class Hub:
    def __init__(self):
        self.sockets: dict[str, set[WebSocket]] = defaultdict(set)

    def add(self, uid: str, ws: WebSocket):
        self.sockets[uid].add(ws)

    def remove(self, uid: str, ws: WebSocket):
        self.sockets[uid].discard(ws)

    def online(self, uid: str) -> bool:
        return bool(self.sockets.get(uid))

    async def send(self, ws: WebSocket, ev: dict) -> bool:
        try:
            await ws.send_text(json.dumps(ev, default=str))
            return True
        except (TypeError, ValueError):
            log.exception("could not serialize event %s", ev.get("type"))
        except Exception as e:  # closed socket; the endpoint's finally removes it
            log.debug("send failed (%s); socket likely closed", type(e).__name__)
        return False

    async def push(self, uid: str, ev: dict):
        for ws in list(self.sockets.get(uid, ())):
            await self.send(ws, ev)


hub = Hub()


async def notify(user_id, kind: str, title: str, body: str = "", data: dict | None = None) -> dict:
    """Persist an assistant-initiated message and push it live if the user is connected."""
    doc = {"user_id": user_id, "kind": kind, "title": title, "body": body, "data": data or {},
           "read": False, "created_at": now()}
    doc["_id"] = (await db().notifications.insert_one(doc)).inserted_id
    await hub.push(str(user_id), {"type": "notification", "item": ser(doc)})
    return doc


async def reminder_loop(interval: float = 30.0):
    """Every `interval` seconds, turn due tasks into reminder notifications (once each)."""
    while True:
        try:
            async for t in db().tasks.find({"status": "open", "due_at": {"$lte": now()}, "reminded": {"$ne": True}}).limit(200):
                res = await db().tasks.update_one({"_id": t["_id"], "reminded": {"$ne": True}}, {"$set": {"reminded": True}})
                if res.modified_count:   # claim atomically so multiple workers don't double-notify
                    timer = t.get("kind") == "timer"
                    title = f"Timer done: {t['title']}" if timer else f"Reminder: {t['title']}"
                    if timer:
                        await db().tasks.update_one({"_id": t["_id"]}, {"$set": {"status": "done"}})
                    await notify(t["user_id"], "timer" if timer else "reminder", title, t.get("notes", ""),
                                 {"task_id": str(t["_id"]), "due_at": t.get("due_at")})
                    if desktop.unavailable() is None:
                        await desktop.notify(title, t.get("notes", ""), "critical")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("reminder loop error")
        await asyncio.sleep(interval)
