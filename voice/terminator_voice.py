#!/usr/bin/env python3
"""Terminator voice service: an always-on, offline voice assistant for your desktop.

  wake word (openWakeWord, "hey jarvis") -> record until you stop talking -> faster-whisper speech-to-text
  -> Terminator over WebSocket (instant quick actions, or the AI agents) -> Piper speech, sentence by sentence.
  Approvals and clarifying questions are asked and answered by voice. Reminders and timers are spoken.

Commands:
  login              link this PC to your Terminator account (stores a revocable device key, never your password)
  run                listen for the wake word (the systemd unit runs this in the background)
  test-mic           record 4 seconds and report whether the microphone signal is usable
  say TEXT           speak TEXT with the configured voice
  simulate WAV       push a recording through the full pipeline instead of the microphone
  install-service    write a systemd --user unit (add --enable to start at login)
"""
import argparse, asyncio, getpass, json, logging, os, re, shutil, socket, subprocess, sys, time, wave
from datetime import date
from pathlib import Path
import numpy as np

log = logging.getLogger("terminator-voice")
CONFIG = Path(os.environ.get("TERMINATOR_VOICE_CONFIG", Path.home() / ".config/terminator/voice.json"))
DATA = Path(os.environ.get("TERMINATOR_VOICE_DATA", Path.home() / ".local/share/terminator-voice"))
RATE, FRAME = 16000, 1280                       # 16 kHz mono, 80 ms frames (what openWakeWord expects)
FRAME_S = FRAME / RATE
DEFAULTS = {"url": "http://127.0.0.1:8000", "wake_word": "hey_jarvis", "threshold": 0.5, "language": "en",
            "whisper_model": "base", "voice": "en_US-lessac-medium", "speech_rate": 1.0, "input_device": "default",
            "follow_up": True}


# ======================================================================================= config
def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG.exists():
        cfg.update(json.loads(CONFIG.read_text()))
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    tmp.chmod(0o600)
    tmp.replace(CONFIG)


# ======================================================================================= text helpers
def speakable(md: str) -> str:
    """Markdown -> something pleasant to hear."""
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", md)
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"`([^`]+)`", r"\1", s)
    s = re.sub(r"^\s{0,3}#{1,6}\s+", "", s, flags=re.M)
    s = re.sub(r"^\s*[-*+]\s+", "", s, flags=re.M)
    s = re.sub(r"^\s*\|.*\|\s*$", "", s, flags=re.M)
    s = re.sub(r"https?://\S+", "link", s)
    s = re.sub(r"[*_~>#|]", "", s)
    return re.sub(r"\s+", " ", s).strip()


class SentenceSplitter:
    """Turns streamed markdown into complete sentences as early as possible; skips fenced code blocks."""
    def __init__(self):
        self.buf, self.in_code, self.said_code = "", False, False

    def feed(self, delta: str) -> list[str]:
        self.buf += delta
        return self._drain(False)

    def flush(self) -> list[str]:
        return self._drain(True)

    def _drain(self, final: bool) -> list[str]:
        out = []
        while True:
            if self.in_code:
                end = self.buf.find("```")
                if end < 0:
                    if final:
                        self.buf = ""
                    return out
                self.buf, self.in_code = self.buf[end + 3:], False
                continue
            fence = self.buf.find("```")
            if fence >= 0:
                out.append(self.buf[:fence])
                if not self.said_code:
                    out.append("I've put the code on screen.")
                    self.said_code = True
                self.buf, self.in_code = self.buf[fence + 3:], True
                continue
            cut = -1
            for m in re.finditer(r"[.!?](?=\s)|\n\n", self.buf):   # terminator followed by whitespace
                cut = m.end()
            if cut > 0:
                out.append(self.buf[:cut])
                self.buf = self.buf[cut:]
            if final:
                out.append(self.buf)
                self.buf = ""
            return [s for s in (speakable(x) for x in out) if s]


