import io, json
from pymongo import MongoClient
from tests.conftest import make_user
from tests import mock_llm

DB = MongoClient("mongodb://localhost:27017")["terminator_test"]


def sse(resp) -> list[dict]:
    out = []
    for block in resp.text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
    return out


def new_conv(client, h):
    return client.post("/api/conversations", headers=h, json={}).json()["id"]


def chat(client, h, cid, **body):
    r = client.post(f"/api/conversations/{cid}/chat", headers=h, json=body)
    assert r.status_code == 200, r.text
    return sse(r)


# ---------- auth & security ----------
def test_auth_flow(client):
    assert client.get("/api/auth/me").status_code == 401
    h = make_user(client, "bob@example.com")
    assert client.get("/api/auth/me", headers=h).json()["email"] == "bob@example.com"
    assert client.post("/api/auth/register", json={"email": "bob@example.com", "password": "correct-horse-1"}).status_code == 409
    assert client.post("/api/auth/login", json={"email": "bob@example.com", "password": "wrong"}).status_code == 401
    assert client.post("/api/auth/login", json={"email": "bob@example.com", "password": "correct-horse-1"}).status_code == 200
    assert client.post("/api/auth/register", json={"email": "x@y.z", "password": "short"}).status_code == 422
    u = DB.users.find_one({"email": "bob@example.com"})
    assert u["password_hash"].startswith("$2") and "correct-horse" not in str(u)


def test_api_key_encrypted_and_hidden(client, auth):
    provs = client.get("/api/providers", headers=auth).json()
    assert "sk-test-secret-123" not in json.dumps(provs)
    assert provs[0]["has_api_key"] is True
    assert "sk-test-secret-123" not in str(DB.providers.find_one())


def test_user_isolation(client, auth):
    cid = new_conv(client, auth)
    other = make_user(client, "eve@example.com")
    assert client.get(f"/api/conversations/{cid}", headers=other).status_code == 404
    assert client.get("/api/providers", headers=other).json() == []


# ---------- models ----------
def test_discover_and_verify(client, auth):
    pid = client.get("/api/providers", headers=auth).json()[0]["id"]
    assert "mock-1" in client.get(f"/api/providers/{pid}/discover", headers=auth).json()["models"]
    r = client.post(f"/api/providers/{pid}/models/mock-1/verify", headers=auth).json()
    assert r["supports_tools"] is True and "pong" in r["reply"]


def test_no_provider_error(client):
    h = make_user(client, "noprov@example.com")
    cid = new_conv(client, h)
    r = client.post(f"/api/conversations/{cid}/chat", headers=h, json={"message": "hi"})
    assert r.status_code == 400 and "No LLM provider" in r.json()["detail"]


def test_unreachable_provider(client):
    h = make_user(client, "down@example.com")
    client.post("/api/providers", headers=h, json={"name": "Down", "base_url": "http://127.0.0.1:9/v1", "models": [{"name": "m"}]})
    evs = chat(client, h, new_conv(client, h), message="hello there friend")
    assert evs[-1]["type"] == "error" and "Cannot reach provider" in evs[-1]["message"]


# ---------- chat & streaming ----------
def test_streaming_chat_persists(client, auth):
    cid = new_conv(client, auth)
    evs = chat(client, auth, cid, message="Say hello")
    types = [e["type"] for e in evs]
    assert types[0] == "meta" and "text" in types and types[-1] == "done"
    assert "".join(e["delta"] for e in evs if e["type"] == "text") == "Hello from mock. pong"
    conv = client.get(f"/api/conversations/{cid}", headers=auth).json()
    assert conv["title"] == "Say hello"
    assert [m["role"] for m in conv["messages"]] == ["user", "assistant"]
    assert DB.usage.count_documents({"model": "mock-1"}) >= 1


def test_history_sent_as_context(client, auth):
    cid = new_conv(client, auth)
    chat(client, auth, cid, message="My first message here")
    chat(client, auth, cid, message="Second one")
    msgs = [r for r in mock_llm.REQUESTS if r.get("tools")][-1]["messages"]
    assert any(m["content"] == "My first message here" for m in msgs)


def test_regenerate(client, auth):
    cid = new_conv(client, auth)
    chat(client, auth, cid, message="Hello")
    chat(client, auth, cid, mode="regenerate")
    roles = [m["role"] for m in client.get(f"/api/conversations/{cid}", headers=auth).json()["messages"]]
    assert roles == ["user", "assistant"]


def test_search_conversations(client, auth):
    cid = new_conv(client, auth)
    chat(client, auth, cid, message="unique-zebra-token question")
    items = client.get("/api/conversations?q=zebra-token", headers=auth).json()["items"]
    assert any(i["id"] == cid for i in items)


