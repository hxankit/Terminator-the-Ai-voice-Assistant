"""Bridge to the local Linux desktop session (GNOME/Wayland or X11): apps, URLs, volume, media, lock, notify.

Everything runs as the server's own user with the graphical session's environment. On a headless server
`unavailable()` explains why desktop tools are off instead of failing obscurely.
"""
import asyncio, difflib, os, re, shutil, time
from configparser import ConfigParser, Error as ConfigError
from pathlib import Path
from .config import get_settings


def session_env() -> dict:
    """Environment needed to talk to the user's desktop session, filling in standard defaults."""
    env = dict(os.environ)
    runtime = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    env.setdefault("XDG_RUNTIME_DIR", runtime)
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime}/bus")
    if "WAYLAND_DISPLAY" not in env and Path(runtime, "wayland-0").exists():
        env["WAYLAND_DISPLAY"] = "wayland-0"
    if "DISPLAY" not in env and Path("/tmp/.X11-unix/X0").exists():
        env["DISPLAY"] = ":0"
    return env


def unavailable() -> str | None:
    if not get_settings().desktop_control:
        return "desktop control is disabled (DESKTOP_CONTROL=false)"
    env = session_env()
    if not (env.get("WAYLAND_DISPLAY") or env.get("DISPLAY")):
        return "no graphical desktop session found (is the server running on a desktop?)"
    return None


async def run(argv: list[str], timeout: float = 10) -> tuple[int, str]:
    """Run a command (no shell) in the desktop session environment; returns (exit code, combined output)."""
    if not shutil.which(argv[0]):
        return 127, f"{argv[0]} is not installed"
    proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                                                stdin=asyncio.subprocess.DEVNULL, env=session_env())
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "timed out"
    return proc.returncode, out.decode(errors="replace").strip()


def spawn_detached(argv: list[str]) -> None:
    """Start a GUI program that must outlive the request (and the server)."""
    import subprocess
    subprocess.Popen(argv, env=session_env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


# ---------------- applications ----------------
_apps: list[dict] = []
_apps_at = 0.0


def _app_dirs() -> list[Path]:
    data_dirs = os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":")
    dirs = [Path(d) / "applications" for d in data_dirs]
    dirs += [Path.home() / ".local/share/applications", Path("/var/lib/snapd/desktop/applications"),
             Path("/var/lib/flatpak/exports/share/applications"), Path.home() / ".local/share/flatpak/exports/share/applications"]
    return [d for d in dict.fromkeys(dirs) if d.is_dir()]


def list_apps(refresh: bool = False) -> list[dict]:
    """Installed launchable applications from .desktop entries (cached for 5 minutes)."""
    global _apps, _apps_at
    if _apps and not refresh and time.time() - _apps_at < 300:
        return _apps
    found: dict[str, dict] = {}
    for d in _app_dirs():
        for f in d.glob("*.desktop"):
            cp = ConfigParser(interpolation=None, strict=False)
            try:
                cp.read(f, encoding="utf-8")
            except (ConfigError, UnicodeDecodeError):
                continue
            if not cp.has_section("Desktop Entry"):
                continue
            e = cp["Desktop Entry"]
            if e.get("Type") != "Application" or e.get("NoDisplay", "").lower() == "true" or e.get("Hidden", "").lower() == "true":
                continue
            app_id = f.stem
            found.setdefault(app_id, {"id": app_id, "name": e.get("Name", app_id), "generic": e.get("GenericName", ""),
                                      "keywords": [k for k in e.get("Keywords", "").split(";") if k]})
    _apps, _apps_at = sorted(found.values(), key=lambda a: a["name"].lower()), time.time()
    return _apps


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


ALIASES = {"browser": "google chrome", "chrome": "google chrome", "terminal": "terminal", "files": "files",
           "file manager": "files", "settings": "settings", "calculator": "calculator", "editor": "text editor",
           "vs code": "visual studio code", "vscode": "visual studio code", "code": "visual studio code"}


