"""Web search (SearxNG or Tavily) and safe page fetching with SSRF protection."""
import asyncio, ipaddress, socket
from urllib.parse import urlparse
import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field
from ..config import get_settings
from .base import tool, ToolError, ToolContext


def _search_unavailable():
    s = get_settings()
    return None if (s.searxng_url or s.tavily_api_key) else "set SEARXNG_URL or TAVILY_API_KEY"


async def _assert_public(url: str):
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ToolError("Only http(s) URLs are allowed")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(p.hostname, None)
    except socket.gaierror:
        raise ToolError(f"Cannot resolve host {p.hostname}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ToolError("Refusing to fetch private/internal network addresses")


async def search(query: str, n: int = 6) -> list[dict]:
    s = get_settings()
    async with httpx.AsyncClient(timeout=20) as c:
        if s.tavily_api_key:
            r = await c.post("https://api.tavily.com/search", json={"api_key": s.tavily_api_key, "query": query, "max_results": n})
            r.raise_for_status()
            return [{"title": x["title"], "url": x["url"], "snippet": x.get("content", "")[:500]} for x in r.json()["results"]]
        r = await c.get(s.searxng_url.rstrip("/") + "/search", params={"q": query, "format": "json"})
        r.raise_for_status()
        return [{"title": x.get("title"), "url": x.get("url"), "snippet": (x.get("content") or "")[:500],
                 "date": x.get("publishedDate")} for x in r.json().get("results", [])[:n]]


async def fetch(url: str, max_chars: int = 15000) -> dict:
    current = url
    async with httpx.AsyncClient(timeout=20, follow_redirects=False,
                                 headers={"User-Agent": "TerminatorAssistant/0.1"}) as c:
        for _ in range(5):  # follow redirects manually so every hop is SSRF-checked
            await _assert_public(current)
            r = await c.get(current)
            if r.is_redirect:
                current = str(r.next_request.url) if r.next_request else current
                continue
            break
    r.raise_for_status()
    ctype = r.headers.get("content-type", "")
    if "html" in ctype:
        soup = BeautifulSoup(r.text, "html.parser")
        for t in soup(["script", "style", "nav", "footer", "noscript", "svg"]):
            t.decompose()
        title = soup.title.string.strip() if soup.title and soup.title.string else ""
        text = "\n".join(l.strip() for l in soup.get_text("\n").splitlines() if l.strip())
    elif ctype.startswith("text/") or "json" in ctype:
        title, text = "", r.text
    else:
        raise ToolError(f"Unsupported content type: {ctype}")
    return {"url": current, "title": title, "text": text[:max_chars]}


class SearchArgs(BaseModel):
    query: str = Field(min_length=2, max_length=400)
    max_results: int = Field(6, ge=1, le=10)


class FetchArgs(BaseModel):
    url: str = Field(max_length=2000)


@tool("web_search", "Search the web for current information. Returns titles, URLs and snippets. "
      "Follow up with fetch_url on the most relevant official sources before answering.", SearchArgs,
      requires=_search_unavailable)
async def web_search(a: SearchArgs, ctx: ToolContext):
    return await search(a.query, a.max_results)


@tool("fetch_url", "Fetch a public web page and return its readable text.", FetchArgs)
async def fetch_url(a: FetchArgs, ctx: ToolContext):
    try:
        return await fetch(a.url)
    except httpx.HTTPError as e:
        raise ToolError(f"Fetch failed: {e}")


class WikiArgs(BaseModel):
    query: str = Field(min_length=2, max_length=200, description="Topic, person, place or thing")


UA = {"User-Agent": "TerminatorAssistant/0.1 (self-hosted personal assistant)"}   # required by Wikimedia API policy


@tool("wikipedia", "Look up well-known people, places, things and topics on Wikipedia (works without web search). "
      "Returns short summaries with links; cite them.", WikiArgs)
async def wikipedia(a: WikiArgs, ctx: ToolContext):
    from urllib.parse import quote
    base = get_settings().wikipedia_base.rstrip("/")
    async with httpx.AsyncClient(timeout=10, headers=UA) as c:
        try:
            r = await c.get(f"{base}/w/api.php", params={"action": "query", "list": "search", "srsearch": a.query,
                                                         "srlimit": 3, "format": "json"})
            r.raise_for_status()
            titles = [x["title"] for x in r.json().get("query", {}).get("search", [])]
            out = []
            for t in titles:
                s = await c.get(f"{base}/api/rest_v1/page/summary/{quote(t.replace(' ', '_'), safe='')}")
                if s.status_code == 200:
                    j = s.json()
                    out.append({"title": j.get("title", t), "summary": (j.get("extract") or "")[:1500],
                                "url": (j.get("content_urls") or {}).get("desktop", {}).get("page")})
        except (httpx.HTTPError, ValueError) as e:
            raise ToolError(f"Wikipedia lookup failed: {e}")
    return out or f"No Wikipedia article found for '{a.query}'."