YES = re.compile(r"^(yes|yeah|yep|yup|sure|ok|okay|go ahead|do it|please do|confirm|approved?|affirmative|run it|haan|ha|ji)\b", re.I)
NO = re.compile(r"^(no|nope|nah|don'?t|do not|cancel|stop|deny|negative|never ?mind|nahi|na)\b", re.I)
WAKE_PREFIX = re.compile(r"^\W*(hey|hi|ok|okay)?\W*(jarvis|terminator)\b\W*", re.I)
# Whisper sometimes "hears" these in noise or silence
HALLUCINATIONS = {"", "you", "thank you", "thanks", "thank you for watching", "thanks for watching", "bye",
                  "subtitles by the amara.org community", "so", "okay"}


def parse_yes_no(text: str) -> bool | None:
    t = WAKE_PREFIX.sub("", text.strip())
    if NO.match(t):
        return False
    if YES.match(t):
        return True
    return None


def describe_action(ev: dict) -> str:
    a = ev.get("args") or {}
    if ev.get("name") in ("run_shell", "run_command"):
        cmd = str(a.get("command", ""))
        return "run this command: " + (cmd if len(cmd) < 140 else cmd[:140] + ", and so on")
    if ev.get("name") in ("whatsapp_send", "teams_message", "instagram_send"):
        kind = {"whatsapp_send": "WhatsApp", "teams_message": "Teams message", "instagram_send": "Instagram message"}[ev["name"]]
        return f"send a {kind} to {a.get('to')} saying {str(a.get('message', ''))[:120]}"
    if ev.get("name") == "email_send":
        return f"email {a.get('to')} with the subject {a.get('subject')}"
    if ev.get("name") == "close_app":
        return f"{'force close' if a.get('force') else 'close'} {a.get('name', 'that app')}; unsaved changes in it could be lost"
    if ev.get("name") == "power_action":
        act = {"shutdown": "shut down", "restart": "restart", "logout": "log you out of", "suspend": "suspend"}.get(a.get("action"), "shut down")
        keep = a.get("save_environment", True) and a.get("action") != "suspend"
        return f"{act} the computer" + (", after saving your open apps and tabs" if keep else "")
    if ev.get("name") == "read_log":
        return f"read the log file {a.get('path')}"
    return f"use {str(ev.get('name', 'a tool')).replace('_', ' ')}"


# ======================================================================================= audio out
def tone(freqs: list[float], ms: int = 110, rate: int = 22050, vol: float = 0.22) -> bytes:
    parts = []
    for f in freqs:
        t = np.arange(int(rate * ms / 1000)) / rate
        env = np.minimum(1, np.minimum(t, t[::-1]) * 40)        # 25 ms fade in/out, no clicks
        parts.append(np.sin(2 * np.pi * f * t) * env * vol)
    return (np.concatenate(parts) * 32767).astype(np.int16).tobytes()


CHIMES = {"listen": tone([660, 880]), "done": tone([880, 660]), "notify": tone([784, 988, 1175], 90),
          "error": tone([330, 247], 160)}