# ---------- routing ----------
def test_keyword_routing(client, auth):
    cid = new_conv(client, auth)
    evs = chat(client, auth, cid, message="nginx returns 502 on my ubuntu server after deploy")
    assert evs[0]["agent"] == "sysadmin"
    evs = chat(client, auth, new_conv(client, auth), message="fix this typescript function bug in my react code")
    assert evs[0]["agent"] == "dev"


def test_forced_agent(client, auth):
    evs = chat(client, auth, new_conv(client, auth), message="hi", agent="writing")
    assert evs[0]["agent"] == "writing" and evs[0]["route"] == "user"


# ---------- tools ----------
def test_tool_call_creates_task(client, auth):
    cid = new_conv(client, auth)
    evs = chat(client, auth, cid, agent="productivity",
               message='TOOL:task_create {"title": "Renew SSL cert", "due_at": "2030-01-01T09:00:00+00:00"}')
    end = next(e for e in evs if e["type"] == "tool_end")
    assert end["status"] == "ok"
    assert DB.tasks.find_one({"title": "Renew SSL cert"})
    assert evs[-1]["type"] == "done"
    assert "Tool done" in "".join(e.get("delta", "") for e in evs)


def test_invalid_tool_args_rejected(client, auth):
    evs = chat(client, auth, new_conv(client, auth), agent="productivity", message='TOOL:task_create {"nope": 1}')
    assert next(e for e in evs if e["type"] == "tool_end")["status"] == "error"


def test_tool_not_in_agent_scope(client, auth):
    evs = chat(client, auth, new_conv(client, auth), agent="writing", message='TOOL:run_command {"command": "uptime", "reason": "x"}')
    assert not any(e["type"] == "approval_required" for e in evs)
    assert not DB.tool_runs.find_one({"tool": "run_command", "args.reason": "x"})


def test_approval_flow(client, auth):
    client.patch("/api/auth/prefs", headers=auth, json={"approval_mode": "ask"})
    try:
        _test_approval_flow(client, auth)
    finally:
        client.patch("/api/auth/prefs", headers=auth, json={"approval_mode": "risky"})


def _test_approval_flow(client, auth):
    cid = new_conv(client, auth)
    evs = chat(client, auth, cid, agent="sysadmin", message='TOOL:run_command {"command": "uptime", "reason": "check load"}')
    appr = next(e for e in evs if e["type"] == "approval_required")
    assert evs[-1].get("awaiting_approval")
    # cannot send new messages while approval pending
    assert client.post(f"/api/conversations/{cid}/chat", headers=auth, json={"message": "x"}).status_code == 409
    r = client.post(f"/api/tools/runs/{appr['run_id']}/decision", headers=auth, json={"approve": True}).json()
    assert r["status"] == "ok" and r["pending_remaining"] == 0
    evs = chat(client, auth, cid, mode="continue", agent="sysadmin")
    assert "load average" in "".join(e.get("delta", "") for e in evs)


def test_denied_and_blocked_commands(client, auth):
    client.patch("/api/auth/prefs", headers=auth, json={"approval_mode": "ask"})
    try:
        _test_denied_and_blocked_commands(client, auth)
    finally:
        client.patch("/api/auth/prefs", headers=auth, json={"approval_mode": "risky"})


def _test_denied_and_blocked_commands(client, auth):
    cid = new_conv(client, auth)
    evs = chat(client, auth, cid, agent="sysadmin", message='TOOL:run_command {"command": "rm -rf /tmp/x", "reason": "cleanup"}')
    rid = next(e for e in evs if e["type"] == "approval_required")["run_id"]
    r = client.post(f"/api/tools/runs/{rid}/decision", headers=auth, json={"approve": True}).json()
    assert r["status"] == "error"           # not on allow-list, even when approved
    cid2 = new_conv(client, auth)
    evs = chat(client, auth, cid2, agent="sysadmin", message='TOOL:run_command {"command": "uptime", "reason": "x"}')
    rid = next(e for e in evs if e["type"] == "approval_required")["run_id"]
    assert client.post(f"/api/tools/runs/{rid}/decision", headers=auth, json={"approve": False}).json()["status"] == "denied"
    assert DB.tool_runs.find_one({"_id": __import__("bson").ObjectId(rid)})["status"] == "denied"


