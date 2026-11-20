"""OpenAI-compatible LLM client (works with Ollama /v1, OpenAI, OpenRouter, vLLM, LM Studio, Groq, ...).

Streams normalized events:
  {"type": "text", "delta": str}
  {"type": "tool_calls", "calls": [{"id", "name", "arguments"(str)}]}
  {"type": "usage", "prompt_tokens", "completion_tokens"}
"""
import asyncio, json, logging
from dataclasses import dataclass, field
from typing import AsyncIterator
from urllib.parse import urlparse
import httpx

log = logging.getLogger("llm")
RETRY_STATUS = {408, 429, 500, 502, 503, 504}
LOCAL_READ_TIMEOUT = 900.0     # a CPU-only model can need minutes before the first token of a long prompt
REMOTE_READ_TIMEOUT = 180.0


def is_local(base_url: str) -> bool:
    host = (urlparse(base_url).hostname or "").lower()
    return host in ("localhost", "::1") or host.startswith("127.") or host.endswith(".local")


def default_timeout(spec: "ModelSpec") -> float:
    return LOCAL_READ_TIMEOUT if is_local(spec.base_url) else REMOTE_READ_TIMEOUT


class LLMError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class ModelSpec:
    provider_id: str
    base_url: str
    api_key: str
    model: str
    temperature: float = 0.7
    max_tokens: int | None = None
    context_window: int = 8192
    supports_tools: bool | None = None   # None = unknown / unverified
    extra: dict = field(default_factory=dict)


def _headers(spec: ModelSpec) -> dict:
    h = {"Content-Type": "application/json"}
    if spec.api_key:
        h["Authorization"] = f"Bearer {spec.api_key}"
    return h


def _url(spec: ModelSpec, path: str) -> str:
    return spec.base_url.rstrip("/") + path


def _provider_message(body: str) -> str:
    """Pull the human-readable message out of an OpenAI-style / Ollama / generic error body."""
    try:
        data = json.loads(body)
        if isinstance(data, dict) and isinstance(data.get("errors"), list) and data["errors"]:   # Cloudflare style
            return "; ".join(str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in data["errors"])[:300]
        err = data.get("error", data.get("detail", data)) if isinstance(data, dict) else data
        msg = err.get("message", err) if isinstance(err, dict) else err
        return str(msg)[:300]
    except (json.JSONDecodeError, AttributeError):
        return body.strip()[:300]


def _explain(status: int, body: str) -> str:
    detail = _provider_message(body)
    if status == 401:
        return f"Provider rejected the API key (401): {detail}. Check the key in Settings → Models."
    if status == 404:
        hint = " If this is Ollama, download it first: ollama pull <model>." if "not found" in detail.lower() else ""
        return f"Not found (404): {detail}.{hint} Check the base URL and model name."
    if status == 429:
        return f"Provider rate limit reached (429): {detail}. Try again shortly."
    return f"Provider error {status}: {detail}"


async def _send(client: httpx.AsyncClient, method: str, url: str, *, retries: int = 2, **kw) -> httpx.Response:
    """Send with retries on connection failures / transient statuses (before any streaming starts).
    A read timeout is NOT retried: the server was working on it, and a retry would start the whole
    (possibly minutes-long) prompt evaluation again from scratch."""
    for attempt in range(retries + 1):
        try:
            req = client.build_request(method, url, **kw)
            resp = await client.send(req, stream=True)
            if resp.status_code in RETRY_STATUS and attempt < retries:
                await resp.aclose()
                await asyncio.sleep(0.5 * 2 ** attempt)
                continue
            if resp.status_code >= 400:
                body = (await resp.aread()).decode(errors="replace")
                await resp.aclose()
                raise LLMError(_explain(resp.status_code, body), resp.status_code)
            return resp
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            if attempt < retries:
                await asyncio.sleep(0.5 * 2 ** attempt)
                continue
            raise LLMError(f"Cannot reach provider at {url}: {type(e).__name__}. Is it running?")
        except httpx.ReadTimeout:
            raise LLMError("The model didn't start answering in time. A local model on CPU can need several "
                           "minutes for a long first prompt; try again (the prompt is cached by then) or use a faster model.")
    raise LLMError("unreachable")


