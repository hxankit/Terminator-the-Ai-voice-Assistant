"""Environment save/restore + power actions. Fake desktop and fake capture: nothing real is read, opened or shut down."""
import struct
from datetime import datetime
import pytest
from pymongo import MongoClient
from app import environment, intents
from tests import mock_llm
from tests.test_api import new_conv, chat
from tests.test_desktop import fake_desktop  # noqa: F401  (fixture)

DB = MongoClient("mongodb://localhost:27017")["terminator_test"]


def _cmd(cid: int, payload: bytes) -> bytes:
    return struct.pack("<H", len(payload) + 1) + bytes([cid]) + payload


def _nav(tab: int, idx: int, url: str) -> bytes:
    pad = b"\0" * ((4 - len(url) % 4) % 4)
    body = struct.pack("<ii", tab, idx) + struct.pack("<i", len(url)) + url.encode() + pad + struct.pack("<i", 0)
    return struct.pack("<I", len(body)) + body


def test_snss_parser(tmp_path):
    d = b"SNSS" + struct.pack("<i", 3)
    d += _cmd(0, struct.pack("<ii", 1, 10)) + _cmd(2, struct.pack("<ii", 10, 0))
    d += _cmd(6, _nav(10, 0, "https://a.example/")) + _cmd(6, _nav(10, 1, "https://a.example/page2"))
    d += _cmd(7, struct.pack("<ii", 10, 1))                                   # back/forward: selected = page2
    d += _cmd(0, struct.pack("<ii", 1, 11)) + _cmd(2, struct.pack("<ii", 11, 1)) + _cmd(6, _nav(11, 0, "https://b.example/"))
    d += _cmd(0, struct.pack("<ii", 2, 20)) + _cmd(6, _nav(20, 0, "chrome://newtab/"))       # new-tab pages skipped
    d += _cmd(0, struct.pack("<ii", 2, 21)) + _cmd(6, _nav(21, 0, "https://c.example/"))
    d += _cmd(0, struct.pack("<ii", 1, 12)) + _cmd(6, _nav(12, 0, "https://closed.example/")) + _cmd(16, struct.pack("<iiq", 12, 0, 0))
    d += _cmd(9, struct.pack("<ii", 4, 1)) + _cmd(0, struct.pack("<ii", 4, 40)) + _cmd(6, _nav(40, 0, "https://popup.example/"))
    d += _cmd(0, struct.pack("<ii", 5, 50)) + _cmd(6, _nav(50, 0, "https://gone.example/")) + _cmd(17, struct.pack("<iiq", 5, 0, 0))
    d += struct.pack("<H", 40) + b"\x06garbage"                               # truncated record at the end is ignored
    f = tmp_path / "Session_1"
    f.write_bytes(d)
    assert environment.parse_snss(f) == [["https://a.example/page2", "https://b.example/"], ["https://c.example/"]]


@pytest.mark.parametrize("text,tool,action", [
    ("open previous envirnment", "restore_environment", None), ("restore my session", "restore_environment", None),
    ("bring back my previous workspace", "restore_environment", None), ("save my environment", "save_environment", None),
    ("shutdonw the system", "power_action", "shutdown"), ("Hey Jarvis, shut down the computer", "power_action", "shutdown"),
    ("restart the pc", "power_action", "restart"), ("log out", "power_action", "logout"), ("suspend", "power_action", "suspend"),
])
def test_environment_and_power_intents(text, tool, action):
    it = intents.match(text, datetime(2026, 11, 20, 15, 0))
    assert it is not None and it.tool == tool
    if action:
        assert it.args["action"] == action


@pytest.mark.parametrize("text", ["turn off the lights", "how do I shut down a nodejs server gracefully",
                                  "remind me to shut down at 6 pm"])
def test_power_phrases_that_must_not_trigger(text):
    it = intents.match(text, datetime(2026, 11, 20, 15, 0))
    assert it is None or it.tool != "power_action"


FAKE_SNAPSHOT = {"browsers": [{"browser": "google-chrome", "profile": "Profile 2", "restores_itself": False,
                               "windows": [["https://a.example/", "https://b.example/"], ["https://c.example/"]]}],
                 "vscode": [], "terminals": [], "apps": [{"id": "mongodb-compass", "name": "MongoDB Compass", "files": []}]}


@pytest.fixture
def fake_capture(monkeypatch):
    snap = dict(FAKE_SNAPSHOT, summary=environment.summarize(FAKE_SNAPSHOT))
    monkeypatch.setattr(environment, "capture", lambda: dict(snap))
    return snap


