"""Password hashing, JWT sessions (httpOnly cookie or Bearer), secret encryption, rate limiting."""
import hashlib, secrets, time
from datetime import timedelta
from collections import defaultdict, deque
import bcrypt, jwt
from cryptography.fernet import Fernet
from fastapi import Depends, HTTPException, Request
from .config import get_settings
from .db import db, now, oid

COOKIE = "session"


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt(12)).decode()


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), hashed.encode())
    except ValueError:
        return False


def make_token(user_id: str) -> str:
    s = get_settings()
    t = int(time.time())
    return jwt.encode({"sub": user_id, "iat": t, "exp": t + s.jwt_ttl_minutes * 60}, s.jwt_secret, "HS256")


def _fernet() -> Fernet:
    return Fernet(get_settings().secret_key.encode())


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode() if value else ""


def decrypt(value: str) -> str:
    return _fernet().decrypt(value.encode()).decode() if value else ""


DEVICE_PREFIX = "dt_"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_device_token() -> str:
    return DEVICE_PREFIX + secrets.token_urlsafe(32)


async def user_from_token(token: str | None) -> dict | None:
    """Resolve a session JWT or a long-lived, revocable device token (voice service, scripts) to a user."""
    if not token:
        return None
    if token.startswith(DEVICE_PREFIX):
        dev = await db().device_tokens.find_one({"token_hash": hash_token(token)})
        if not dev:
            return None
        if not dev.get("last_used_at") or now() - dev["last_used_at"] > timedelta(minutes=1):
            await db().device_tokens.update_one({"_id": dev["_id"]}, {"$set": {"last_used_at": now()}})
        return await db().users.find_one({"_id": dev["user_id"]})
    try:
        payload = jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])
        return await db().users.find_one({"_id": oid(payload["sub"])})
    except Exception:  # bad signature, expired, or malformed subject id
        return None


async def current_user(request: Request) -> dict:
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401, "Not authenticated")
    user = await user_from_token(token)
    if not user:
        raise HTTPException(401, "Invalid or expired session")
    return user


class RateLimiter:
    """In-process sliding-window limiter. Use a Redis-backed limiter when running multiple workers."""
    def __init__(self):
        self.hits: dict[str, deque] = defaultdict(deque)

    def check(self, key: str, limit: int, window: float = 60.0):
        q, t = self.hits[key], time.monotonic()
        while q and t - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            raise HTTPException(429, "Too many requests, slow down")
        q.append(t)


limiter = RateLimiter()


def rate_limited(limit: int | None = None):
    async def dep(request: Request, user: dict = Depends(current_user)):
        limiter.check(f"u:{user['_id']}:{request.url.path}", limit or get_settings().rate_limit_per_minute)
        return user
    return dep
