"""Gmail (app password, IMAP/SMTP) and Instagram (instagrapi) with fakes - no real accounts involved."""
import imaplib, smtplib, time
from types import SimpleNamespace
import pytest
from pymongo import MongoClient
from app.integrations import gmail, instagram
from tests.test_api import new_conv, chat

DB = MongoClient("mongodb://localhost:27017")["terminator_test"]
GOOD_PW = "abcdabcdabcdabcd"
MAIL = {b"1": b"From: Priya Nair <priya@example.com>\r\nTo: me@gmail.com\r\nSubject: Release notes\r\n"
               b"Date: Fri, 20 Nov 2026 09:00:00 +0530\r\n\r\nThe build is green\r\n",
        b"2": b"From: CI Bot <ci@example.com>\r\nTo: me@gmail.com\r\nSubject: Build failed\r\n\r\nPipeline #42 failed\r\n"}


class FakeIMAP:
    def login(self, user, pw):
        if pw != GOOD_PW:
            raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)")
    def select(self, folder, readonly=True):
        return "OK", [b"2"]
    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            return "OK", [b"2" if "is:unread" in args[-1] else b"1 2"]
        uid = args[0]
        flags = b"" if uid == b"2" else b"\\Seen"
        return "OK", [(b"1 (FLAGS (" + flags + b") BODY[]<0> {100}", MAIL[uid]), b")"]
    def logout(self):
        pass


class FakeSMTP:
    sent: list = []
    def login(self, user, pw):
        if pw != GOOD_PW:
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")
    def send_message(self, msg):
        FakeSMTP.sent.append(msg)
    def quit(self):
        pass


@pytest.fixture
def fake_gmail(monkeypatch):
    monkeypatch.setattr(gmail, "_imap", FakeIMAP)
    monkeypatch.setattr(gmail, "_smtp", FakeSMTP)
    FakeSMTP.sent.clear()


def reply(evs):
    return "".join(e.get("delta", "") for e in evs)


def test_gmail_connect_send_and_read(client, auth, fake_gmail):
    bad = client.post("/api/integrations/gmail/connect", headers=auth, json={"email": "me@gmail.com", "app_password": "wrongwrongwrongw"})
    assert bad.status_code == 422 and "rejected" in bad.json()["detail"]
    ok = client.post("/api/integrations/gmail/connect", headers=auth, json={"email": "me@gmail.com", "app_password": "abcd abcd abcd abcd"})
    assert ok.json() == {"state": "connected", "email": "me@gmail.com"}
    doc = DB.integrations.find_one({"kind": "gmail"})
    assert GOOD_PW not in str(doc)                                    # stored encrypted
    r = reply(chat(client, auth, new_conv(client, auth), message="send a gmail to priya nair saying the release is out"))
    assert r == "Email sent to Priya Nair."
    sent = FakeSMTP.sent[-1]
    assert sent["To"] == "priya@example.com" and sent["Subject"] == "The release is out" and "The release is out" in sent.get_content()
    assert reply(chat(client, auth, new_conv(client, auth), message="any new gmails")) == "Latest unread: CI Bot <ci@example.com>: Build failed."
    client.post("/api/integrations/gmail/disconnect", headers=auth)
    assert client.get("/api/integrations/gmail", headers=auth).json() == {"state": "not_connected"}


# ---------------- Instagram ----------------
from instagrapi.exceptions import BadPassword, TwoFactorRequired


