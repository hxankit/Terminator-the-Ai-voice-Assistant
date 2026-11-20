"""Scripted OpenAI-compatible server used ONLY by tests (deterministic stand-in for a real LLM).
Directives in the latest user message:  TOOL:<name> <json-args>  -> model emits that tool call."""
import hashlib, json, re, socket, threading, time
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

app = FastAPI()
REQUESTS: list[dict] = []


def _chunk(delta: dict | None = None, usage=None) -> str:
    body = {"choices": [{"index": 0, "delta": delta}] if delta is not None else []}
    if usage:
        body["usage"] = usage
    return f"data: {json.dumps(body)}\n\n"


def _reply(msgs: list[dict], tools: list | None):
    sys = msgs[0]["content"] if msgs and msgs[0]["role"] == "system" else ""
    if sys.startswith("Classify"):
        return {"text": '{"agent": "general"}'}
    if sys.startswith("You decide what is worth saving"):
        u = msgs[-1]["content"]
        return {"text": '[{"kind":"preference","content":"Prefers concise answers"}]' if "remember" in u.lower() else "[]"}
    last = msgs[-1]
    if last["role"] == "tool":
        return {"text": f"Tool done. Result starts: {last['content'][:200]}"}
    if tools:
        if "get_number" in json.dumps(tools):
            return {"tool": ("get_number", '{"n": 7}')}
        m = re.search(r"TOOL:(\w+) (\{.*\})", last.get("content") or "", re.S)
        if m:
            return {"tool": (m.group(1), m.group(2))}
    if tools and (m := re.search(r"LEAK:(\w+) (\{.*?\})(?=\s*(<context>|$))", last.get("content") or "", re.S)):
        return {"text": f"To answer the question, I will call the `{m.group(1)}` function.\n\n```json\n"
                        f'{{"name": "{m.group(1)}", "parameters": {m.group(2)}}}\n```'}
    if "NUMERIC" in (last.get("content") or ""):
        return {"text": "", "numeric": True}
    if "SLOW" in (last.get("content") or ""):
        return {"text": "word " * 200, "slow": True}
    return {"text": "Hello from mock. pong"}


@app.post("/v1/chat/completions")
async def chat(req: Request):
    body = await req.json()
    REQUESTS.append(body)
    if "STALL" in json.dumps(body["messages"][-1]):      # simulates a slow CPU model: no response for 3 s
        import asyncio
        await asyncio.sleep(3)
    return await _respond(body)


async def _respond(body: dict):
    r = _reply(body["messages"], body.get("tools"))

    async def gen():
        import asyncio
        if "tool" in r:
            name, args = r["tool"]
            yield _chunk({"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": name, "arguments": ""}}]})
            yield _chunk({"tool_calls": [{"index": 0, "function": {"arguments": args}}]})
        elif r.get("numeric"):                                # Cloudflare-style: numeric tokens as JSON numbers
            for piece in ["The answer is ", 42, "."]:
                yield _chunk({"content": piece})
        else:
            for w in re.findall(r"\S+\s*", r["text"]):
                yield _chunk({"content": w})
                if r.get("slow"):
                    await asyncio.sleep(0.02)
        yield _chunk(None, {"prompt_tokens": 11, "completion_tokens": 5})
        yield "data: [DONE]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


# ---- Cloudflare-style API (account lookup, model search, strict OpenAI-compatible endpoint) ----
CF_TOKEN, CF_ACCOUNT = "cf-test-token", "0123456789abcdef0123456789abcdef"
from fastapi.responses import JSONResponse


def _cf_auth(req: Request) -> bool:
    return req.headers.get("authorization") == f"Bearer {CF_TOKEN}"


@app.get("/client/v4/accounts")
async def cf_accounts(req: Request):
    if not _cf_auth(req):
        return JSONResponse({"success": False, "errors": [{"message": "Invalid API Token"}]}, status_code=403)
    return {"success": True, "result": [{"id": CF_ACCOUNT, "name": "Test account"}]}


@app.get("/client/v4/accounts/{acct}/ai/models/search")
async def cf_models(acct: str, req: Request, task: str = ""):
    if not _cf_auth(req):
        return JSONResponse({"success": False, "errors": [{"message": "Invalid API Token"}]}, status_code=403)
    names = {"Text Generation": ["@cf/meta/llama-3.3-70b-instruct-fp8-fast", "@cf/meta/llama-3.1-8b-instruct-fast"],
             "Text Embeddings": ["@cf/baai/bge-m3"]}.get(task, [])
    return {"success": True, "result": [{"name": n} for n in names]}


@app.post("/client/v4/accounts/{acct}/ai/v1/chat/completions")
async def cf_chat(acct: str, req: Request):
    body = await req.json()
    REQUESTS.append(body)
    if not _cf_auth(req):
        return JSONResponse({"errors": [{"message": "Authentication error"}]}, status_code=401)
    if "stream_options" in body:                                  # strict endpoint: rejects optional fields
        return JSONResponse({"errors": [{"message": "Unrecognized key: stream_options"}]}, status_code=400)
    return await _respond(body)