class Speaker:
    """Serial speech queue: synthesize with Piper, play with aplay. stop() interrupts immediately (barge-in)."""

    def __init__(self, voice_name: str, rate: float = 1.0, dry_run: bool = False):
        self.dry_run, self.rate = dry_run, rate
        self.queue: asyncio.Queue = asyncio.Queue()
        self.proc: asyncio.subprocess.Process | None = None
        self.gen = 0
        self.idle = asyncio.Event()
        self.idle.set()
        self.spoken: list[str] = []                 # transcript of what was said (dry-run / tests)
        self.voice = self.sample_rate = None
        path = DATA / f"{voice_name}.onnx"
        if not dry_run:
            try:
                from piper import PiperVoice
                self.voice = PiperVoice.load(str(path))
                self.sample_rate = self.voice.config.sample_rate
            except Exception as e:
                log.warning("Piper voice unavailable (%s); falling back to spd-say", e)
        self.task = asyncio.create_task(self._worker())

    def say(self, text: str) -> None:
        t = speakable(text)
        if t:
            self.idle.clear()
            self.queue.put_nowait((self.gen, "say", t))

    def chime(self, name: str) -> None:
        self.idle.clear()
        self.queue.put_nowait((self.gen, "chime", name))

    def stop(self) -> None:
        self.gen += 1
        while not self.queue.empty():
            self.queue.get_nowait()
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
        self.idle.set()

    async def wait_idle(self) -> None:
        await self.idle.wait()

    @property
    def speaking(self) -> bool:
        return not self.idle.is_set()

    def _synth(self, text: str) -> bytes:
        try:
            from piper import SynthesisConfig
            cfg = SynthesisConfig(length_scale=1 / max(0.5, self.rate))
            return b"".join(c.audio_int16_bytes for c in self.voice.synthesize(text, syn_config=cfg))
        except ImportError:
            return b"".join(c.audio_int16_bytes for c in self.voice.synthesize(text))

    async def _play(self, pcm: bytes, rate: int) -> None:
        self.proc = await asyncio.create_subprocess_exec("aplay", "-q", "-t", "raw", "-f", "S16_LE", "-r", str(rate),
                                                         "-c", "1", stdin=asyncio.subprocess.PIPE,
                                                         stderr=asyncio.subprocess.DEVNULL)
        try:
            self.proc.stdin.write(pcm)
            await self.proc.stdin.drain()
            self.proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            pass
        await self.proc.wait()

    async def _worker(self) -> None:
        while True:
            gen, kind, payload = await self.queue.get()
            try:
                if gen != self.gen:
                    continue
                if self.dry_run:
                    self.spoken.append(payload if kind == "say" else f"[{payload} chime]")
                    log.info("SAY: %s", self.spoken[-1])
                elif kind == "chime":
                    await self._play(CHIMES[payload], 22050)
                elif self.voice is not None:
                    pcm = await asyncio.to_thread(self._synth, payload)
                    if gen == self.gen:
                        await self._play(pcm, self.sample_rate)
                else:
                    self.proc = await asyncio.create_subprocess_exec("spd-say", "-w", payload)
                    await self.proc.wait()
            except Exception:
                log.exception("speech failed")
            finally:
                if self.queue.empty():
                    self.idle.set()


# ======================================================================================= audio in
class MicSource:
    def __init__(self, device: str = "default"):
        self.device = device

    async def frames(self):
        proc = await asyncio.create_subprocess_exec("arecord", "-q", "-D", self.device, "-f", "S16_LE", "-r", str(RATE),
                                                    "-c", "1", "-t", "raw", stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)
        try:
            while True:
                data = await proc.stdout.readexactly(FRAME * 2)
                yield np.frombuffer(data, dtype=np.int16)
        except asyncio.IncompleteReadError:
            err = (await proc.stderr.read()).decode(errors="replace").strip()
            raise RuntimeError(f"Microphone stopped: {err or 'arecord exited'}")
        finally:
            if proc.returncode is None:
                proc.kill()


def read_wav_16k(path: str) -> np.ndarray:
    with wave.open(path, "rb") as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise SystemExit("WAV must be 16-bit PCM")
    a = np.frombuffer(raw, dtype=np.int16).reshape(-1, ch).mean(axis=1)
    if rate != RATE:
        a = np.interp(np.arange(0, len(a), rate / RATE), np.arange(len(a)), a)
    return a.astype(np.int16)