async def stream_chat(spec: ModelSpec, messages: list[dict], tools: list[dict] | None = None,
                      timeout: float | None = None) -> AsyncIterator[dict]:
    timeout = timeout or default_timeout(spec)
    payload: dict = {"model": spec.model, "messages": messages, "stream": True,
                     "temperature": spec.temperature, "stream_options": {"include_usage": True}}
    if spec.max_tokens:
        payload["max_tokens"] = spec.max_tokens
    if tools and spec.supports_tools is not False:
        payload["tools"] = tools
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10)) as client:
        try:
            resp = await _send(client, "POST", _url(spec, "/chat/completions"), json=payload, headers=_headers(spec))
        except LLMError as e:
            if e.status not in (400, 422) or "stream_options" not in payload:
                raise
            # some OpenAI-compatible endpoints (e.g. Cloudflare Workers AI) reject optional fields: retry without
            payload.pop("stream_options")
            resp = await _send(client, "POST", _url(spec, "/chat/completions"), json=payload, headers=_headers(spec))
        calls: dict[int, dict] = {}
        try:
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if chunk.get("error"):
                    raise LLMError(str(chunk["error"]))
                if chunk.get("usage"):
                    u = chunk["usage"]
                    yield {"type": "usage", "prompt_tokens": u.get("prompt_tokens", 0),
                           "completion_tokens": u.get("completion_tokens", 0)}
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    content = delta.get("content")
                    if content is not None and content != "":
                        # some providers (Cloudflare Workers AI) send numeric tokens as JSON numbers
                        yield {"type": "text", "delta": content if isinstance(content, str) else
                               json.dumps(content) if isinstance(content, (dict, list)) else str(content)}
                    for tc in delta.get("tool_calls") or []:
                        slot = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "arguments": ""})
                        slot["id"] = tc.get("id") or slot["id"]
                        fn = tc.get("function") or {}
                        slot["name"] += str(fn.get("name") or "")
                        args = fn.get("arguments")
                        slot["arguments"] += args if isinstance(args, str) else json.dumps(args) if args else ""
        except httpx.ReadTimeout:
            raise LLMError("The model stopped sending output (timed out). Try again or use a faster model.")
        finally:
            await resp.aclose()
        if calls:
            out = []
            for i, c in sorted(calls.items()):
                out.append({"id": c["id"] or f"call_{i}", "name": c["name"], "arguments": c["arguments"] or "{}"})
            yield {"type": "tool_calls", "calls": out}


async def complete(spec: ModelSpec, messages: list[dict], **kw) -> str:
    """Non-streaming convenience wrapper (used for routing / memory extraction)."""
    parts = []
    async for ev in stream_chat(spec, messages, **kw):
        if ev["type"] == "text":
            parts.append(ev["delta"])
    return "".join(parts)


async def embed(spec: ModelSpec, texts: list[str], model: str) -> list[list[float]]:
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await _send(client, "POST", _url(spec, "/embeddings"),
                           json={"model": model, "input": texts}, headers=_headers(spec))
        data = json.loads(await resp.aread())
        await resp.aclose()
    return [d["embedding"] for d in sorted(data["data"], key=lambda d: d["index"])]


async def list_models(base_url: str, api_key: str) -> list[str]:
    spec = ModelSpec("", base_url, api_key, "")
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await _send(client, "GET", _url(spec, "/models"), headers=_headers(spec), retries=0)
        data = json.loads(await resp.aread())
        await resp.aclose()
    return sorted(m["id"] for m in data.get("data", []))


async def probe_tools(spec: ModelSpec) -> bool:
    """Verify tool calling empirically: force a trivial call and check a tool_call comes back."""
    tool = {"type": "function", "function": {"name": "get_number", "description": "Returns a number",
            "parameters": {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}}}
    probe = ModelSpec(**{**spec.__dict__, "supports_tools": None, "temperature": 0})
    try:
        async for ev in stream_chat(probe, [{"role": "user", "content": "Call get_number with n=7. Do not answer in text."}],
                                    tools=[tool], timeout=60):
            if ev["type"] == "tool_calls":
                return any(c["name"] == "get_number" for c in ev["calls"])
    except LLMError as e:
        if e.status == 400:
            return False
        raise
    return False
