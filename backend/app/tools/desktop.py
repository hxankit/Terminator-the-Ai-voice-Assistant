"""Tools that act on this computer's desktop session. All are logged; run_shell needs approval every time."""
import asyncio, os, re, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import quote, quote_plus
import httpx
from pydantic import BaseModel, Field, model_validator
from .. import desktop, contacts
from ..config import get_settings
from ..db import db, now
from .base import tool, ToolError, ToolContext

NEEDS_DESKTOP = desktop.unavailable


def local_time(dt: datetime) -> str:
    return dt.astimezone().strftime("%-I:%M %p").lstrip("0")


# ---------------- apps, websites, files ----------------
class AppArgs(BaseModel):
    name: str = Field(min_length=1, max_length=100, description="App name as the user said it, e.g. 'chrome', 'files', 'calculator'")


@tool("open_app", "Open an installed application on this computer by name.", AppArgs, risk="write", requires=NEEDS_DESKTOP)
async def open_app(a: AppArgs, ctx: ToolContext):
    app, alts = desktop.match_app(a.name)
    if not app:
        sugg = ", ".join(x["name"] for x in alts)
        raise ToolError(f"No installed app clearly matches '{a.name}'." + (f" Did you mean: {sugg}?" if sugg else ""))
    desktop.launch_app(app)
    return {"opened": app["name"]}


class OpenWithArgs(BaseModel):
    app: str = Field(min_length=1, max_length=100, description="Application, e.g. 'vscode', 'files', 'gimp'")
    path: str = Field(min_length=1, max_length=1000,
                      description="Full path, ~/path, or just a folder name to look up in the home folder")


def resolve_target(raw: str) -> str:
    if raw.startswith(("/", "~")):
        p = Path(raw).expanduser().resolve()
        if not any(str(p) == r or str(p).startswith(r + "/") for r in ALLOWED_FILE_ROOTS):
            raise ToolError("Only files in your home folder or on external drives can be opened")
        if not p.exists():
            raise ToolError(f"{p} does not exist")
        return str(p)
    path, candidates = desktop.resolve_dir(raw)
    if path:
        return path
    if candidates:
        raise ToolError(f"Several folders match '{raw}': " + ", ".join(candidates[:5]) + ". Which one?")
    raise ToolError(f"I couldn't find a folder called '{raw}' in your home folder.")


@tool("open_with", "Open a folder or file in a specific application, e.g. a project folder in VS Code. "
      "`path` may be a full path or just a folder name (it is looked up in the home folder).",
      OpenWithArgs, risk="write", requires=NEEDS_DESKTOP)
async def open_with(a: OpenWithArgs, ctx: ToolContext):
    app, alts = desktop.match_app(a.app)
    if not app:
        raise ToolError(f"No installed app clearly matches '{a.app}'." +
                        (f" Did you mean: {', '.join(x['name'] for x in alts)}?" if alts else ""))
    target = resolve_target(a.path)
    desktop.launch_app(app, [target])
    return {"opened": target, "app": app["name"]}


class ListAppsArgs(BaseModel):
    query: str = Field("", max_length=100)


@tool("list_apps", "List installed applications (optionally filtered).", ListAppsArgs, requires=NEEDS_DESKTOP)
async def list_apps(a: ListAppsArgs, ctx: ToolContext):
    q = a.query.lower()
    names = [x["name"] for x in desktop.list_apps() if q in x["name"].lower() or q in x["generic"].lower()]
    return names[:150]


class UrlArgs(BaseModel):
    url: str = Field(max_length=2000)


@tool("open_url", "Open a website in the user's default browser.", UrlArgs, risk="write", requires=NEEDS_DESKTOP)
async def open_url(a: UrlArgs, ctx: ToolContext):
    url = a.url.strip()
    if not re.match(r"^https?://", url):
        if re.match(r"^[\w.-]+\.[a-z]{2,}(/.*)?$", url, re.I):
            url = "https://" + url
        else:
            raise ToolError("Only http(s) web addresses can be opened")
    desktop.open_target(url)
    return {"opened": url}


