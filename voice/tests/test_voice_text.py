import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from terminator_voice import SentenceSplitter, parse_yes_no, speakable, describe_action, Endpointer, WAKE_PREFIX
import numpy as np


def test_splitter_streams_sentences_and_skips_code():
    s = SentenceSplitter()
    out = []
    for ch in "Sure! Pi is 3.14 roughly. Code:\n```python\nprint('a. b')\n```\nThat prints **hi**. See [docs](https://x.y) now":
        out += s.feed(ch)
    early = list(out)
    out += s.flush()
    assert early[:2] == ["Sure!", "Pi is 3.14 roughly."]
    assert "I've put the code on screen." in out and not any("print(" in x for x in out)
    assert out[-1] == "See docs now"


def test_yes_no():
    assert parse_yes_no("Yes, go ahead") is True
    assert parse_yes_no("hey jarvis okay do it") is True
    assert parse_yes_no("No, don't") is False
    assert parse_yes_no("maybe later") is None


def test_describe_and_speakable():
    assert describe_action({"name": "run_shell", "args": {"command": "df -h"}}) == "run this command: df -h"
    assert speakable("# Title\n- **bold** item with `code` and https://example.com") == "Title bold item with code and link"
    assert WAKE_PREFIX.sub("", "Hey Jarvis, what time is it?") == "what time is it?"


def test_endpointer_start_and_end():
    loud, quiet = np.full(1280, 3000, np.int16), np.zeros(1280, np.int16)
    ep = Endpointer(noise=50, start_timeout=2)
    states = [ep.feed(f) for f in [quiet] * 3 + [loud] * 10 + [quiet] * 12]
    assert "speech" in states and states[-1] == "done"
    ep = Endpointer(noise=50, start_timeout=0.5)
    assert [ep.feed(quiet) for _ in range(10)][-1] == "timeout"


# ---------- conversation logic with a fake backend (no audio, no network) ----------
import asyncio
from terminator_voice import Assistant, Speaker


class FakeBackend:
    def __init__(self, script):
        self.script, self.sent, self.decisions, self.q = script, [], [], None
        self.on_notification = lambda item: None

    async def conversation(self, fresh=False):
        return "c1"

    def subscribe(self, cid):
        self.q = asyncio.Queue()
        return self.q

    def unsubscribe(self, cid, q):
        pass

    async def send(self, msg):
        self.sent.append(msg)
        delay, events = self.script.pop(0)
        async def feed():
            await asyncio.sleep(delay)
            for ev in events:
                self.q.put_nowait({**ev, "conversation_id": "c1"})
        asyncio.create_task(feed())

    async def decide(self, run_id, **body):
        self.decisions.append((run_id, body))
        return {"status": "ok", "pending_remaining": 0}


def make_assistant(script, answers):
    sp = Speaker("none", dry_run=True)
    a = Assistant({"threshold": 0.5, "follow_up": False}, source=None, speaker=sp, ears=None, backend=FakeBackend(script))
    async def fake_listen(start_timeout=6.0, chime=True):
        return answers.pop(0)
    a.listen = fake_listen
    return a, sp


def test_voice_approval_yes_continues_turn():
    async def go():
        approval = {"type": "approval_required", "run_id": "r1", "name": "run_shell", "args": {"command": "df -h"}}
        a, sp = make_assistant([(0, [approval, {"type": "end"}]),
                                (0, [{"type": "text", "delta": "Disk is 40% full. "}, {"type": "end"}])], ["Yes, go ahead"])
        await a.handle("how full is my disk")
        await sp.wait_idle()
        assert a.backend.decisions == [("r1", {"approve": True})]
        assert a.backend.sent[-1] == {"type": "chat", "conversation_id": "c1", "mode": "continue"}
        assert "I need your permission to run this command: df -h. Should I go ahead?" in sp.spoken
        assert sp.spoken[-1] == "Disk is 40% full."
    asyncio.run(go())


def test_voice_unclear_answer_is_a_no():
    async def go():
        approval = {"type": "approval_required", "run_id": "r2", "name": "run_shell", "args": {"command": "rm -rf build"}}
        a, sp = make_assistant([(0, [approval, {"type": "end"}]), (0, [{"type": "end"}])], ["maybe later"])
        await a.handle("clean the build")
        await sp.wait_idle()
        assert a.backend.decisions == [("r2", {"approve": False})] and "I'll take that as a no." in sp.spoken
    asyncio.run(go())


def test_voice_question_answer_and_slow_model_feedback():
    async def go():
        q = {"type": "question", "run_id": "q1", "name": "ask_user", "args": {"question": "Which Rahul?", "options": ["Rahul S", "Rahul K"]}}
        a, sp = make_assistant([(3.3, [q, {"type": "end"}]), (0, [{"type": "text", "delta": "Done."}, {"type": "end"}])], ["Rahul K"])
        await a.handle("message rahul i'm late")
        await sp.wait_idle()
        assert sp.spoken[0] == "Working on it."                       # spoken after 3 s of silence from the model
        assert "Which Rahul? You can say Rahul S or Rahul K." in sp.spoken
        assert a.backend.decisions == [("q1", {"answer": "Rahul K"})] and sp.spoken[-1] == "Done."
    asyncio.run(go())
