"""Cloudflare Workers AI helpers. Chat and embeddings use Cloudflare's OpenAI-compatible endpoint
(https://api.cloudflare.com/client/v4/accounts/<ACCOUNT_ID>/ai/v1); these helpers cover account lookup
and model discovery, which use Cloudflare's own REST API."""
import re
import httpx
from ..config import get_settings
from .provider import LLMError

BASE_RE = re.compile(r"/accounts/([0-9a-f]{32})/ai/v1/?$")
PLACEHOLDER = "/accounts/ACCOUNT_ID/"


def needs_account(base_url: str) -> bool:
    return PLACEHOLDER in base_url


async def fill_account(base_url: str, token: str) -> str:
    """Replace the ACCOUNT_ID placeholder using the account(s) the token can access."""
    if not token:
        raise LLMError("Add your Cloudflare API token to this provider (Edit → API key), then save.")
    accts = await accounts(token)
    if not accts:
        raise LLMError("This Cloudflare token can't access any account. Give it the Workers AI permission.")
    if len(accts) > 1:
        raise LLMError("This token can access several Cloudflare accounts (" + ", ".join(a["name"] for a in accts) +
                       "). Edit the provider and pick one with 'Detect account'.")
    return base_url_for(accts[0]["id"])


def is_cloudflare(base_url: str) -> bool:
    return "/ai/v1" in base_url and "/accounts/" in base_url and (
        "api.cloudflare.com" in base_url or base_url.startswith(get_settings().cloudflare_api_base))


def base_url_for(account_id: str) -> str:
    return f"{get_settings().cloudflare_api_base}/accounts/{account_id}/ai/v1"


async def _get(path: str, token: str, params: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=20) as c:
        try:
            r = await c.get(get_settings().cloudflare_api_base + path, params=params,
                            headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as e:
            raise LLMError(f"Cannot reach Cloudflare: {type(e).__name__}")
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code >= 400 or not data.get("success", False):
        msg = "; ".join(e.get("message", "") for e in data.get("errors", [])) or r.text[:200]
        raise LLMError(f"Cloudflare rejected the request ({r.status_code}): {msg}", r.status_code)
    return data


async def accounts(token: str) -> list[dict]:
    data = await _get("/accounts", token, {"per_page": 50})
    return [{"id": a["id"], "name": a.get("name", a["id"])} for a in data.get("result", [])]


async def models(base_url: str, token: str) -> list[str]:
    """Text-generation and embedding models available to the account."""
    m = BASE_RE.search(base_url)
    if not m:
        raise LLMError("Base URL must look like .../accounts/<account id>/ai/v1")
    names = []
    for task in ("Text Generation", "Text Embeddings"):
        data = await _get(f"/accounts/{m.group(1)}/ai/models/search", token, {"task": task, "per_page": 200})
        names += [x["name"] for x in data.get("result", []) if x.get("name")]
    return sorted(set(names))