def test_sandbox_python(client, auth):
    code = ("import pandas as pd, socket\nprint(pd.DataFrame({'a':[1,2,3]}).a.sum())\n"
            "open('out/r.txt','w').write('hi')\n"
            "try:\n socket.create_connection(('1.1.1.1',80),2); print('NET-OPEN')\nexcept OSError: print('NET-BLOCKED')\n"
            "import os; print('HOME-VISIBLE' if os.path.exists('/home') and os.listdir('/home') else 'HOME-HIDDEN')")
    evs = chat(client, auth, new_conv(client, auth), agent="dev", message="TOOL:run_python " + json.dumps({"code": code}))
    out = next(e for e in evs if e["type"] == "tool_end")
    assert out["status"] == "ok", out
    assert "6" in out["preview"] and "NET-BLOCKED" in out["preview"] and "HOME-HIDDEN" in out["preview"]
    f = DB.generated_files.find_one({"filename": "r.txt"})
    assert client.get(f"/api/files/{f['_id']}", headers=auth).content == b"hi"


def test_ssrf_blocked(client, auth):
    evs = chat(client, auth, new_conv(client, auth), agent="research", message='TOOL:fetch_url {"url": "http://127.0.0.1:27017"}')
    end = next(e for e in evs if e["type"] == "tool_end")
    assert end["status"] == "error" and "private" in end["preview"]


def test_tool_output_marked_untrusted(client, auth):
    cid = new_conv(client, auth)
    chat(client, auth, cid, agent="productivity", message='TOOL:task_list {"status": "all"}')
    tool_msg = DB.tool_runs.database.messages.find_one({"name": "task_list"})
    assert 'trust="untrusted"' in tool_msg["content"]


def test_excel_generation(client, auth):
    args = {"filename": "rep", "sheets": [{"name": "S", "columns": ["a", "b"], "rows": [[1, 2], [3, 4]]}]}
    evs = chat(client, auth, new_conv(client, auth), agent="data", message="TOOL:create_excel " + json.dumps(args))
    data = json.loads(next(e for e in evs if e["type"] == "tool_end")["preview"])
    r = client.get(data["download_url"], headers=auth)
    assert r.status_code == 200 and r.content[:2] == b"PK"
    from openpyxl import load_workbook
    assert load_workbook(io.BytesIO(r.content))["S"]["B3"].value == 4


# ---------- memory ----------
def test_memory_crud_and_injection(client, auth):
    r = client.post("/api/memories", headers=auth, json={"content": "User deploys on Ubuntu 24.04 with PM2", "kind": "long_term"})
    mid = r.json()["id"]
    client.post("/api/memories", headers=auth, json={"content": "Answer in British English", "kind": "preference"})
    assert client.post("/api/memories", headers=auth, json={"content": "my key is sk-abcdefghijklmnopqrstuvwx"}).status_code == 422
    chat(client, auth, new_conv(client, auth), message="How should I deploy with PM2 on Ubuntu?")
    msgs = [r for r in mock_llm.REQUESTS if r.get("tools")][-1]["messages"]
    last_user = [m for m in msgs if m["role"] == "user"][-1]["content"]
    assert "Ubuntu 24.04 with PM2" in last_user and "British English" in last_user
    assert "Ubuntu 24.04" not in msgs[0]["content"]          # system prompt stays stable for prompt caching
    assert client.put(f"/api/memories/{mid}", headers=auth, json={"content": "Deploys with Docker", "kind": "long_term"}).status_code == 200
    assert client.delete(f"/api/memories/{mid}", headers=auth).status_code == 200
    assert not any(m["id"] == mid for m in client.get("/api/memories", headers=auth).json())


def test_auto_memory_is_selective(client, auth):
    import time
    before = DB.memories.count_documents({"source": "auto"})
    chat(client, auth, new_conv(client, auth), message="what is 2+2")
    time.sleep(0.5)
    assert DB.memories.count_documents({"source": "auto"}) == before
    chat(client, auth, new_conv(client, auth), message="Please remember I like short replies")
    for _ in range(20):
        if DB.memories.count_documents({"source": "auto"}) > before:
            break
        time.sleep(0.1)
    assert DB.memories.find_one({"source": "auto", "content": "Prefers concise answers"})