class YouTubeArgs(BaseModel):
    query: str = Field(min_length=1, max_length=200)


async def first_youtube_video(query: str) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=6, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en"}) as c:
            r = await c.get("https://www.youtube.com/results", params={"search_query": query})
        m = re.search(r'"videoId":"([\w-]{11})"', r.text)
        return f"https://www.youtube.com/watch?v={m.group(1)}" if m else None
    except httpx.HTTPError:
        return None


@tool("play_youtube", "Play a song or video on YouTube in the browser (opens the top result).", YouTubeArgs,
      risk="write", requires=NEEDS_DESKTOP)
async def play_youtube(a: YouTubeArgs, ctx: ToolContext):
    url = await first_youtube_video(a.query) or f"https://www.youtube.com/results?search_query={quote_plus(a.query)}"
    desktop.open_target(url)
    return {"playing": a.query, "url": url}


ALLOWED_FILE_ROOTS = (str(Path.home()), "/media", "/mnt")


class OpenFileArgs(BaseModel):
    path: str = Field(max_length=1000, description="Absolute path or ~/... path of a file or folder")


@tool("open_file", "Open a file or folder with its default application (within the user's home or external drives).",
      OpenFileArgs, risk="write", requires=NEEDS_DESKTOP)
async def open_file(a: OpenFileArgs, ctx: ToolContext):
    p = Path(a.path).expanduser().resolve()
    if not any(str(p) == r or str(p).startswith(r + "/") for r in ALLOWED_FILE_ROOTS):
        raise ToolError("Only files in your home folder or on external drives can be opened")
    if not p.exists():
        raise ToolError(f"{p} does not exist")
    desktop.open_target(str(p))
    return {"opened": str(p)}


SKIP_DIRS = {"node_modules", ".venv", "venv", "__pycache__", ".git", ".cache", "snap", ".local", ".npm", "site-packages"}


class FindArgs(BaseModel):
    query: str = Field(min_length=1, max_length=100, description="Words in the file name, e.g. 'invoice pdf'")
    folder: str | None = Field(None, max_length=500, description="Only search inside this folder, e.g. ~/Downloads")
    limit: int = Field(15, ge=1, le=50)


@tool("find_files", "Search file and folder names in the user's home folder. Returns newest matches first.", FindArgs)
async def find_files(a: FindArgs, ctx: ToolContext):
    words = [w for w in re.split(r"\s+", a.query.lower()) if w]
    start = Path(a.folder).expanduser().resolve() if a.folder else Path.home()
    if not any(str(start) == r or str(start).startswith(r + "/") for r in ALLOWED_FILE_ROOTS):
        raise ToolError("I can only search your home folder and external drives")
    if not start.is_dir():
        raise ToolError(f"{start} is not a folder")
    def walk():
        hits, deadline = [], time.monotonic() + 3
        for root, dirs, files in os.walk(start):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS]
            for d in dirs:
                if all(w in d.lower() for w in words):
                    p = os.path.join(root, d)
                    try:
                        hits.append((os.stat(p).st_mtime, p + "/", 0))
                    except OSError:
                        pass
            for f in files:
                if all(w in f.lower() for w in words):
                    p = os.path.join(root, f)
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    hits.append((st.st_mtime, p, st.st_size))
            if time.monotonic() > deadline:
                break
        hits.sort(reverse=True)
        return hits[:a.limit]
    hits = await asyncio.to_thread(walk)
    return [{"path": p.rstrip("/"), "type": "folder" if p.endswith("/") else "file", "size_kb": round(s / 1024, 1),
             "modified": datetime.fromtimestamp(m).strftime("%Y-%m-%d %H:%M")} for m, p, s in hits] or "No matching files or folders found."


# ---------------- audio, media, session ----------------
class VolumeArgs(BaseModel):
    level: int | None = Field(None, ge=0, le=150, description="Set to this percent")
    change: int | None = Field(None, ge=-100, le=100, description="Relative change in percent, e.g. 10 or -10")
    mute: bool | None = Field(None, description="true to mute, false to unmute")

    @model_validator(mode="after")
    def _one(self):
        if self.level is None and self.change is None and self.mute is None:
            raise ValueError("give level, change or mute")
        return self


