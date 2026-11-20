"""WhatsApp bridge integration against a fake bridge and fake message stores (no real WhatsApp involved)."""
import sqlite3, time
import pytest
from app.integrations import whatsapp as wa
from tests import mock_llm
from tests.test_api import new_conv, chat
from tests.test_desktop import fake_desktop  # noqa: F401  (fixture)

ARJUN, MOM, FAMILY = "919811122233@s.whatsapp.net", "919800011111@s.whatsapp.net", "120363000000000001@g.us"


@pytest.fixture
def bridge(tmp_path, monkeypatch, llm_url):
    """Fake store (same schema as the Go bridge) + the mock server playing the bridge's REST API."""
    from app.config import get_settings
    con = sqlite3.connect(tmp_path / "whatsapp.db")
    con.execute("CREATE TABLE whatsmeow_contacts (our_jid TEXT, their_jid TEXT, first_name TEXT, full_name TEXT, push_name TEXT, business_name TEXT)")
    con.executemany("INSERT INTO whatsmeow_contacts VALUES ('me', ?, ?, ?, ?, NULL)",
                    [(ARJUN, "Arjun", "Arjun Mehta", "Arjun D"), (MOM, "Mummy", "Mummy", "Mom")])
    con.commit(); con.close()
    con = sqlite3.connect(tmp_path / "messages.db")
    con.execute("CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT, last_message_time TIMESTAMP)")
    con.execute("CREATE TABLE messages (id TEXT, chat_jid TEXT, sender TEXT, content TEXT, timestamp TIMESTAMP, is_from_me BOOLEAN, media_type TEXT)")
    con.executemany("INSERT INTO chats VALUES (?, ?, ?)", [(ARJUN, "Arjun Mehta", "2026-11-20 10:00:00"),
                                                         (FAMILY, "Family", "2026-11-20 11:00:00")])
    con.executemany("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, NULL)", [
        ("1", ARJUN, "919811122233", "Are we meeting today?", "2026-11-20 09:58:00", 0),
        ("2", ARJUN, "me", "Yes, at 5", "2026-11-20 10:00:00", 1)])
    con.commit(); con.close()
    monkeypatch.setattr(get_settings(), "whatsapp_store_dir", str(tmp_path))
    monkeypatch.setattr(get_settings(), "whatsapp_bridge_url", llm_url.removesuffix("/v1"))
    mock_llm.WA_SENT.clear()
    return tmp_path


def test_find_contacts_uses_phone_book_and_chat_names(bridge):
    assert wa.find_contacts("arjun mehta")[0]["jid"] == ARJUN
    assert wa.find_contacts("mom")[0]["jid"] == MOM                       # push name / nickname
    fam = wa.find_contacts("family")[0]
    assert fam["jid"] == FAMILY and fam["group"] is True


def test_send_command_sends_directly(client, auth, bridge):
    evs = chat(client, auth, new_conv(client, auth), message="send a whatsapp to arjun mehta saying hi there")
    assert evs[0]["agent"] == "quick" and not any(e["type"] == "approval_required" for e in evs)
    assert mock_llm.WA_SENT[-1] == {"recipient": ARJUN, "message": "Hi there"}
    assert "".join(e.get("delta", "") for e in evs) == "Sent to Arjun Mehta."


def test_message_first_phrasing_and_groups(client, auth, bridge):
    chat(client, auth, new_conv(client, auth), message="message hii to arjun mehta on whatsapp")
    assert mock_llm.WA_SENT[-1] == {"recipient": ARJUN, "message": "Hii"}
    chat(client, auth, new_conv(client, auth), message="whatsapp family: dinner at 8")
    assert mock_llm.WA_SENT[-1] == {"recipient": FAMILY, "message": "Dinner at 8"}


def test_model_initiated_send_still_asks(client, auth, bridge):
    evs = chat(client, auth, new_conv(client, auth), agent="desktop",
               message='TOOL:whatsapp_send {"to": "Arjun Mehta", "message": "hello"}')
    assert any(e["type"] == "approval_required" for e in evs) and not mock_llm.WA_SENT