def test_restore_relaunches_everything(fake_desktop):
    calls, _ = fake_desktop
    done = environment.restore(FAKE_SNAPSHOT)
    assert ["google-chrome", "--profile-directory=Profile 2", "--new-window", "https://a.example/", "https://b.example/"] in calls["spawn"]
    assert ["google-chrome", "--profile-directory=Profile 2", "--new-window", "https://c.example/"] in calls["spawn"]
    assert ["gtk-launch", "mongodb-compass"] in calls["spawn"]
    assert done == ["2 browser windows", "MongoDB Compass"]
    restores_itself = {**FAKE_SNAPSHOT, "browsers": [{**FAKE_SNAPSHOT["browsers"][0], "restores_itself": True}]}
    calls["spawn"].clear()
    environment.restore(restores_itself)                       # Chrome set to "continue where you left off"
    assert calls["spawn"][0] == ["google-chrome", "--profile-directory=Profile 2"]


def test_restore_without_snapshot_explains(client, auth, fake_desktop):
    DB.env_snapshots.delete_many({})
    reply = "".join(e.get("delta", "") for e in chat(client, auth, new_conv(client, auth), message="open previous environment"))
    assert "don't have a saved environment yet" in reply


def test_shutdown_needs_approval_saves_then_powers_off_without_llm(client, auth, fake_desktop, fake_capture):
    calls, _ = fake_desktop
    DB.env_snapshots.delete_many({})
    cid = new_conv(client, auth)
    n_llm = len(mock_llm.REQUESTS)
    evs = chat(client, auth, cid, message="shut down the system")
    appr = next(e for e in evs if e["type"] == "approval_required")
    assert appr["name"] == "power_action" and appr["args"]["action"] == "shutdown"
    assert not any(c[0] == "bash" for c in calls["spawn"])     # nothing happens before approval
    r = client.post(f"/api/tools/runs/{appr['run_id']}/decision", headers=auth, json={"approve": True}).json()
    assert r["status"] == "ok"
    power = [c for c in calls["spawn"] if c[0] == "bash"][-1]
    assert "gnome-session-quit --power-off --no-prompt" in power[2] and "systemctl poweroff" in power[2]
    assert DB.env_snapshots.find_one({"reason": "shutdown"})["summary"].startswith("Chrome: 2 windows, 3 tabs")
    reply = "".join(e.get("delta", "") for e in chat(client, auth, cid, mode="continue"))
    assert reply.startswith("Shutting down in a few seconds") and "open previous environment" in reply
    assert len(mock_llm.REQUESTS) == n_llm                     # the whole flow never needed the LLM
    # next day: "open previous environment" reopens the saved tabs
    calls["spawn"].clear()
    reply = "".join(e.get("delta", "") for e in chat(client, auth, new_conv(client, auth), message="open previous environment"))
    assert "2 browser windows" in reply and "MongoDB Compass" in reply
    assert any("https://c.example/" in c for c in calls["spawn"])


def test_denied_shutdown_does_nothing(client, auth, fake_desktop, fake_capture):
    calls, _ = fake_desktop
    cid = new_conv(client, auth)
    appr = next(e for e in chat(client, auth, cid, message="restart the computer") if e["type"] == "approval_required")
    client.post(f"/api/tools/runs/{appr['run_id']}/decision", headers=auth, json={"approve": False})
    assert not any(c[0] == "bash" for c in calls["spawn"])
    assert "".join(e.get("delta", "") for e in chat(client, auth, cid, mode="continue")) == "Okay, I won't."


def test_environments_api(client, auth, fake_desktop, fake_capture):
    chat(client, auth, new_conv(client, auth), message="save my environment")
    envs = client.get("/api/environments", headers=auth).json()
    assert envs[0]["reason"] == "manual" and envs[0]["tabs"] == 3
    r = client.post(f"/api/environments/{envs[0]['id']}/restore", headers=auth).json()
    assert "MongoDB Compass" in r["restored"]


def test_shutdown_snapshot_is_not_overwritten_by_autosave(client, auth, fake_desktop, fake_capture):
    import time
    environment._frozen_until = 0.0
    appr = next(e for e in chat(client, auth, new_conv(client, auth), message="shut down the computer")
                if e["type"] == "approval_required")
    client.post(f"/api/tools/runs/{appr['run_id']}/decision", headers=auth, json={"approve": True})
    assert environment._frozen_until > time.time() + 600       # autosave paused while apps close
    environment._frozen_until = 0.0