@tool("set_volume", "Change the computer's speaker volume or mute/unmute it.", VolumeArgs, risk="write", requires=NEEDS_DESKTOP)
async def set_volume(a: VolumeArgs, ctx: ToolContext):
    try:
        return await desktop.set_volume(a.level, a.change, a.mute)
    except RuntimeError as e:
        raise ToolError(str(e))


class MediaArgs(BaseModel):
    action: Literal["play_pause", "play", "pause", "next", "previous", "stop"]


@tool("media_control", "Control the active media player (Spotify, browser video, etc.): play, pause, next, previous.",
      MediaArgs, risk="write", requires=NEEDS_DESKTOP)
async def media_control(a: MediaArgs, ctx: ToolContext):
    try:
        player = await desktop.media_control(a.action)
    except RuntimeError as e:
        raise ToolError(str(e))
    return {"done": a.action, "player": player}


class Empty(BaseModel):
    pass


@tool("lock_screen", "Lock the computer screen.", Empty, risk="write", requires=NEEDS_DESKTOP)
async def lock_screen(a: Empty, ctx: ToolContext):
    try:
        await desktop.lock_screen()
    except RuntimeError as e:
        raise ToolError(str(e))
    return {"locked": True}


class NotifyArgs(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    body: str = Field("", max_length=500)


@tool("show_notification", "Show a desktop notification.", NotifyArgs, risk="write", requires=NEEDS_DESKTOP)
async def show_notification(a: NotifyArgs, ctx: ToolContext):
    await desktop.notify(a.title, a.body)
    return {"shown": True}


class TimerArgs(BaseModel):
    seconds: int = Field(ge=1, le=7 * 24 * 3600, description="Timer length in seconds (5 minutes = 300)")
    label: str = Field("Timer", max_length=100)


@tool("set_timer", "Start a countdown timer; the user is alerted (spoken + notification) when it ends.", TimerArgs, risk="write")
async def set_timer(a: TimerArgs, ctx: ToolContext):
    due = now() + timedelta(seconds=a.seconds)
    r = await db().tasks.insert_one({"user_id": ctx.user["_id"], "title": a.label, "kind": "timer", "notes": "",
                                     "priority": "normal", "status": "open", "due_at": due, "created_at": now()})
    return {"timer_id": str(r.inserted_id), "label": a.label, "ends_at": local_time(due), "seconds": a.seconds}


# ---------------- contacts & WhatsApp ----------------
class ContactSearchArgs(BaseModel):
    query: str = Field(min_length=1, max_length=100)


@tool("contacts_search", "Look up the user's contacts by name.", ContactSearchArgs)
async def contacts_search(a: ContactSearchArgs, ctx: ToolContext):
    hits = await contacts.resolve(ctx.user["_id"], a.query)
    return [{"name": c["name"], "phones": c.get("phones", []), "emails": c.get("emails", [])} for c in hits] \
        or f"No contact matches '{a.query}'."


def _looks_like_phone(s: str) -> bool:
    return bool(re.fullmatch(r"[+\d][\d\s().-]{5,}", s.strip()))


async def resolve_recipient(user: dict, to: str) -> tuple[str, str]:
    """(display name, international digits) for a contact name or phone number."""
    cc = (user.get("prefs") or {}).get("default_country_code", "")
    try:
        if _looks_like_phone(to):
            return to.strip(), contacts.normalize_phone(to, cc)
        hits = await contacts.resolve(user["_id"], to)
        if not hits:
            raise ToolError(f"I don't have a contact called '{to}'. Add them in Contacts, or give me the number.")
        if len(hits) > 1 and hits[0]["name"].lower() != to.strip().lower():
            raise ToolError("Several contacts match: " + ", ".join(h["name"] for h in hits[:5]) + ". Which one?")
        c = hits[0]
        if not c.get("phones"):
            raise ToolError(f"{c['name']} has no phone number saved.")
        return c["name"], contacts.normalize_phone(c["phones"][0], cc)
    except contacts.ContactError as e:
        raise ToolError(str(e))


class WhatsAppArgs(BaseModel):
    to: str = Field(min_length=1, max_length=100, description="Contact or group name (as saved) or phone number")
    message: str = Field(min_length=1, max_length=4000)
    draft: bool = Field(False, description="Only open WhatsApp with the text typed in instead of sending")


async def _wa_recipient(user: dict, to: str) -> tuple[str, str]:
    """(display name, bridge recipient: digits or JID). Terminator contacts first, then WhatsApp's own names."""
    from ..integrations import whatsapp as wa
    try:
        return await resolve_recipient(user, to)
    except ToolError as first_error:
        if _looks_like_phone(to):
            raise
        hits = await asyncio.to_thread(wa.find_contacts, to)
        if not hits:
            raise first_error
        top = [h for h in hits if h["score"] == hits[0]["score"]]
        if len(top) > 1:
            raise ToolError("Several WhatsApp chats match: " + ", ".join(h["name"] for h in top[:5]) + ". Which one?")
        return hits[0]["name"], hits[0]["jid"]


@tool("whatsapp_send", "Send a WhatsApp message to a contact or group (by name or number) through the linked WhatsApp "
      "bridge. If WhatsApp isn't linked it explains how to link it (never opens the browser on its own). "
      "draft=true only types it into WhatsApp Web for the user to send.", WhatsAppArgs, risk="approval")
async def whatsapp_send(a: WhatsAppArgs, ctx: ToolContext):
    from ..integrations import whatsapp as wa
    state = (await wa.status())["state"]
    if state == "connected" and not a.draft:
        name, recipient = await _wa_recipient(ctx.user, a.to)
        r = await wa.send(recipient, a.message)
        if not r["success"]:
            raise ToolError(f"WhatsApp didn't accept it: {r['message'] or 'unknown error'}")
        return {"sent": True, "to": name}
    if not a.draft:
        # never fall back to the browser silently: say why it can't send and how to fix it
        raise ToolError({
            "not_set_up": "WhatsApp isn't linked to Terminator yet. Open [Settings → WhatsApp](#/settings) and scan the "
                          "QR code with your phone (WhatsApp → Linked devices → Link a device), then ask again.",
            "starting": "WhatsApp isn't linked to Terminator yet. Open [Settings → WhatsApp](#/settings); a QR code "
                        "appears there within a few seconds. Scan it with your phone, then ask again.",
            "waiting_for_scan": "WhatsApp isn't linked yet. Scan the QR code in [Settings → WhatsApp](#/settings) "
                                "with your phone (WhatsApp → Linked devices → Link a device), then ask again.",
            "offline": "WhatsApp is linked but the bridge isn't running. Start it with "
                       "`systemctl --user restart terminator-whatsapp`, then ask again.",
        }.get(state, "The WhatsApp bridge isn't reachable right now."))
    if reason := desktop.unavailable():
        raise ToolError(f"I can't open WhatsApp Web here ({reason}).")
    name, digits = await resolve_recipient(ctx.user, a.to)
    desktop.open_target(f"https://web.whatsapp.com/send?phone={digits}&text={quote(a.message)}")
    return {"prepared": True, "to": name, "phone": f"+{digits}",
            "next_step": "WhatsApp Web is open with the message typed in. Press Enter there to send it."}


class WaReadArgs(BaseModel):
    chat: str = Field(min_length=1, max_length=100, description="Contact or group name")
    limit: int = Field(15, ge=1, le=50)


@tool("whatsapp_read", "Read the latest messages of a WhatsApp chat (contact or group) from the linked WhatsApp.", WaReadArgs)
async def whatsapp_read(a: WaReadArgs, ctx: ToolContext):
    from ..integrations import whatsapp as wa
    hits = await asyncio.to_thread(wa.find_contacts, a.chat)
    if not hits:
        raise ToolError(f"No WhatsApp chat matches '{a.chat}'." + ("" if (await wa.status())["state"] != "not_set_up"
                                                                     else " WhatsApp isn't linked yet (Settings → WhatsApp)."))
    msgs = await asyncio.to_thread(wa.recent_messages, hits[0]["jid"], a.limit)
    return {"chat": hits[0]["name"], "messages": msgs} if msgs else f"No stored messages with {hits[0]['name']} yet."


class WaChatsArgs(BaseModel):
    limit: int = Field(10, ge=1, le=30)


@tool("whatsapp_chats", "List the most recent WhatsApp chats from the linked WhatsApp.", WaChatsArgs)
async def whatsapp_chats(a: WaChatsArgs, ctx: ToolContext):
    from ..integrations import whatsapp as wa
    return await asyncio.to_thread(wa.recent_chats, a.limit) or "No chats stored yet (is WhatsApp linked? Settings → WhatsApp)."


# ---------------- arbitrary terminal commands (approval every time) ----------------
CATASTROPHIC = [
    r"\brm\s+(-\w+\s+)*-\w*[rR]\w*\s+(-\w+\s+)*(/|/\*|~|~/|~/\*|\$HOME/?|/home/?\S*)(\s|$)",
    r"\bmkfs(\.\w+)?\b", r"\bdd\b[^|;&]*\bof=/dev/(sd|nvme|hd|vd|mmcblk)", r">\s*/dev/(sd|nvme|hd|vd|mmcblk)",
    r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", r"\bchmod\s+(-\w+\s+)*-R\s+\S+\s+/(\s|$)",
    r"\bchown\s+(-\w+\s+)*-R\s+\S+\s+/(\s|$)", r"\bwipefs\b", r"\bshred\b[^|;&]*/dev/",
]


def blocked_reason(command: str) -> str | None:
    for pat in CATASTROPHIC:
        if re.search(pat, command):
            return "This command could destroy the system or your files, so it is blocked even with approval."
    return None


class ShellArgs(BaseModel):
    command: str = Field(min_length=1, max_length=2000, description="A bash command line to run on this computer")
    reason: str = Field(max_length=300, description="Why this command is needed (shown to the user for approval)")


@tool("run_shell", "Run a terminal (bash) command on this computer. The user must approve the exact command first. "
      "Use for tasks no other tool covers. Prefer read-only commands; never use sudo (it cannot prompt for a password).",
      ShellArgs, risk="approval")
async def run_shell(a: ShellArgs, ctx: ToolContext):
    if reason := blocked_reason(a.command):
        raise ToolError(reason)
    timeout = get_settings().shell_timeout_s
    proc = await asyncio.create_subprocess_exec("/bin/bash", "-c", a.command, cwd=str(Path.home()), env=desktop.session_env(),
                                                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT, start_new_session=True)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.CancelledError:                      # user pressed Stop
        try:
            os.killpg(proc.pid, 9)
        except ProcessLookupError:
            pass
        raise
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, 9)
        except ProcessLookupError:
            pass
        raise ToolError(f"Command timed out after {timeout}s and was stopped")
    text = out.decode(errors="replace")
    if len(text) > 12000:
        text = "...[earlier output truncated]\n" + text[-12000:]
    return f"$ {a.command}\n(exit {proc.returncode})\n{text}"