# ---------- RAG ----------
def test_rag_upload_retrieve_and_cite(client, auth):
    txt = ("Terminator onboarding guide.\n\nThe VPN password rotation happens every 90 days.\n\n"
           "Lunch is served at noon in building C.") * 3
    r = client.post("/api/documents", headers=auth, files={"file": ("guide.txt", txt.encode(), "text/plain")})
    doc = r.json()
    assert doc["status"] == "ready" and doc["retrieval"] == "vector", doc
    hits = client.post("/api/kb/search", headers=auth, json={"query": "VPN password rotation"}).json()
    assert hits and "90 days" in hits[0]["text"] and hits[0]["filename"] == "guide.txt"
    evs = chat(client, auth, new_conv(client, auth), message="How often is VPN rotation?", use_kb=True)
    cites = next(e for e in evs if e["type"] == "citations")["items"]
    assert any(c["title"] == "guide.txt" for c in cites)
    msgs = [r for r in mock_llm.REQUESTS if r.get("tools")][-1]["messages"]
    assert "90 days" in [m for m in msgs if m["role"] == "user"][-1]["content"]


def test_rag_csv_and_profile(client, auth):
    csv = "city,sales\nParis,10\nLyon,30\nNice,20\n"
    doc = client.post("/api/documents", headers=auth, files={"file": ("s.csv", csv.encode(), "text/csv")}).json()
    evs = chat(client, auth, new_conv(client, auth), agent="data",
               message="TOOL:profile_table " + json.dumps({"document_id": doc["id"]}))
    prev = next(e for e in evs if e["type"] == "tool_end")["preview"]
    assert '"rows": 3' in prev


def test_upload_validation(client, auth):
    assert client.post("/api/documents", headers=auth, files={"file": ("x.exe", b"MZ", "application/octet-stream")}).status_code == 415
    assert client.post("/api/documents", headers=auth, files={"file": ("x.pdf", b"not a pdf", "application/pdf")}).status_code == 415


def test_tasks_crud(client, auth):
    t = client.post("/api/tasks", headers=auth, json={"title": "Write report", "due_at": "2031-05-01T10:00:00Z", "priority": "high"}).json()
    assert any(x["id"] == t["id"] for x in client.get("/api/tasks", headers=auth).json())
    assert client.put(f"/api/tasks/{t['id']}", headers=auth, json={"title": "Write report", "status": "done"}).status_code == 200
    assert any(x["id"] == t["id"] for x in client.get("/api/tasks?status=done", headers=auth).json())
    assert client.post("/api/tasks", headers=auth, json={"title": "x", "due_at": "not-a-date"}).status_code == 422
    client.delete(f"/api/tasks/{t['id']}", headers=auth)
    assert not any(x["id"] == t["id"] for x in client.get("/api/tasks?status=all", headers=auth).json())


def test_health_agents_usage(client, auth):
    assert client.get("/api/health").json()["mongo"] == "ok"
    agents = client.get("/api/agents", headers=auth).json()
    assert len(agents) == 9 and any(a["key"] == "desktop" for a in agents)
    assert client.get("/api/usage", headers=auth).json()[0]["calls"] > 0


def test_auth_rate_limit(client, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "auth_rate_limit_per_minute", 3)
    codes = [client.post("/api/auth/login", json={"email": "rl@example.com", "password": "x"}).status_code for _ in range(5)]
    assert codes[-1] == 429


# ---------- two-way realtime ----------
import pytest
from starlette.websockets import WebSocketDisconnect


def ws_url(h):
    return "/api/ws?token=" + h["Authorization"][7:]


def ws_collect(ws, until="end"):
    evs = []
    while True:
        ev = ws.receive_json()
        evs.append(ev)
        if ev["type"] == until or (ev["type"] == "error" and "status" in ev):
            return evs


def test_ws_requires_auth_and_origin(client, auth):
    client.cookies.clear()   # drop the session cookie left by registration
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect("/api/ws") as ws:
            ws.receive_json()
    assert e.value.code == 4401
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect(ws_url(auth), headers={"origin": "https://evil.example"}) as ws:
            ws.receive_json()
    assert e.value.code == 4403


def test_ws_chat_stream(client, auth):
    cid = new_conv(client, auth)
    with client.websocket_connect(ws_url(auth)) as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"type": "ping"}); assert ws.receive_json()["type"] == "pong"
        ws.send_json({"type": "chat", "conversation_id": cid, "message": "Say hello"})
        evs = ws_collect(ws)
        assert all(e.get("conversation_id") == cid for e in evs)
        assert "".join(e.get("delta", "") for e in evs) == "Hello from mock. pong"
        ws.send_json({"type": "chat", "conversation_id": cid, "message": ""})
        assert ws.receive_json()["status"] == 422