class FileSource:
    """Feeds a recording in real time (80 ms per frame) followed by silence, like a microphone would."""
    def __init__(self, audio: np.ndarray, tail_s: float = 3.0):
        pad = (-len(audio)) % FRAME
        self.audio = np.concatenate([np.zeros(RATE // 2, np.int16), audio, np.zeros(pad + int(tail_s * RATE), np.int16)])
        self.finished = asyncio.Event()

    async def frames(self):
        for i in range(0, len(self.audio), FRAME):
            yield self.audio[i:i + FRAME]
            await asyncio.sleep(FRAME_S)
        self.finished.set()
        while True:                                    # keep "listening" to silence until the test ends
            yield np.zeros(FRAME, np.int16)
            await asyncio.sleep(FRAME_S)


def rms(frame: np.ndarray) -> float:
    return float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))


class Endpointer:
    """Energy-based start/end-of-speech detection relative to the measured background noise."""
    def __init__(self, noise: float, start_timeout: float, end_silence: float = 0.8, max_len: float = 15.0,
                 ignore_first: float = 0.2, already_speaking: bool = False):
        self.thr = max(noise * 3.0, 250.0)
        self.start_timeout, self.end_silence, self.max_len, self.ignore_first = start_timeout, end_silence, max_len, ignore_first
        self.t = self.silence = 0.0
        self.started, self.speech_frames = already_speaking, 0

    def feed(self, frame: np.ndarray) -> str:
        self.t += FRAME_S
        level = rms(frame)
        if self.t < self.ignore_first and not self.started:
            return "waiting"
        if not self.started:
            if level > self.thr:
                self.started, self.speech_frames = True, 1
                return "speech"
            return "timeout" if self.t > self.start_timeout else "waiting"
        if level > self.thr * 0.7:
            self.silence, self.speech_frames = 0.0, self.speech_frames + 1
        else:
            self.silence += FRAME_S
        if self.silence >= self.end_silence or self.t >= self.max_len:
            return "done"
        return "speech"


class Ears:
    def __init__(self, cfg: dict):
        from openwakeword.model import Model
        from faster_whisper import WhisperModel
        self.wake = Model(wakeword_models=[cfg["wake_word"]], inference_framework="onnx")
        try:    # offline first: no network call on every start once the model is downloaded
            self.whisper = WhisperModel(cfg["whisper_model"], device="cpu", compute_type="int8",
                                        download_root=str(DATA / "whisper"), local_files_only=True)
        except Exception:
            self.whisper = WhisperModel(cfg["whisper_model"], device="cpu", compute_type="int8", download_root=str(DATA / "whisper"))
        self.language = None if cfg["language"] in ("", "auto") else cfg["language"]

    def wake_score(self, frame: np.ndarray) -> float:
        return max(self.wake.predict(frame).values())

    def reset(self) -> None:
        self.wake.reset()

    def transcribe(self, audio: np.ndarray) -> str:
        segments, _ = self.whisper.transcribe(audio.astype(np.float32) / 32768.0, language=self.language, beam_size=1,
                                              best_of=1, temperature=0.0, condition_on_previous_text=False,
                                              without_timestamps=True, vad_filter=False)
        text = " ".join(s.text.strip() for s in segments).strip()
        return "" if text.lower().strip(" .!?,") in HALLUCINATIONS else text


# ======================================================================================= backend
class Backend:
    def __init__(self, url: str, token: str):
        import httpx
        self.url, self.token = url.rstrip("/"), token
        self.http = httpx.AsyncClient(base_url=self.url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        self.ws = None
        self.connected = asyncio.Event()
        self.subs: dict[str, set[asyncio.Queue]] = {}
        self.on_notification = lambda item: None
        self.fatal: str | None = None

    async def run(self) -> None:
        import websockets
        ws_url = re.sub(r"^http", "ws", self.url) + "/api/ws"
        backoff = 1
        while True:
            try:
                async with websockets.connect(ws_url, additional_headers={"Authorization": f"Bearer {self.token}"},
                                              ping_interval=20, max_size=8_000_000) as ws:
                    self.ws, backoff = ws, 1
                    self.connected.set()
                    log.info("connected to %s", self.url)
                    async for raw in ws:
                        ev = json.loads(raw)
                        if ev.get("type") == "notification":
                            self.on_notification(ev["item"])
                        for q in list(self.subs.get(ev.get("conversation_id") or "", ())):
                            q.put_nowait(ev)
            except websockets.exceptions.ConnectionClosed as e:
                if e.rcvd and e.rcvd.code == 4401:
                    self.fatal = "Terminator rejected this device key (revoked?). Run: terminator-voice login"
                    log.error(self.fatal)
                    return
                log.warning("connection closed (%s); retrying in %ss", e, backoff)
            except (OSError, asyncio.TimeoutError) as e:
                log.warning("cannot reach Terminator at %s (%s); retrying in %ss", self.url, type(e).__name__, backoff)
            except Exception as e:      # websockets.ConnectionClosed / InvalidStatus etc.
                log.warning("connection lost (%s); retrying in %ss", e, backoff)
            self.connected.clear()
            self.ws = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def send(self, msg: dict) -> None:
        await asyncio.wait_for(self.connected.wait(), 10)
        await self.ws.send(json.dumps(msg))

    def subscribe(self, cid: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.subs.setdefault(cid, set()).add(q)
        return q

    def unsubscribe(self, cid: str, q: asyncio.Queue) -> None:
        self.subs.get(cid, set()).discard(q)

    async def conversation(self, fresh: bool = False) -> str:
        """One conversation per day, titled 'Voice <date>', so history stays readable in the app."""
        state_file = CONFIG.with_name(CONFIG.stem + ".state.json")   # per config, so test setups never mix
        state = json.loads(state_file.read_text()) if state_file.exists() else {}
        today = date.today().isoformat()
        if not fresh and state.get("date") == today and state.get("conversation_id"):
            return state["conversation_id"]
        r = await self.http.post("/api/conversations", json={"title": f"Voice {today}"})
        r.raise_for_status()
        cid = r.json()["id"]
        await self.http.patch(f"/api/conversations/{cid}", json={"title": f"Voice {today}"})
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps({"date": today, "conversation_id": cid}))
        return cid

    async def decide(self, run_id: str, **body) -> dict:
        r = await self.http.post(f"/api/tools/runs/{run_id}/decision", json=body)
        r.raise_for_status()
        return r.json()


# ======================================================================================= the assistant
class Assistant:
    def __init__(self, cfg: dict, source, speaker: Speaker, ears: Ears, backend: Backend):
        self.cfg, self.source, self.speaker, self.ears, self.backend = cfg, source, speaker, ears, backend
        self.listen_q: asyncio.Queue | None = None
        self.preroll: list[np.ndarray] = []
        self.noise = 300.0
        self.busy: asyncio.Task | None = None
        self.current_cid: str | None = None
        self.cooldown_until = 0.0
        self.turns_done = 0
        backend.on_notification = self._notification

    def _notification(self, item: dict) -> None:
        log.info("notification: %s", item.get("title"))
        self.speaker.chime("notify")
        self.speaker.say(f"{item.get('title', '')}. {item.get('body', '')}")

    async def dispatch(self) -> None:
        """Single consumer of microphone frames: wake-word detection, or feeding the active listener."""
        async for frame in self.source.frames():
            self.preroll = (self.preroll + [frame])[-4:]
            if self.listen_q is not None:
                self.listen_q.put_nowait(frame)
                continue
            level = rms(frame)
            if level < self.noise * 3 or self.noise < 50:
                self.noise = 0.97 * self.noise + 0.03 * level
            score = self.ears.wake_score(frame)
            if score >= self.cfg["threshold"] and time.monotonic() > self.cooldown_until:
                log.info("wake word (%.2f)", score)
                self.ears.reset()
                self.cooldown_until = time.monotonic() + 1.5
                self.listen_q = asyncio.Queue()                # capture from this frame on: nothing is lost
                for f in self.preroll[-3:]:
                    self.listen_q.put_nowait(f)
                if self.busy and not self.busy.done():          # barge-in: interrupt speech and the running turn
                    self.speaker.stop()
                    self.busy.cancel()
                    if self.current_cid:
                        asyncio.create_task(self.backend.send({"type": "stop", "conversation_id": self.current_cid}))
                else:
                    self.speaker.stop()
                self.busy = asyncio.create_task(self.on_wake())

    async def listen(self, start_timeout: float = 6.0, chime: bool = True) -> str:
        if self.listen_q is None:
            self.listen_q = asyncio.Queue()
        q = self.listen_q            # local: a barge-in may install a newer queue while this one is cancelled
        if chime:
            self.speaker.chime("listen")
        ep = Endpointer(self.noise, start_timeout)
        kept: list[np.ndarray] = []
        try:
            while True:
                frame = await q.get()
                state = ep.feed(frame)
                kept.append(frame)
                if state in ("done", "timeout"):
                    break
        finally:
            if self.listen_q is q:
                self.listen_q = None
        if not ep.started or ep.speech_frames < 3:
            log.info("heard nothing")
            return ""
        text = await asyncio.to_thread(self.ears.transcribe, np.concatenate(kept))
        text = WAKE_PREFIX.sub("", text).strip()
        log.info("heard: %r", text)
        return text

    async def on_wake(self) -> None:
        try:
            text = await self.listen(chime=True)
            if not text:
                self.speaker.chime("done")
                return
            await self.handle(text)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("turn failed")
            self.speaker.chime("error")
            self.speaker.say("Sorry, something went wrong." if not isinstance(e, asyncio.TimeoutError)
                             else "I can't reach Terminator right now.")
        finally:
            self.turns_done += 1

    async def handle(self, text: str) -> None:
        if re.fullmatch(r"(stop|cancel|never ?mind|shut up|be quiet|quiet|that'?s all)[.!]?", text.strip(), re.I):
            self.speaker.stop()
            return
        reply, pending = await self.turn({"message": text})
        if pending:
            await self.resolve(pending)
        elif self.cfg.get("follow_up") and reply.rstrip().endswith("?"):
            await self.speaker.wait_idle()                     # follow-up mode: answer without the wake word
            follow = await self.listen(start_timeout=5)
            if follow:
                await self.handle(follow)

    async def turn(self, payload: dict, retry: bool = True) -> tuple[str, list]:
        cid = await self.backend.conversation()
        self.current_cid = cid
        q = self.backend.subscribe(cid)
        splitter, text, pending, waited = SentenceSplitter(), "", [], False
        try:
            await self.backend.send({"type": "chat", "conversation_id": cid, **payload})
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), 3.0 if not (text or waited) else 900)
                except asyncio.TimeoutError:
                    if not (text or waited):
                        waited = True
                        self.speaker.say("Working on it.")       # immediate feedback while the model thinks
                        continue
                    raise
                kind = ev["type"]
                if kind == "text":
                    text += ev["delta"]
                    for s in splitter.feed(ev["delta"]):
                        self.speaker.say(s)
                elif kind == "tool_start" and ev.get("retract"):
                    self.speaker.stop()                        # model wrote a tool call as text: don't read it out
                    splitter = SentenceSplitter()
                elif kind in ("approval_required", "question"):
                    pending.append(ev)
                elif kind == "error":
                    if ev.get("status") == 404 and retry:       # conversation was deleted in the app
                        await self.backend.conversation(fresh=True)
                        return await self.turn(payload, retry=False)
                    self.speaker.say(f"Sorry. {ev.get('message', 'Something went wrong.')}")
                    if "status" in ev:                         # rejected before starting: no 'end' follows
                        break
                elif kind == "end":
                    break
            for s in splitter.flush():
                self.speaker.say(s)
            return text, pending
        finally:
            self.backend.unsubscribe(cid, q)

    async def resolve(self, pending: list) -> None:
        for p in pending:
            await self.speaker.wait_idle()
            if p["type"] == "question":
                args = p.get("args") or {}
                opts = args.get("options") or []
                self.speaker.say(args.get("question", "I have a question.") +
                                 (f" You can say {', '.join(opts[:-1])} or {opts[-1]}." if len(opts) > 1 else ""))
                await self.speaker.wait_idle()
                answer = await self.listen(start_timeout=7)
                if not answer:
                    self.speaker.say("I'll leave that question in the app for you.")
                    return
                r = await self.backend.decide(p["run_id"], answer=answer)
            else:
                self.speaker.say(f"I need your permission to {describe_action(p)}. Should I go ahead?")
                await self.speaker.wait_idle()
                answer = await self.listen(start_timeout=7)
                if not answer:
                    self.speaker.say("I didn't hear an answer, so I've left it in the app for you.")
                    return
                approve = parse_yes_no(answer)
                if approve is None:
                    self.speaker.say("I'll take that as a no.")
                r = await self.backend.decide(p["run_id"], approve=bool(approve))
            if r.get("pending_remaining") == 0:
                _, more = await self.turn({"mode": "continue"})
                if more:
                    await self.resolve(more)