# ---------------- closing apps ----------------
APP_ALIASES = {
    "excel": ["calc", "spreadsheet", "spreadsheets", "wps spreadsheets"], "spreadsheet": ["calc", "spreadsheets"],
    "word": ["writer", "wps writer", "document"], "powerpoint": ["impress", "presentation", "wps presentation"],
    "browser": ["chrome", "firefox", "brave", "chromium"], "chrome": ["google chrome"], "vscode": ["visual studio code"],
    "vs code": ["visual studio code"], "code": ["visual studio code"], "teams": ["teams for linux", "microsoft teams"],
    "compass": ["mongodb compass"], "mongo": ["mongodb compass"], "files": ["files"], "editor": ["text editor"],
}


def find_running(name: str) -> list[dict]:
    """Running desktop apps matching a spoken name ('excel' -> LibreOffice Calc / WPS Spreadsheets)."""
    import difflib
    from .. import environment
    q = name.lower().strip()
    wanted = [q] + APP_ALIASES.get(q, [])
    hits = []
    for app in environment.running_apps(environment._proc_info(), include_handled=True):
        n, i = app["name"].lower(), app["id"].lower()
        if any(w == n or w in n.split() or w in n or w in i for w in wanted) or \
                difflib.SequenceMatcher(None, q, n).ratio() >= 0.8:
            hits.append(app)
    return hits