def match_app(query: str) -> tuple[dict | None, list[dict]]:
    """Best app for a spoken/typed name, plus close alternatives. Returns (None, suggestions) if unsure."""
    q = _norm(ALIASES.get(_norm(query), query))
    if not q:
        return None, []
    apps = list_apps()
    scored = []
    for a in apps:
        name, generic = _norm(a["name"]), _norm(a["generic"])
        kws = [_norm(k) for k in a["keywords"]]
        if q == name or q == _norm(a["id"]):
            s = 1.0
        elif name.startswith(q) or q.startswith(name):
            s = 0.9
        elif q in name.split() or q == generic:
            s = 0.85
        elif q in kws:
            s = 0.7
        else:
            s = max(difflib.SequenceMatcher(None, q, name).ratio(), difflib.SequenceMatcher(None, q, generic).ratio() * 0.9)
        scored.append((s, a))
    scored.sort(key=lambda x: -x[0])
    best = scored[0] if scored else (0, None)
    alts = [a for s, a in scored[1:4] if s >= 0.5]
    if best[0] >= 0.8 and not (len(scored) > 1 and scored[1][0] == best[0] and best[0] < 1.0):
        return best[1], alts
    return None, [a for s, a in scored[:4] if s >= 0.5]


def launch_app(app: dict, args: list[str] | None = None) -> None:
    """gtk-launch passes extra arguments as files/URIs (the entry's %F/%U), e.g. a folder for VS Code."""
    spawn_detached(["gtk-launch", app["id"], *(args or [])])


# ---------------- folders (fuzzy "open my inventory folder") ----------------
DIR_SKIP = {"node_modules", ".venv", "venv", "__pycache__", "site-packages", "dist", "build", "snap", "target"}
_dirs: list[str] = []
_dirs_at = 0.0


def dir_index(refresh: bool = False, max_depth: int = 5) -> list[str]:
    """Folders under home (no hidden or dependency folders), cached for 5 minutes."""
    global _dirs, _dirs_at
    if _dirs and not refresh and time.time() - _dirs_at < 300:
        return _dirs
    home, out, deadline = str(Path.home()), [], time.monotonic() + 2.5
    for root, dirs, _ in os.walk(home):
        depth = root[len(home):].count(os.sep)
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in DIR_SKIP] if depth < max_depth else []
        out.extend(os.path.join(root, d) for d in dirs)
        if time.monotonic() > deadline:
            break
    _dirs, _dirs_at = out, time.time()
    return out


