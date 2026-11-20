import os, tempfile
os.environ["MONGO_DB"] = "terminator_test"
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="term-test-")
os.environ["AUTH_RATE_LIMIT_PER_MINUTE"] = "1000"
os.environ["REMINDER_INTERVAL_S"] = "0.2"
os.environ["DESKTOP_CONTROL"] = "false"   # tests must never touch the real desktop
# ...nor real accounts: never the real WhatsApp bridge / its session store, never real Microsoft Graph
os.environ["WHATSAPP_BRIDGE_URL"] = "http://127.0.0.1:9"
os.environ["WHATSAPP_STORE_DIR"] = tempfile.mkdtemp(prefix="term-wa-")
os.environ["GRAPH_BASE_URL"] = "http://127.0.0.1:9/v1.0"
os.environ["MS_AUTHORITY_BASE"] = "http://127.0.0.1:9"
os.environ["CLOUDFLARE_API_BASE"] = "http://127.0.0.1:9/client/v4"
os.environ["WIKIPEDIA_BASE"] = "http://127.0.0.1:9"
os.environ["SYSTEM_COMMAND_ALLOWLIST"] = "uptime,df"
import pytest
from pymongo import MongoClient
from fastapi.testclient import TestClient
from tests import mock_llm


@pytest.fixture(scope="session")
def llm_url():
    return mock_llm.start()


@pytest.fixture(scope="session")
def client(llm_url):
    MongoClient("mongodb://localhost:27017").drop_database("terminator_test")
    from app.main import app
    with TestClient(app) as c:
        yield c


def make_user(client, email):
    r = client.post("/api/auth/register", json={"email": email, "password": "correct-horse-1"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture(scope="session")
def auth(client, llm_url):
    h = make_user(client, "alice@example.com")
    r = client.post("/api/providers", headers=h, json={"name": "Mock", "base_url": llm_url, "api_key": "sk-test-secret-123",
                    "models": [{"name": "mock-1"}], "embedding_model": "mock-embed"})
    assert r.status_code == 200, r.text
    return h