# ======================================================================================= commands
async def cmd_run(cfg: dict, source=None, dry_run: bool = False, until_turns: int | None = None,
                  linger: float = 0.0) -> Assistant:
    if not cfg.get("token"):
        raise SystemExit("Not linked to Terminator yet. Run: terminator-voice login")
    backend = Backend(cfg["url"], cfg["token"])
    speaker = Speaker(cfg["voice"], cfg.get("speech_rate", 1.0), dry_run=dry_run)
    log.info("loading wake word and speech models...")
    ears = await asyncio.to_thread(Ears, cfg)
    assistant = Assistant(cfg, source or MicSource(cfg["input_device"]), speaker, ears, backend)
    bt = asyncio.create_task(backend.run())
    log.info('ready - say "%s"', cfg["wake_word"].replace("_", " "))
    if not dry_run:
        speaker.chime("listen")
    dt = asyncio.create_task(assistant.dispatch())
    try:
        while True:
            await asyncio.sleep(0.2)
            if backend.fatal:
                raise SystemExit(backend.fatal)
            if dt.done():
                dt.result()                                    # surface microphone errors
            if until_turns is not None and assistant.turns_done >= until_turns and \
                    (not assistant.busy or assistant.busy.done()) and not speaker.speaking:
                if linger > 0:                                 # keep listening for spoken notifications
                    await asyncio.sleep(linger)
                    await speaker.wait_idle()
                return assistant
    finally:
        dt.cancel()
        bt.cancel()