def _words(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def find_dirs(query: str, limit: int = 5) -> list[tuple[float, str]]:
    """Folders whose name matches `query`, best first, as (score, path)."""
    q = _words(re.sub(r"\b(folder|directory|dir|project|repo|repository|workspace)\b", "", query, flags=re.I))
    if not q:
        return []
    scored = []
    for path in dir_index():
        name = _words(os.path.basename(path))
        if not name:
            continue
        if name == q:
            s = 1.0
        elif name.startswith(q):
            s = 0.9
        elif set(q.split()) <= set(name.split()):
            s = 0.85
        else:
            s = difflib.SequenceMatcher(None, q.replace(" ", ""), name.replace(" ", "")).ratio()
        if s >= 0.75:
            scored.append((round(s, 3), path))
    return sorted(scored, key=lambda x: (-x[0], x[1].count(os.sep), x[1]))[:limit]


def resolve_dir(query: str) -> tuple[str | None, list[str]]:
    """(path, candidates). Among equally good matches, a folder that contains all the others wins
    (".../Inventory" over ".../Inventory/backend/uploads/inventory"); otherwise it's ambiguous (None)."""
    hits = find_dirs(query)
    if not hits:
        return None, []
    ties = [p for sc, p in hits if hits[0][0] - sc < 0.04]
    if len(ties) == 1:
        return ties[0], [p for _, p in hits]
    root = min(ties, key=lambda p: p.count(os.sep))
    if all(p == root or p.startswith(root + os.sep) for p in ties):
        return root, [p for _, p in hits]
    return None, [p for _, p in hits]


def open_target(target: str) -> None:
    """Open a URL or file with the user's default application."""
    spawn_detached(["xdg-open", target])


# ---------------- audio ----------------
async def get_volume() -> dict:
    code, out = await run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"])
    m = re.search(r"Volume:\s*([\d.]+)", out)
    if code != 0 or not m:
        raise RuntimeError(f"Could not read volume: {out}")
    return {"percent": round(float(m.group(1)) * 100), "muted": "MUTED" in out}


async def set_volume(percent: int | None = None, change: int | None = None, mute: bool | None = None) -> dict:
    sink = "@DEFAULT_AUDIO_SINK@"
    if mute is not None:
        code, out = await run(["wpctl", "set-mute", sink, "1" if mute else "0"])
        if code:
            raise RuntimeError(out)
    if percent is not None:
        code, out = await run(["wpctl", "set-volume", "-l", "1.5", sink, f"{max(0, min(percent, 150)) / 100:.2f}"])
        if code:
            raise RuntimeError(out)
    elif change:
        code, out = await run(["wpctl", "set-volume", "-l", "1.5", sink, f"{abs(change)}%{'+' if change > 0 else '-'}"])
        if code:
            raise RuntimeError(out)
    if (percent is not None or change) and mute is None:
        await run(["wpctl", "set-mute", sink, "0"])   # changing volume implies you want to hear it
    return await get_volume()


# ---------------- media (MPRIS over D-Bus) ----------------
async def media_players() -> list[str]:
    code, out = await run(["gdbus", "call", "--session", "--dest", "org.freedesktop.DBus", "--object-path",
                           "/org/freedesktop/DBus", "--method", "org.freedesktop.DBus.ListNames"])
    return re.findall(r"'(org\.mpris\.MediaPlayer2\.[^']+)'", out) if code == 0 else []


async def _player_status(name: str) -> str:
    code, out = await run(["gdbus", "call", "--session", "--dest", name, "--object-path", "/org/mpris/MediaPlayer2",
                           "--method", "org.freedesktop.DBus.Properties.Get", "org.mpris.MediaPlayer2.Player", "PlaybackStatus"])
    m = re.search(r"'(Playing|Paused|Stopped)'", out)
    return m.group(1) if m else "Unknown"


async def media_control(action: str) -> str:
    method = {"play_pause": "PlayPause", "play": "Play", "pause": "Pause", "next": "Next",
              "previous": "Previous", "stop": "Stop"}[action]
    players = await media_players()
    if not players:
        raise RuntimeError("No media player is running")
    statuses = {p: await _player_status(p) for p in players}
    target = next((p for p, s in statuses.items() if s == "Playing"), None) or \
        next((p for p, s in statuses.items() if s == "Paused"), players[0])
    code, out = await run(["gdbus", "call", "--session", "--dest", target, "--object-path", "/org/mpris/MediaPlayer2",
                           "--method", f"org.mpris.MediaPlayer2.Player.{method}"])
    if code:
        raise RuntimeError(out)
    return target.removeprefix("org.mpris.MediaPlayer2.").split(".")[0]


# ---------------- session ----------------
async def lock_screen() -> None:
    code, out = await run(["gdbus", "call", "--session", "--dest", "org.gnome.ScreenSaver", "--object-path",
                           "/org/gnome/ScreenSaver", "--method", "org.gnome.ScreenSaver.SetActive", "true"])
    if code:
        code, out = await run(["loginctl", "lock-session"])
        if code:
            raise RuntimeError(f"Could not lock the screen: {out}")


async def notify(title: str, body: str = "", urgency: str = "normal") -> None:
    await run(["notify-send", "-a", "Terminator", "-u", urgency, title, body])