def test_ws_clarifying_question(client, auth):
    cid = new_conv(client, auth)
    with client.websocket_connect(ws_url(auth)) as ws:
        ws.receive_json()
        ws.send_json({"type": "chat", "conversation_id": cid,
                      "message": 'TOOL:ask_user {"question": "Which environment, staging or prod?", "options": ["staging", "prod"]}'})
        evs = ws_collect(ws)
        q = next(e for e in evs if e["type"] == "question")
        assert q["args"]["options"] == ["staging", "prod"]
        ws.send_json({"type": "chat", "conversation_id": cid, "message": "hi"})
        assert ws.receive_json()["status"] == 409          # blocked until answered
        ws.send_json({"type": "decision", "run_id": q["run_id"], "answer": "staging"})
        assert ws.receive_json()["pending_remaining"] == 0
        ws.send_json({"type": "chat", "conversation_id": cid, "mode": "continue"})
        evs = ws_collect(ws)
        assert "The user answered: staging" in "".join(e.get("delta", "") for e in evs)


def test_assistant_initiated_reminder_push(client, auth):
    import time
    with client.websocket_connect(ws_url(auth)) as ws:
        ws.receive_json()
        client.post("/api/tasks", headers=auth, json={"title": "Call the bank", "due_at": "2020-01-01T00:00:00Z"})
        deadline = time.time() + 5
        while time.time() < deadline:
            ev = ws.receive_json()
            if ev["type"] == "notification" and ev["item"]["title"] == "Reminder: Call the bank":
                break
        else:
            pytest.fail("no reminder pushed")
    items = client.get("/api/notifications?unread=true", headers=auth).json()
    assert any(n["title"] == "Reminder: Call the bank" for n in items)
    client.post("/api/notifications/read", headers=auth)
    assert client.get("/api/notifications?unread=true", headers=auth).json() == []


def test_voice_transcribe_unsupported_provider(client, auth):
    r = client.post("/api/voice/transcribe", headers=auth, files={"file": ("a.webm", b"\x1aE\xdf\xa3", "audio/webm")})
    assert r.status_code == 501 and "transcriptions" in r.json()["detail"]


def test_provider_error_messages_are_specific():
    from app.llm.provider import _explain
    ollama = '{"error":{"message":"model \'llama3.1:8b\' not found","type":"not_found_error"}}'
    msg = _explain(404, ollama)
    assert "model 'llama3.1:8b' not found" in msg and "ollama pull" in msg
    assert "bad key" in _explain(401, '{"error": {"message": "bad key"}}')
    assert "upstream exploded" in _explain(500, "upstream exploded")


def test_system_prompt_is_stable_across_turns(client, auth):
    """Prompt-cache friendliness: same agent => byte-identical system prompt and tools on every turn."""
    cid = new_conv(client, auth)
    chat(client, auth, cid, message="Explain what a closure is in javascript code", agent="dev")
    first = [r for r in mock_llm.REQUESTS if r.get("tools")][-1]
    chat(client, auth, cid, message="Now show an example function with a bug", agent="dev")
    second = [r for r in mock_llm.REQUESTS if r.get("tools")][-1]
    assert first["messages"][0] == second["messages"][0] and first["tools"] == second["tools"]
    # history is replayed as stored (no stale context block), only the newest user message carries context
    users = [m["content"] for m in second["messages"] if m["role"] == "user"]
    assert users[0] == "Explain what a closure is in javascript code"
    assert users[-1].endswith("Now show an example function with a bug")      # the user's words come last


def test_reindex_upgrades_keyword_documents(client, auth):
    from bson import ObjectId
    doc = client.post("/api/documents", headers=auth, files={"file": ("notes.txt", b"Solar panels need cleaning twice a year.", "text/plain")}).json()
    DB.chunks.update_many({"document_id": ObjectId(doc["id"])}, {"$unset": {"embedding": "", "embed_model": ""}})
    DB.documents.update_one({"_id": ObjectId(doc["id"])}, {"$set": {"retrieval": "keyword"}})
    r = client.post(f"/api/documents/{doc['id']}/reindex", headers=auth).json()
    assert r["retrieval"] == "vector" and r["embed_model"] == "mock-embed"
    assert DB.chunks.find_one({"document_id": ObjectId(doc["id"])})["embed_model"] == "mock-embed"


def test_vectors_from_another_embedding_model_are_ignored(client, auth):
    from bson import ObjectId
    doc = client.post("/api/documents", headers=auth, files={"file": ("other.txt", b"Quantum kittens purr loudly.", "text/plain")}).json()
    DB.chunks.update_many({"document_id": ObjectId(doc["id"])}, {"$set": {"embed_model": "other-model", "embedding": [0.1] * 1024}})
    hits = client.post("/api/kb/search", headers=auth, json={"query": "quantum kittens"}).json()   # no crash on 1024 vs 64 dims
    assert any(h["filename"] == "other.txt" for h in hits)                                          # still found by keyword
