"""Resolve which provider/model to use for a user. Providers are stored per user with encrypted keys."""
from fastapi import HTTPException
from ..config import get_settings
from ..db import db, oid
from ..security import decrypt
from . import cloudflare
from .provider import LLMError, ModelSpec

ENV_PROVIDER_ID = "env-default"


def env_provider() -> dict | None:
    s = get_settings()
    if not s.default_provider_base_url:
        return None
    return {"_id": ENV_PROVIDER_ID, "name": "Default (env)", "base_url": s.default_provider_base_url,
            "models": [{"name": s.default_model, "supports_tools": None}] if s.default_model else [],
            "embedding_model": s.default_embedding_model, "readonly": True}


async def user_providers(user_id) -> list[dict]:
    items = [p async for p in db().providers.find({"user_id": user_id})]
    env = env_provider()
    return ([env] if env else []) + items


async def resolve(user: dict, provider_id: str | None = None, model: str | None = None) -> ModelSpec:
    providers = await user_providers(user["_id"])
    if not providers:
        raise HTTPException(400, "No LLM provider configured. Add one in Settings → Models "
                                 "(e.g. Ollama at http://localhost:11434/v1).")
    pid = provider_id or (user.get("prefs") or {}).get("provider_id")
    prov = next((p for p in providers if str(p["_id"]) == str(pid)), None) or providers[0]
    models = prov.get("models") or []
    name = model or (user.get("prefs") or {}).get("model") or (models[0]["name"] if models else None)
    if not name:
        raise HTTPException(400, f"Provider '{prov['name']}' has no model configured.")
    mcfg = next((m for m in models if m["name"] == name), {"name": name})
    key = get_settings().default_provider_api_key if prov["_id"] == ENV_PROVIDER_ID else decrypt(prov.get("api_key_enc", ""))
    if cloudflare.needs_account(prov["base_url"]):
        try:
            prov["base_url"] = await cloudflare.fill_account(prov["base_url"], key)
        except LLMError as e:
            raise HTTPException(400, f"Cloudflare provider '{prov['name']}': {e}")
        if prov["_id"] != ENV_PROVIDER_ID:
            await db().providers.update_one({"_id": prov["_id"]}, {"$set": {"base_url": prov["base_url"]}})
    return ModelSpec(provider_id=str(prov["_id"]), base_url=prov["base_url"], api_key=key, model=name,
                     temperature=mcfg.get("temperature", 0.7), max_tokens=mcfg.get("max_tokens"),
                     context_window=mcfg.get("context_window", 8192), supports_tools=mcfg.get("supports_tools"),
                     extra={"embedding_model": prov.get("embedding_model") or ""})


async def fallback_for(user: dict, current: ModelSpec) -> ModelSpec | None:
    """Model to use when `current` fails: the user's chosen fallback, else another provider (local first)."""
    prefs = user.get("prefs") or {}
    if prefs.get("fallback_provider_id") and prefs.get("fallback_model"):
        try:
            spec = await resolve(user, prefs["fallback_provider_id"], prefs["fallback_model"])
            return None if (spec.provider_id, spec.model) == (current.provider_id, current.model) else spec
        except HTTPException:
            return None
    from .provider import is_local
    others = [p for p in await user_providers(user["_id"]) if str(p["_id"]) != current.provider_id and p.get("models")]
    others.sort(key=lambda p: not is_local(p["base_url"]))           # prefer a local model as the safety net
    for p in others:
        try:
            return await resolve(user, str(p["_id"]), p["models"][0]["name"])
        except HTTPException:
            continue
    return None


async def embedding_spec(user: dict) -> tuple[ModelSpec, str] | None:
    """Return (spec, embedding_model) for the first provider with an embedding model, or None."""
    for prov in await user_providers(user["_id"]):
        if prov.get("embedding_model"):
            key = get_settings().default_provider_api_key if prov["_id"] == ENV_PROVIDER_ID else decrypt(prov.get("api_key_enc", ""))
            return ModelSpec(str(prov["_id"]), prov["base_url"], key, ""), prov["embedding_model"]
    return None