# ---- fake Microsoft Graph ----
GRAPH: list[tuple[str, str, dict]] = []
PEOPLE = {"arjun": [{"id": "u-arjun", "displayName": "Arjun Mehta", "personType": {"class": "Person"},
                        "scoredEmailAddresses": [{"address": "arjun@contoso.com"}]}],
          "priya": [{"id": "u-p1", "displayName": "Priya Nair", "personType": {"class": "Person"},
                     "scoredEmailAddresses": [{"address": "priya.nair@contoso.com"}]},
                    {"id": "u-p2", "displayName": "Priya Shah", "personType": {"class": "Person"},
                     "scoredEmailAddresses": [{"address": "priya.shah@contoso.com"}]}]}


def _graph_ok(req: Request) -> bool:
    return req.headers.get("authorization") == "Bearer test-token"


@app.api_route("/v1.0/{path:path}", methods=["GET", "POST"])
async def graph(path: str, req: Request):
    if not _graph_ok(req):
        return JSONResponse({"error": {"code": "InvalidAuthenticationToken", "message": "Access token is empty."}}, status_code=401)
    body = await req.json() if req.method == "POST" else {}
    GRAPH.append((req.method, "/" + path, body))
    q = req.query_params
    if path == "me":
        return {"id": "me-id", "displayName": "Sam Carter", "mail": "sam@contoso.com"}
    if path == "me/people":
        key = q.get("$search", "").strip('"').lower().split()[0] if q.get("$search") else ""
        return {"value": PEOPLE.get(key, [])}
    if path == "users":
        return {"value": []}
    if path == "chats" and req.method == "POST":
        return JSONResponse({"id": "chat-1"}, status_code=201)
    if path.startswith("chats/") and path.endswith("/messages"):
        return JSONResponse({"id": "msg-1"}, status_code=201)
    if path == "me/chats":
        return {"value": [{"id": "chat-1", "chatType": "oneOnOne", "topic": None, "members": [{"displayName": "Arjun Mehta"}],
                           "lastMessagePreview": {"createdDateTime": "2026-11-20T10:00:00Z", "body": {"content": "<p>see you at 5</p>"},
                                                  "from": {"user": {"displayName": "Arjun Mehta"}}}}]}
    if path == "me/sendMail":
        return JSONResponse(None, status_code=202)
    if path == "me/messages":
        return {"value": [{"subject": "Build failed", "from": {"emailAddress": {"name": "CI Bot"}}, "isRead": False,
                           "receivedDateTime": "2026-11-20T09:00:00Z", "bodyPreview": "Pipeline #42 failed"}]}
    if path == "me/calendarView":
        return {"value": [{"subject": "Standup", "start": {"dateTime": "2026-11-20T04:30:00.0000000"},
                           "end": {"dateTime": "2026-11-20T04:45:00.0000000"}, "location": {"displayName": "Teams"},
                           "organizer": {"emailAddress": {"name": "Sam Carter"}}, "isOnlineMeeting": True},
                          {"subject": "Release review", "start": {"dateTime": "2026-11-20T09:30:00.0000000"},
                           "end": {"dateTime": "2026-11-20T10:30:00.0000000"}, "location": {}, "organizer": {}}]}
    return JSONResponse({"error": {"message": f"no mock for {path}"}}, status_code=404)


# ---- fake WhatsApp bridge (whatsapp-mcp's POST /api/send) ----
WA_SENT: list[dict] = []


@app.post("/api/send")
async def wa_send(req: Request):
    body = await req.json()
    WA_SENT.append(body)
    return {"success": True, "message": f"Message sent to {body['recipient']}"}


def _vec(text: str) -> list[float]:
    v = [0.0] * 64
    for w in re.findall(r"\w+", text.lower()):
        v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 64] += 1
    return v


@app.get("/w/api.php")                                   # Wikipedia search
async def wiki_search(srsearch: str = ""):
    return {"query": {"search": [{"title": "Ada Lovelace"}] if "lovelace" in srsearch.lower() else []}}


@app.get("/api/rest_v1/page/summary/{title}")              # Wikipedia page summary
async def wiki_summary(title: str):
    return {"title": title.replace("_", " "), "extract": "Ada Lovelace was an English mathematician (1815-1852).",
            "content_urls": {"desktop": {"page": f"https://en.wikipedia.org/wiki/{title}"}}}


@app.post("/v1/embeddings")
async def embeddings(req: Request):
    body = await req.json()
    return {"data": [{"index": i, "embedding": _vec(t)} for i, t in enumerate(body["input"])]}


@app.get("/v1/models")
async def models():
    return {"data": [{"id": "mock-1"}, {"id": "mock-embed"}]}


def start() -> str:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    return f"http://127.0.0.1:{port}/v1"