class CloseArgs(BaseModel):
    name: str = Field(min_length=1, max_length=80, description="App to close, e.g. 'excel', 'chrome', 'teams'")
    force: bool = Field(False, description="Only if the user explicitly asks to force-close")


@tool("close_app", "Close a running desktop application ('excel' closes the open spreadsheet app). The user approves "
      "first because unsaved work could be lost. Apps are asked to quit normally; force only if the user asks.",
      CloseArgs, risk="approval", requires=NEEDS_DESKTOP)
async def close_app(a: CloseArgs, ctx: ToolContext):
    import signal
    targets = await asyncio.to_thread(find_running, a.name)
    if not targets:
        raise ToolError(f"{a.name} doesn't seem to be running.")
    # signal each app's main process only; the app then shuts its own helper processes down
    pids = {pid: t["name"] for t in targets for pid in (t.get("roots") or t["pids"])}
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL if a.force else signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    def alive(pid: int) -> bool:
        """Running, not just a zombie waiting for its parent to collect it."""
        try:
            return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] not in ("Z", "X")
        except (OSError, IndexError):
            return False
    for _ in range(30):                                   # give apps up to 6 s to close cleanly
        if not any(alive(pid) for pid in pids):
            break
        await asyncio.sleep(0.2)
    still = sorted({name for pid, name in pids.items() if alive(pid)})
    closed = sorted({t["name"] for t in targets} - set(still))
    return {"closed": closed, "still_open": still}