def test_not_linked_explains_instead_of_opening_chrome(client, auth, bridge, fake_desktop, monkeypatch):
    from app.config import get_settings
    calls, _ = fake_desktop
    monkeypatch.setattr(get_settings(), "whatsapp_bridge_url", "http://127.0.0.1:9")      # bridge API down
    evs = chat(client, auth, new_conv(client, auth), message="send a whatsapp to +91 98111 22233 saying yo")
    reply = "".join(e.get("delta", "") for e in evs)
    assert "isn't linked" in reply and "Settings → WhatsApp" in reply and not reply.startswith("Sorry")
    assert not calls["spawn"] and not mock_llm.WA_SENT                                        # no Chrome, no send


def test_status_distinguishes_linked_from_never_linked(bridge, monkeypatch):
    import asyncio, sqlite3
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "whatsapp_bridge_url", "http://127.0.0.1:9")
    con = sqlite3.connect(bridge / "whatsapp.db")
    con.execute("CREATE TABLE whatsmeow_device (jid TEXT)")
    con.commit()
    assert asyncio.run(wa.status())["state"] == "starting"                                     # never scanned
    con.execute("INSERT INTO whatsmeow_device VALUES ('919800000000.0:1@s.whatsapp.net')")
    con.commit(); con.close()
    assert asyncio.run(wa.status())["state"] == "offline"                                      # linked, bridge down


def test_draft_only_prepares(client, auth, bridge, fake_desktop):
    calls, _ = fake_desktop
    chat(client, auth, new_conv(client, auth), message="draft a whatsapp to +91 98111 22233 saying see you soon")
    assert not mock_llm.WA_SENT and calls["spawn"][-1][1].startswith("https://web.whatsapp.com/send")


def test_read_messages_and_chats(client, auth, bridge):
    evs = chat(client, auth, new_conv(client, auth), agent="desktop", message='TOOL:whatsapp_read {"chat": "arjun"}')
    prev = next(e for e in evs if e["type"] == "tool_end")["preview"]
    assert "Are we meeting today?" in prev and '"from": "You"' in prev
    assert [c["name"] for c in wa.recent_chats()] == ["Family", "Arjun Mehta"]


def test_status_reports_connection_and_qr(client, auth, bridge, monkeypatch):
    from app.config import get_settings
    assert client.get("/api/integrations/whatsapp", headers=auth).json()["state"] == "connected"
    monkeypatch.setattr(get_settings(), "whatsapp_bridge_url", "http://127.0.0.1:9")
    (bridge / "qr.txt").write_text("2@abc,def,ghi")
    st = client.get("/api/integrations/whatsapp", headers=auth).json()
    assert st == {"state": "waiting_for_scan", "qr": "2@abc,def,ghi"}
    (bridge / "qr.txt").unlink()
    assert client.get("/api/integrations/whatsapp", headers=auth).json()["state"] == "starting"


def test_users_own_request_sends_without_approval(client, auth, bridge):
    evs = chat(client, auth, new_conv(client, auth), agent="desktop",
               message='tell arjun this. TOOL:whatsapp_send {"to": "Arjun Mehta", "message": "hello"}')
    assert not any(e["type"] == "approval_required" for e in evs)
    assert mock_llm.WA_SENT[-1] == {"recipient": ARJUN, "message": "hello"}


def test_follow_up_stays_with_the_conversation_agent(client, auth, bridge):
    cid = new_conv(client, auth)
    chat(client, auth, cid, agent="desktop", message='ask arjun on whatsapp what he is doing')
    evs = chat(client, auth, cid, message="so there are test messages tell her")      # no keywords at all
    assert evs[0]["agent"] == "desktop" and evs[0]["route"] == "context"
    evs = chat(client, auth, cid, message="now fix this python bug in my function code")  # clear new topic
    assert evs[0]["agent"] == "dev"