async def cmd_login(url: str, email: str | None, password_stdin: bool) -> None:
    import httpx
    url = url.rstrip("/")
    email = email or input("Terminator email: ").strip()
    password = sys.stdin.readline().rstrip("\n") if password_stdin else getpass.getpass("Password (not stored): ")
    async with httpx.AsyncClient(base_url=url, timeout=20) as http:
        r = await http.post("/api/auth/login", json={"email": email, "password": password})
        if r.status_code != 200:
            raise SystemExit(f"Login failed: {r.json().get('detail', r.text)}")
        session = r.json()["token"]
        r = await http.post("/api/auth/device-tokens", json={"name": f"Voice on {socket.gethostname()}"},
                            headers={"Authorization": f"Bearer {session}"})
        r.raise_for_status()
    cfg = load_config()
    cfg.update({"url": url, "token": r.json()["token"]})
    save_config(cfg)
    print(f"Linked. Device key saved to {CONFIG} (revoke anytime in Settings → Voice devices).")


async def cmd_test_mic(cfg: dict) -> None:
    print("Recording 4 seconds - say something...")
    frames = []
    async for f in MicSource(cfg["input_device"]).frames():
        frames.append(f)
        if len(frames) * FRAME_S >= 4:
            break
    a = np.concatenate(frames).astype(np.int32)
    levels = [rms(f) for f in frames]
    clipped = float(np.mean(np.abs(a) >= 32700))
    print(f"level: quietest {min(levels):.0f}, median {np.median(levels):.0f}, loudest {max(levels):.0f} | clipped {clipped:.0%}")
    if max(levels) < 3:
        print("✗ Silence - no microphone signal. Check it is plugged in and not muted (Settings → Sound → Input).")
    elif clipped > 0.05 or np.median(levels) > 8000:
        print("✗ The signal is clipping/saturated. Lower the input volume and Mic Boost "
              "(Settings → Sound → Input, or `alsamixer` → F4), and make sure a microphone is actually connected.")
    elif max(levels) < 4 * min(levels):
        print("? Signal present but speech is barely above noise - move closer or raise input volume a bit.")
    else:
        print("✓ Microphone looks usable.")