# ---------------- Microsoft Teams (prefilled chat; the user presses Enter) ----------------
_teams_handler: bool | None = None


def teams_app_handles_links() -> bool:
    """True if a Teams desktop app (e.g. teams-for-linux) is registered for msteams: links."""
    global _teams_handler
    if _teams_handler is None:
        import shutil, subprocess
        try:
            out = subprocess.run(["xdg-mime", "query", "default", "x-scheme-handler/msteams"], capture_output=True,
                                 text=True, timeout=5, env=desktop.session_env()).stdout.strip() if shutil.which("xdg-mime") else ""
        except (OSError, subprocess.SubprocessError):
            out = ""
        _teams_handler = bool(out)
    return _teams_handler


async def resolve_email(user: dict, to: str) -> tuple[str, str]:
    """(display name, email) for a contact name or an email address."""
    if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", to.strip()):
        return to.strip(), to.strip()
    hits = await contacts.resolve(user["_id"], to)
    if not hits:
        raise ToolError(f"I don't know {to}'s Teams email yet. Tell me their work email (I'll remember it), "
                        "or add them in Contacts.")
    if len(hits) > 1 and hits[0]["name"].lower() != to.strip().lower():
        raise ToolError("Several contacts match: " + ", ".join(h["name"] for h in hits[:5]) + ". Which one?")
    c = hits[0]
    if not c.get("emails"):
        raise ToolError(f"{c['name']} has no email saved, and Teams needs their work email. What is it?")
    return c["name"], c["emails"][0]


class TeamsArgs(BaseModel):
    to: str = Field(min_length=1, max_length=120, description="Colleague's name or work email")
    message: str = Field(min_length=1, max_length=4000)
    draft: bool = Field(False, description="Only open Teams with the text typed in instead of sending")


@tool("teams_message", "Send a Microsoft Teams chat message to a colleague (found by name in the organization) as the "
      "user, when Microsoft 365 is connected. Otherwise it opens Teams with the text typed in and the user presses "
      "Enter (needs their work email). Say which happened.", TeamsArgs, risk="approval")
