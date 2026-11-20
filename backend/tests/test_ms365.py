"""Microsoft 365 (Teams chats, Outlook mail, calendar) against a simulated Microsoft Graph."""
import time
import pytest
from app.integrations import ms365
from tests import mock_llm
from tests.test_api import new_conv, chat


@pytest.fixture
def graph(monkeypatch, llm_url):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "graph_base_url", llm_url.removesuffix("/v1") + "/v1.0")

    async def tok(user):
        return "test-token"

    async def yes(user):
        return True
    monkeypatch.setattr(ms365, "token", tok)
    monkeypatch.setattr(ms365, "connected", yes)
    mock_llm.GRAPH.clear()
    return mock_llm.GRAPH


def reply(evs):
    return "".join(e.get("delta", "") for e in evs)


def test_device_code_sign_in(client, auth, monkeypatch):
    class FakeApp:
        def __init__(self, client_id, authority=None, token_cache=None):
            self.authority = authority
        def initiate_device_flow(self, scopes):
            assert "Chat.ReadWrite" in scopes and "Mail.Send" in scopes
            return {"user_code": "ABCD-1234", "verification_uri": "https://microsoft.com/devicelogin", "expires_in": 900}
        def acquire_token_by_device_flow(self, flow):
            time.sleep(0.5)                                     # the user approving in their browser
            return {"access_token": "t", "id_token_claims": {"name": "Sam Carter", "preferred_username": "sam@contoso.com", "oid": "1"}}
        def get_accounts(self):
            return [{"username": "sam@contoso.com"}]
        def acquire_token_silent(self, scopes, account):
            return {"access_token": "silent"}
    monkeypatch.setattr(ms365.msal, "PublicClientApplication", FakeApp)
    r = client.post("/api/integrations/ms365/connect", headers=auth,
                    json={"client_id": "11111111-2222-3333-4444-555555555555", "tenant_id": "contoso.onmicrosoft.com"})
    assert r.status_code == 200 and r.json()["user_code"] == "ABCD-1234"
    assert client.get("/api/integrations/ms365", headers=auth).json()["state"] == "signing_in"
    for _ in range(30):
        st = client.get("/api/integrations/ms365", headers=auth).json()
        if st["state"] == "connected":
            break
        time.sleep(0.1)
    assert st["state"] == "connected" and st["account"]["email"] == "sam@contoso.com"
    assert client.post("/api/integrations/ms365/connect", headers=auth, json={"client_id": "not-a-guid"}).status_code == 422
    client.post("/api/integrations/ms365/disconnect", headers=auth)
    assert client.get("/api/integrations/ms365", headers=auth).json()["state"] == "signed_out"


def test_teams_message_by_name_sends_as_user(client, auth, graph):
    evs = chat(client, auth, new_conv(client, auth), message="message hii to arjun mehta in teams")
    assert evs[0]["agent"] == "quick" and reply(evs) == "Sent to Arjun Mehta on Teams."
    create = next(b for m, p, b in graph if p == "/chats")
    assert create["chatType"] == "oneOnOne"
    assert [x["user@odata.bind"] for x in create["members"]] == [
        "https://graph.microsoft.com/v1.0/users('me-id')", "https://graph.microsoft.com/v1.0/users('u-arjun')"]
    assert next(b for m, p, b in graph if p == "/chats/chat-1/messages") == {"body": {"contentType": "text", "content": "Hii"}}


def test_ambiguous_colleague_is_reported(client, auth, graph):
    r = reply(chat(client, auth, new_conv(client, auth), message="say hi to priya on teams"))
    assert "Several people match" in r and "Priya Nair" in r and "Priya Shah" in r
    assert not any(p.endswith("/messages") for m, p, b in graph)


def test_email_with_subject_and_body(client, auth, graph):
    r = reply(chat(client, auth, new_conv(client, auth), message="email arjun mehta about release saying the build is green"))
    assert r == "Email sent to Arjun Mehta."
    msg = next(b for m, p, b in graph if p == "/me/sendMail")["message"]
    assert msg["subject"] == "Release" and msg["body"]["content"] == "The build is green"
    assert msg["toRecipients"] == [{"emailAddress": {"address": "arjun@contoso.com"}}]


def test_calendar_and_inbox_summaries(client, auth, graph):
    r = reply(chat(client, auth, new_conv(client, auth), message="what's on my calendar today"))
    assert r.startswith("You have 2 events today:") and "Standup" in r and "Release review" in r
    assert reply(chat(client, auth, new_conv(client, auth), message="any new emails")) == "Latest unread: CI Bot: Build failed."


def test_model_initiated_email_asks_first(client, auth, graph):
    evs = chat(client, auth, new_conv(client, auth), agent="desktop",
               message='TOOL:email_send {"to": "Arjun Mehta", "subject": "Hi", "body": "Hello"}')
    assert any(e["type"] == "approval_required" for e in evs)
    assert not any(p == "/me/sendMail" for m, p, b in graph)


def test_teams_read_recent_chats(client, auth, graph):
    evs = chat(client, auth, new_conv(client, auth), agent="desktop", message='TOOL:teams_read {"limit": 5}')
    prev = next(e for e in evs if e["type"] == "tool_end")["preview"]
    assert "Arjun Mehta" in prev and "see you at 5" in prev and "<p>" not in prev


def test_not_connected_falls_back(client, auth):
    from datetime import datetime
    from app import intents
    assert intents.match("email arjun about release saying hi", datetime(2026, 11, 20, 15), ms_connected=False) is None
    assert intents.match("what's on my calendar today", datetime(2026, 11, 20, 15), ms_connected=False) is None
