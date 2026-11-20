"""Provider & model management. API keys are encrypted at rest and never returned."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, HttpUrl
from ..db import db, now, oid, ser
from ..deps import User
from ..llm import provider, cloudflare
from ..llm.registry import user_providers, resolve, ENV_PROVIDER_ID
from ..security import encrypt, decrypt

router = APIRouter(prefix="/api/providers", tags=["models"])


class ModelCfg(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    temperature: float = Field(0.7, ge=0, le=2)
    max_tokens: int | None = Field(None, ge=1, le=200_000)
    context_window: int = Field(8192, ge=512, le=2_000_000)
    supports_tools: bool | None = None   # None = unverified; set by /verify


class ProviderIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    base_url: HttpUrl
    api_key: str | None = Field(None, max_length=500)   # omit on update to keep existing
    models: list[ModelCfg] = Field(default_factory=list, max_length=50)
    embedding_model: str = Field("", max_length=200)


def _out(p: dict) -> dict:
    o = ser(p)
    o["has_api_key"] = bool(p.get("api_key_enc")) if p["_id"] != ENV_PROVIDER_ID else None
    return o


@router.get("")
async def list_providers(user: dict = User):
    return [_out(p) for p in await user_providers(user["_id"])]


async def _complete_base_url(base_url: str, api_key: str) -> str:
    """Cloudflare preset saved before 'Detect account': look the account up now instead of failing later."""
    if not cloudflare.needs_account(base_url):
        return base_url
    try:
        return await cloudflare.fill_account(base_url, api_key)
    except provider.LLMError as e:
        raise HTTPException(422, str(e))


@router.post("")
async def create_provider(body: ProviderIn, user: dict = User):
    base_url = await _complete_base_url(str(body.base_url).rstrip("/"), body.api_key or "")
    doc = {"user_id": user["_id"], "name": body.name, "base_url": base_url,
           "api_key_enc": encrypt(body.api_key or ""), "models": [m.model_dump() for m in body.models],
           "embedding_model": body.embedding_model, "created_at": now()}
    doc["_id"] = (await db().providers.insert_one(doc)).inserted_id
    return _out(doc)


async def _own(user, pid: str) -> dict:
    if pid == ENV_PROVIDER_ID:
        raise HTTPException(400, "The env-configured provider is read-only; edit .env instead")
    p = await db().providers.find_one({"_id": oid(pid), "user_id": user["_id"]})
    if not p:
        raise HTTPException(404, "Provider not found")
    return p


@router.put("/{pid}")
async def update_provider(pid: str, body: ProviderIn, user: dict = User):
    p = await _own(user, pid)
    key = body.api_key if body.api_key is not None else decrypt(p.get("api_key_enc", ""))
    upd = {"name": body.name, "base_url": await _complete_base_url(str(body.base_url).rstrip("/"), key),
           "embedding_model": body.embedding_model,
           "models": [m.model_dump() for m in body.models]}
    if body.api_key is not None:
        upd["api_key_enc"] = encrypt(body.api_key)
    await db().providers.update_one({"_id": p["_id"]}, {"$set": upd})
    return _out(await db().providers.find_one({"_id": p["_id"]}))


@router.delete("/{pid}")
async def delete_provider(pid: str, user: dict = User):
    p = await _own(user, pid)
    await db().providers.delete_one({"_id": p["_id"]})
    return {"ok": True}


class CloudflareLookup(BaseModel):
    api_key: str = Field(min_length=10, max_length=500)


@router.post("/cloudflare/accounts")
async def cloudflare_accounts(body: CloudflareLookup, user: dict = User):
    """Find the account id(s) a Cloudflare API token can use, to build the Workers AI base URL.
    The token is only forwarded to Cloudflare; it is not stored by this call."""
    try:
        accts = await cloudflare.accounts(body.api_key)
    except provider.LLMError as e:
        raise HTTPException(502, str(e))
    return [{"id": a["id"], "name": a["name"], "base_url": cloudflare.base_url_for(a["id"])} for a in accts]


@router.get("/{pid}/discover")
async def discover_models(pid: str, user: dict = User):
    """List model ids the provider reports at GET /models."""
    providers = await user_providers(user["_id"])
    p = next((x for x in providers if str(x["_id"]) == pid), None)
    if not p:
        raise HTTPException(404, "Provider not found")
    spec = await resolve(user, pid, (p.get("models") or [{"name": "x"}])[0]["name"])
    try:
        if cloudflare.is_cloudflare(spec.base_url):
            return {"models": await cloudflare.models(spec.base_url, spec.api_key)}
        return {"models": await provider.list_models(spec.base_url, spec.api_key)}
    except provider.LLMError as e:
        raise HTTPException(502, str(e))


@router.post("/{pid}/models/{model:path}/verify")
async def verify_model(pid: str, model: str, user: dict = User):
    """Send a real test prompt and probe tool calling. Stores verified capability."""
    spec = await resolve(user, pid, model)
    try:
        reply = await provider.complete(spec, [{"role": "user", "content": "Reply with the single word: pong"}], timeout=60)
        tools_ok = await provider.probe_tools(spec)
    except provider.LLMError as e:
        raise HTTPException(502, str(e))
    if pid != ENV_PROVIDER_ID:
        await db().providers.update_one({"_id": oid(pid), "user_id": user["_id"], "models.name": model},
                                        {"$set": {"models.$.supports_tools": tools_ok, "models.$.verified_at": now()}})
    return {"ok": True, "reply": reply[:200], "supports_tools": tools_ok}