async def teams_message(a: TeamsArgs, ctx: ToolContext):
    from ..integrations import ms365
    if not a.draft and await ms365.connected(ctx.user):
        try:
            return await ms365.teams_send(ctx.user, a.to, a.message)
        except (ms365.GraphError, ms365.NotConnected) as e:
            raise ToolError(str(e))
    if reason := desktop.unavailable():
        raise ToolError(f"Microsoft 365 isn't connected (Settings → Microsoft 365) and I can't open Teams here ({reason}).")
    name, email = await resolve_email(ctx.user, a.to)
    path = f"/l/chat/0/0?users={quote(email)}&message={quote(a.message)}"
    desktop.open_target(("msteams:" if teams_app_handles_links() else "https://teams.microsoft.com") + path)
    return {"prepared": True, "to": name, "email": email,
            "next_step": "Teams is open with the message typed in. Press Enter there to send it."}


class TeamsReadArgs(BaseModel):
    limit: int = Field(10, ge=1, le=30)


@tool("teams_read", "List recent Microsoft Teams chats with the latest message in each (needs Microsoft 365 connected).",
      TeamsReadArgs)
async def teams_read(a: TeamsReadArgs, ctx: ToolContext):
    from ..integrations import ms365
    try:
        return await ms365.teams_recent(ctx.user, a.limit) or "No recent chats."
    except (ms365.GraphError, ms365.NotConnected) as e:
        raise ToolError(str(e))


class EmailArgs(BaseModel):
    to: str = Field(min_length=1, max_length=200, description="Person's name or an email address")
    subject: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=20000)
    draft: bool = Field(False, description="Only open the compose window with the email filled in instead of sending")
    account: Literal["auto", "outlook", "gmail"] = Field("auto", description="auto: Outlook if connected, else Gmail")


async def _email_account(user: dict, wanted: str) -> str | None:
    from ..integrations import gmail, ms365
    if wanted == "gmail":
        return "gmail" if await gmail.connected(user) else None
    if wanted == "outlook":
        return "outlook" if await ms365.connected(user) else None
    if await ms365.connected(user):
        return "outlook"
    return "gmail" if await gmail.connected(user) else None


@tool("email_send", "Send an email from the user's Outlook (Microsoft 365). Recipients can be colleagues' names. "
      "With draft=true (or when Microsoft 365 isn't connected) it opens Outlook on the web with the email filled in.",
      EmailArgs, risk="approval")
async def email_send(a: EmailArgs, ctx: ToolContext):
    from ..integrations import gmail, ms365
    acct = None if a.draft else await _email_account(ctx.user, a.account)
    try:
        if acct == "outlook":
            return await ms365.email_send(ctx.user, a.to, a.subject, a.body)
        if acct == "gmail":
            try:
                name, addr = await resolve_email(ctx.user, a.to)          # Terminator contacts first
            except ToolError:
                name, addr = await gmail.find_address(ctx.user, a.to)     # then people you've emailed with
            return await gmail.send(ctx.user, name, addr, a.subject, a.body)
    except (ms365.GraphError, ms365.NotConnected, gmail.GmailError) as e:
        raise ToolError(str(e))
    if not a.draft:
        raise ToolError("No email account is connected. Connect Gmail (Settings → Gmail) or Microsoft 365 "
                        "(Settings → Microsoft 365), then ask again.")
    if reason := desktop.unavailable():
        raise ToolError(f"Microsoft 365 isn't connected and I can't open Outlook here ({reason}).")
    name, email = await resolve_email(ctx.user, a.to)
    if a.account == "gmail" or (a.account == "auto" and not await ms365.connected(ctx.user) and await gmail.connected(ctx.user)):
        desktop.open_target(f"https://mail.google.com/mail/?view=cm&fs=1&to={quote(email)}&su={quote(a.subject)}&body={quote(a.body)}")
        where = "Gmail"
    else:
        desktop.open_target(f"https://outlook.office.com/mail/deeplink/compose?to={quote(email)}&subject={quote(a.subject)}"
                            f"&body={quote(a.body)}")
        where = "Outlook"
    return {"prepared": True, "to": name, "email": email,
            "next_step": f"{where} is open with the email filled in. Click Send there."}