class FakeIG:
    sent: list = []
    def __init__(self):
        self.delay_range = None
        self.challenge_code_handler = None
    def login(self, username, password, verification_code=""):
        if password != "pw":
            raise BadPassword("bad password")
        if username == "twofa" and not verification_code:
            raise TwoFactorRequired("two factor")
        if username == "twofa":
            assert verification_code == "654321"
        if username == "challenged":
            assert self.challenge_code_handler(username, "EMAIL") == "123456"
        return True
    def get_settings(self):
        return {"authorization_data": {"sessionid": "s"}}
    def set_settings(self, s):
        self.settings = s
    def direct_threads(self, amount=20, **kw):
        riya = SimpleNamespace(pk="42", username="riya.s", full_name="Riya Sharma")
        return [SimpleNamespace(id="t1", users=[riya], thread_title="Riya Sharma",
                                messages=[SimpleNamespace(text="hey!", item_type="text", timestamp="2026-11-20 10:00", user_id="42")])]
    def direct_messages(self, thread_id, amount=20):
        return [SimpleNamespace(text="hey!", item_type="text", timestamp="2026-11-20 10:00", user_id="42"),
                SimpleNamespace(text="hi riya", item_type="text", timestamp="2026-11-20 09:59", user_id="1")]
    def direct_send(self, text, user_ids=[], **kw):
        FakeIG.sent.append((text, user_ids))
    def user_id_from_username(self, u):
        return "1"
    def user_following(self, user_id, use_cache=True, amount=0):
        return {}


@pytest.fixture
def fake_ig(monkeypatch):
    monkeypatch.setattr(instagram, "Client", FakeIG)
    instagram.CLIENTS.clear(); instagram.LOGINS.clear(); FakeIG.sent.clear()
    yield
    instagram.CLIENTS.clear(); instagram.LOGINS.clear()


def _wait(client, auth, states):
    for _ in range(50):
        st = client.get("/api/integrations/instagram", headers=auth).json()
        if st["state"] in states:
            return st
        time.sleep(0.1)
    return st


def test_instagram_login_with_2fa_and_challenge_never_stores_password(client, auth, fake_ig):
    client.post("/api/integrations/instagram/connect", headers=auth, json={"username": "twofa", "password": "pw"})
    assert _wait(client, auth, {"needs_2fa"})["state"] == "needs_2fa"
    assert client.post("/api/integrations/instagram/code", headers=auth, json={"code": "654321"}).status_code == 200
    assert _wait(client, auth, {"connected"}) == {"state": "connected", "username": "twofa"}
    doc = DB.integrations.find_one({"kind": "instagram"})
    assert "pw" not in {v for v in doc.values() if isinstance(v, str)} and doc["settings_enc"] and "password" not in doc
    client.post("/api/integrations/instagram/disconnect", headers=auth)
    client.post("/api/integrations/instagram/connect", headers=auth, json={"username": "challenged", "password": "pw"})
    st = _wait(client, auth, {"needs_code"})
    assert st["state"] == "needs_code" and st["kind"] == "EMAIL"
    client.post("/api/integrations/instagram/code", headers=auth, json={"code": "123456"})
    assert _wait(client, auth, {"connected"})["state"] == "connected"
    client.post("/api/integrations/instagram/disconnect", headers=auth)
    client.post("/api/integrations/instagram/connect", headers=auth, json={"username": "someone", "password": "nope"})
    st = _wait(client, auth, {"error"})
    assert st["state"] == "error" and "password is wrong" in st["error"]


def test_instagram_send_and_read(client, auth, fake_ig):
    client.post("/api/integrations/instagram/connect", headers=auth, json={"username": "me", "password": "pw"})
    _wait(client, auth, {"connected"})
    r = reply(chat(client, auth, new_conv(client, auth), message="message riya on instagram saying are you coming tonight"))
    assert r == "Sent to Riya Sharma on Instagram." and FakeIG.sent[-1] == ("Are you coming tonight", [42])
    r = reply(chat(client, auth, new_conv(client, auth), message="dm gandalf on insta: you shall not pass"))
    assert "isn't in your Instagram DMs" in r and len(FakeIG.sent) == 1     # never messages a stranger
    evs = chat(client, auth, new_conv(client, auth), agent="desktop", message='TOOL:instagram_read {"who": "riya"}')
    prev = next(e for e in evs if e["type"] == "tool_end")["preview"]
    assert '"from": "You"' in prev and "hey!" in prev
    client.post("/api/integrations/instagram/disconnect", headers=auth)
