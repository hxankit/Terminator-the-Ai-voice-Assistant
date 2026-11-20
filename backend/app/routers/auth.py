from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field, field_validator
from pymongo.errors import DuplicateKeyError
from ..config import get_settings
from ..db import db, now, oid, ser
from ..deps import User
from ..security import COOKIE, hash_password, verify_password, make_token, limiter, new_device_token, hash_token

router = APIRouter(prefix="/api/auth", tags=["auth"])


class Register(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(min_length=8, max_length=128)
    name: str = Field("", max_length=80)

    @field_validator("email")
    @classmethod
    def _email(cls, v):
        import re
        v = v.strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v):
            raise ValueError("invalid email")
        return v


class Login(BaseModel):
    email: str
    password: str


def _set_cookie(resp: Response, token: str):
    s = get_settings()
    resp.set_cookie(COOKIE, token, httponly=True, secure=s.is_prod, samesite="lax",
                    max_age=s.jwt_ttl_minutes * 60, path="/")


def public_user(u: dict) -> dict:
    out = ser(u)
    return {k: out[k] for k in ("id", "email", "name", "prefs", "created_at") if k in out}


@router.post("/register")
async def register(body: Register, request: Request, response: Response):
    limiter.check(f"ip:{request.client.host}:register", get_settings().auth_rate_limit_per_minute)
    if not get_settings().allow_registration:
        raise HTTPException(403, "Registration is disabled")
    doc = {"email": body.email, "name": body.name or body.email.split("@")[0], "password_hash": hash_password(body.password),
           "prefs": {"auto_memory": True}, "created_at": now()}
    try:
        doc["_id"] = (await db().users.insert_one(doc)).inserted_id
    except DuplicateKeyError:
        raise HTTPException(409, "An account with this email already exists")
    token = make_token(str(doc["_id"]))
    _set_cookie(response, token)
    return {"user": public_user(doc), "token": token}


@router.post("/login")
async def login(body: Login, request: Request, response: Response):
    limiter.check(f"ip:{request.client.host}:login", get_settings().auth_rate_limit_per_minute)
    u = await db().users.find_one({"email": body.email.strip().lower()})
    if not u or not verify_password(body.password, u["password_hash"]):
        raise HTTPException(401, "Invalid email or password")
    token = make_token(str(u["_id"]))
    _set_cookie(response, token)
    return {"user": public_user(u), "token": token}


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
async def me(user: dict = User):
    return public_user(user)


class Prefs(BaseModel):
    provider_id: str | None = None
    model: str | None = None
    auto_memory: bool | None = None
    theme: str | None = Field(None, pattern="^(light|dark|system)$")
    llm_routing: bool | None = None          # let the model classify ambiguous requests (extra LLM call)
    fast_path: bool | None = None            # instant handling of common commands without the LLM
    default_country_code: str | None = Field(None, pattern=r"^\d{0,4}$")   # for numbers without +CC
    fallback_provider_id: str | None = None   # used when the main model fails ("" = automatic)
    approval_mode: str | None = Field(None, pattern="^(ask|risky|never)$")   # see app/approvals.py
    fallback_model: str | None = None


@router.patch("/prefs")
async def update_prefs(body: Prefs, user: dict = User):
    upd = {f"prefs.{k}": v for k, v in body.model_dump(exclude_unset=True).items()}
    if upd:
        await db().users.update_one({"_id": user["_id"]}, {"$set": upd})
    return public_user(await db().users.find_one({"_id": user["_id"]}))


# ---------- device tokens (voice service, scripts) ----------
class DeviceIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


@router.post("/device-tokens")
async def create_device_token(body: DeviceIn, user: dict = User):
    """Create a long-lived, revocable token. The plain token is returned once and only its hash is stored."""
    token = new_device_token()
    doc = {"user_id": user["_id"], "name": body.name, "token_hash": hash_token(token), "created_at": now(), "last_used_at": None}
    doc["_id"] = (await db().device_tokens.insert_one(doc)).inserted_id
    return {"id": str(doc["_id"]), "name": body.name, "token": token}


@router.get("/device-tokens")
async def list_device_tokens(user: dict = User):
    return [{"id": str(d["_id"]), "name": d["name"], "created_at": d["created_at"], "last_used_at": d.get("last_used_at")}
            async for d in db().device_tokens.find({"user_id": user["_id"]}).sort("created_at", -1)]


@router.delete("/device-tokens/{tid}")
async def revoke_device_token(tid: str, user: dict = User):
    r = await db().device_tokens.delete_one({"_id": oid(tid), "user_id": user["_id"]})
    if not r.deleted_count:
        raise HTTPException(404, "Device not found")
    return {"ok": True}