class EmailListArgs(BaseModel):
    query: str = Field("", max_length=200, description="Search words (sender, subject, content); empty for latest")
    unread_only: bool = False
    limit: int = Field(10, ge=1, le=25)
    account: Literal["auto", "outlook", "gmail"] = "auto"


@tool("email_list", "Read or search the user's inbox (Outlook or Gmail): latest, unread, or matching words.", EmailListArgs)
async def email_list(a: EmailListArgs, ctx: ToolContext):
    from ..integrations import gmail, ms365
    acct = await _email_account(ctx.user, a.account)
    try:
        if acct == "outlook":
            return await ms365.email_list(ctx.user, a.query, a.unread_only, a.limit) or "No matching emails."
        if acct == "gmail":
            return await gmail.list_mail(ctx.user, a.query, a.unread_only, a.limit) or "No matching emails."
    except (ms365.GraphError, ms365.NotConnected, gmail.GmailError) as e:
        raise ToolError(str(e))
    raise ToolError("No email account is connected (Settings → Gmail or Microsoft 365).")


class CalendarArgs(BaseModel):
    day: Literal["today", "tomorrow", "week"] = "today"


@tool("calendar_list", "The user's Outlook calendar events for today, tomorrow or the next 7 days.", CalendarArgs)
async def calendar_list(a: CalendarArgs, ctx: ToolContext):
    from ..integrations import ms365
    try:
        start, end = ms365.day_range(a.day)
        return {"day": a.day, "events": await ms365.calendar(ctx.user, start, end)}
    except (ms365.GraphError, ms365.NotConnected) as e:
        raise ToolError(str(e))


class ContactSaveArgs(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: str | None = Field(None, max_length=200)
    phone: str | None = Field(None, max_length=40)
    nickname: str | None = Field(None, max_length=60, description="What the user calls them, e.g. 'Mom'")


@tool("contact_save", "Save or update a contact (name with email and/or phone) when the user gives you their details.",
      ContactSaveArgs, risk="write")
async def contact_save(a: ContactSaveArgs, ctx: ToolContext):
    phones = []
    if a.phone:
        try:
            phones = ["+" + contacts.normalize_phone(a.phone, (ctx.user.get("prefs") or {}).get("default_country_code", ""))]
        except contacts.ContactError as e:
            raise ToolError(str(e))
    if a.email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", a.email):
        raise ToolError(f"'{a.email}' doesn't look like an email address")
    existing = (await contacts.resolve(ctx.user["_id"], a.name) or [None])[0]
    name = existing["name"] if existing and existing["name"].lower() == a.name.lower() else a.name.strip().title()
    new = await contacts.upsert(ctx.user["_id"], name, phones, [a.email] if a.email else [],
                                [a.nickname] if a.nickname else None)
    return {"saved": name, "new": new, "email": a.email, "phone": phones[0] if phones else None}


# ---------------- Instagram (unofficial, at the user's request) ----------------
class InstaArgs(BaseModel):
    to: str = Field(min_length=1, max_length=100, description="Name or @username of someone in your DMs / following")
    message: str = Field(min_length=1, max_length=2000)


@tool("instagram_send", "Send an Instagram direct message to someone the user already chats with or follows.",
      InstaArgs, risk="approval")
async def instagram_send(a: InstaArgs, ctx: ToolContext):
    from ..integrations import instagram
    try:
        return await instagram.send(ctx.user, a.to, a.message)
    except instagram.InstagramError as e:
        raise ToolError(str(e))


class InstaReadArgs(BaseModel):
    who: str = Field("", max_length=100, description="Person to read the conversation with; empty lists recent chats")
    limit: int = Field(10, ge=1, le=30)


@tool("instagram_read", "Read recent Instagram DMs: the latest chats, or the conversation with one person.", InstaReadArgs)
async def instagram_read(a: InstaReadArgs, ctx: ToolContext):
    from ..integrations import instagram
    try:
        return await instagram.read(ctx.user, a.who, a.limit) if a.who else await instagram.threads(ctx.user, a.limit)
    except instagram.InstagramError as e:
        raise ToolError(str(e))