async def cmd_say(cfg: dict, text: str) -> None:
    sp = Speaker(cfg["voice"], cfg.get("speech_rate", 1.0))
    splitter = SentenceSplitter()
    for s in splitter.feed(text) + splitter.flush():
        sp.say(s)
    await asyncio.sleep(0.05)
    await sp.wait_idle()


def cmd_install_service(enable: bool) -> None:
    unit_dir = Path.home() / ".config/systemd/user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit = unit_dir / "terminator-voice.service"
    here = Path(__file__).resolve().parent
    unit.write_text(f"""[Unit]
Description=Terminator voice assistant (wake word)
After=pipewire.service pipewire-pulse.service

[Service]
ExecStart={here / '.venv/bin/python'} {here / 'terminator_voice.py'} run
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
""")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    print(f"Wrote {unit}")
    if enable:
        subprocess.run(["systemctl", "--user", "enable", "--now", "terminator-voice"], check=False)
        print("Enabled: starts at login. Logs: journalctl --user -u terminator-voice -f")
    else:
        print("Start now:        systemctl --user start terminator-voice\n"
              "Start at login:   systemctl --user enable terminator-voice\n"
              "Logs:             journalctl --user -u terminator-voice -f")


def main() -> None:
    p = argparse.ArgumentParser(prog="terminator-voice", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    lg = sub.add_parser("login")
    lg.add_argument("--url", default=load_config()["url"])
    lg.add_argument("--email")
    lg.add_argument("--password-stdin", action="store_true")
    sub.add_parser("run")
    sub.add_parser("test-mic")
    sp = sub.add_parser("say")
    sp.add_argument("text")
    sm = sub.add_parser("simulate")
    sm.add_argument("wav")
    sm.add_argument("--speak", action="store_true", help="play the reply aloud instead of printing it")
    sm.add_argument("--linger", type=float, default=0.0, help="seconds to keep running afterwards (to hear reminders)")
    inst = sub.add_parser("install-service")
    inst.add_argument("--enable", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    if args.cmd == "login":
        asyncio.run(cmd_login(args.url, args.email, args.password_stdin))
    elif args.cmd == "run":
        asyncio.run(cmd_run(cfg))
    elif args.cmd == "test-mic":
        asyncio.run(cmd_test_mic(cfg))
    elif args.cmd == "say":
        asyncio.run(cmd_say(cfg, args.text))
    elif args.cmd == "simulate":
        async def sim():
            a = await cmd_run(cfg, FileSource(read_wav_16k(args.wav)), dry_run=not args.speak, until_turns=1,
                              linger=args.linger)
            print(json.dumps({"spoken": a.speaker.spoken}, indent=1))
        asyncio.run(asyncio.wait_for(sim(), 900))
    elif args.cmd == "install-service":
        cmd_install_service(args.enable)


if __name__ == "__main__":
    main()
