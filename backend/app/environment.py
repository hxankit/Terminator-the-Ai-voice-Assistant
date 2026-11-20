"""Save and restore the desktop "environment": browser windows and tabs, VS Code projects, terminals, open apps.

Snapshots are taken on demand ("save my environment"), before a shutdown done through Terminator, and
automatically every few minutes, so "open previous environment" also works after a normal shutdown.
Everything stays local (MongoDB on this machine).
"""
import asyncio, glob, json, logging, os, re, shutil, struct, subprocess, time
from pathlib import Path
from urllib.parse import unquote, urlparse
from . import desktop
from .db import db, now

log = logging.getLogger("environment")
CHROME_DIRS = [Path.home() / ".config/google-chrome", Path.home() / ".config/chromium",
               Path.home() / ".config/BraveSoftware/Brave-Browser"]
CHROME_BIN = {"google-chrome": "google-chrome", "chromium": "chromium", "Brave-Browser": "brave-browser"}
VSCODE_STORAGE = Path.home() / ".config/Code/User/globalStorage/storage.json"
SKIP_URLS = ("chrome://newtab", "about:blank", "chrome-search://", "chrome://new-tab-page", "edge://newtab")


# ======================================================================= Chrome session (SNSS) parser
class _Pickle:
    """Reader for Chromium base::Pickle payloads (4-byte aligned fields)."""
    def __init__(self, data: bytes):
        self.d, self.p = data, 4                 # skip the uint32 payload-size header

    def _align(self):
        self.p = (self.p + 3) & ~3

    def int32(self) -> int:
        v = struct.unpack_from("<i", self.d, self.p)[0]
        self.p += 4
        return v

    def string(self) -> str:
        n = self.int32()
        if n < 0 or self.p + n > len(self.d):
            raise ValueError("bad string")
        s = self.d[self.p:self.p + n].decode("utf-8", "replace")
        self.p += n
        self._align()
        return s


def parse_snss(path: str | Path) -> list[list[str]]:
    """Replay a Chrome 'Session_*' file into the currently open windows -> tab URLs (in tab order)."""
    data = Path(path).read_bytes()
    if data[:4] != b"SNSS":
        raise ValueError("not a Chrome session file")
    tabs: dict[int, dict] = {}
    windows: dict[int, dict] = {}
    closed_tabs, closed_windows = set(), set()
    tab = lambda tid: tabs.setdefault(tid, {"window": None, "index": 0, "navs": {}, "sel": None})
    pos = 8
    while pos + 2 <= len(data):
        size = struct.unpack_from("<H", data, pos)[0]
        pos += 2
        if size == 0 or pos + size > len(data):
            break
        cmd, payload = data[pos], data[pos + 1:pos + size]
        pos += size
        try:
            if cmd == 0 and len(payload) >= 8:                         # SetTabWindow(window_id, tab_id)
                wid, tid = struct.unpack_from("<ii", payload)
                tab(tid)["window"] = wid
                windows.setdefault(wid, {"type": 0})
            elif cmd == 2 and len(payload) >= 8:                       # SetTabIndexInWindow(tab_id, index)
                tid, idx = struct.unpack_from("<ii", payload)
                tab(tid)["index"] = idx
            elif cmd == 6:                                              # UpdateTabNavigation (pickled)
                pk = _Pickle(payload)
                tid, nav_index = pk.int32(), pk.int32()
                tab(tid)["navs"][nav_index] = pk.string()
            elif cmd == 7 and len(payload) >= 8:                       # SetSelectedNavigationIndex(tab_id, index)
                tid, idx = struct.unpack_from("<ii", payload)
                tab(tid)["sel"] = idx
            elif cmd == 9 and len(payload) >= 8:                       # SetWindowType(window_id, type)
                wid, wtype = struct.unpack_from("<ii", payload)
                windows.setdefault(wid, {})["type"] = wtype
            elif cmd == 16 and len(payload) >= 4:                      # TabClosed
                closed_tabs.add(struct.unpack_from("<i", payload)[0])
            elif cmd == 17 and len(payload) >= 4:                      # WindowClosed
                closed_windows.add(struct.unpack_from("<i", payload)[0])
            elif cmd == 5 and len(payload) >= 8:                       # TabNavigationPathPrunedFromBack(tab, index)
                tid, idx = struct.unpack_from("<ii", payload)
                t = tab(tid)
                t["navs"] = {k: v for k, v in t["navs"].items() if k < idx}
            elif cmd == 24 and len(payload) >= 12:                     # TabNavigationPathPruned(tab, index, count)
                tid, idx, count = struct.unpack_from("<iii", payload)
                t = tab(tid)
                t["navs"] = {(k if k < idx else k - count): v for k, v in t["navs"].items() if not idx <= k < idx + count}
        except (struct.error, ValueError, UnicodeDecodeError):
            continue                                                    # one bad record never spoils the rest
    out: dict[int, list[tuple[int, str]]] = {}
    for tid, t in tabs.items():
        wid = t["window"]
        if tid in closed_tabs or wid is None or wid in closed_windows or windows.get(wid, {}).get("type", 0) != 0:
            continue
        if not t["navs"]:
            continue
        url = t["navs"].get(t["sel"]) if t["sel"] is not None else None
        url = url or t["navs"][max(t["navs"])]
        if url and not url.startswith(SKIP_URLS):
            out.setdefault(wid, []).append((t["index"], url))
    return [[u for _, u in sorted(v)] for v in out.values() if v]


def chrome_sessions() -> list[dict]:
    """Current windows/tabs of every Chromium-family profile that has a session file."""
    found = []
    for base in CHROME_DIRS:
        for sess_dir in glob.glob(str(base / "*" / "Sessions")):
            files = sorted(glob.glob(os.path.join(sess_dir, "Session_*")), key=os.path.getmtime)
            if not files:
                continue
            try:
                wins = parse_snss(files[-1])
            except (OSError, ValueError) as e:
                log.info("could not read %s: %s", files[-1], e)
                continue
            profile_dir = Path(sess_dir).parent
            if wins:
                found.append({"browser": CHROME_BIN.get(base.name, base.name), "profile": profile_dir.name,
                              "windows": wins, "restores_itself": _restores_itself(profile_dir)})
    return found


def _restores_itself(profile_dir: Path) -> bool:
    """True if the profile is set to 'Continue where you left off' (Chrome reopens tabs by itself)."""
    try:
        prefs = json.loads((profile_dir / "Preferences").read_text())
        return prefs.get("session", {}).get("restore_on_startup") == 1
    except (OSError, ValueError):
        return False


# ======================================================================= VS Code, terminals, apps
def vscode_folders() -> list[str]:
    try:
        state = json.loads(VSCODE_STORAGE.read_text()).get("windowsState", {})
    except (OSError, ValueError):
        return []
    wins = [state.get("lastActiveWindow") or {}] + list(state.get("openedWindows") or [])
    out = []
    for w in wins:
        uri = w.get("folder") or (w.get("workspace") or {}).get("configPath") or w.get("workspaceIdentifier", {}).get("configURIPath")
        if uri and uri.startswith("file://"):
            path = unquote(urlparse(uri).path)
            if path not in out and os.path.exists(path):
                out.append(path)
    return out


def _proc_info() -> list[dict]:
    """The user's processes: pid, ppid, exe, cmdline, cwd, whether attached to the graphical session."""
    me, procs = os.getuid(), []
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            if os.stat(f"/proc/{pid}").st_uid != me:
                continue
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                argv = [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
            if not argv:
                continue
            with open(f"/proc/{pid}/stat") as f:
                ppid = int(f.read().rsplit(")", 1)[1].split()[1])
            try:
                exe = os.readlink(f"/proc/{pid}/exe")
            except OSError:
                exe = ""
            if len(argv) == 1 and " " in argv[0] and not os.path.exists(argv[0]):
                argv = argv[0].split(" ")          # Chromium/Electron rewrite their command line into one string
            procs.append({"pid": int(pid), "ppid": ppid, "argv": argv, "exe": exe,
                          "name": os.path.basename(exe) if exe else os.path.basename(argv[0]), "cwd": _cwd(pid)})
        except (OSError, ValueError, IndexError):
            continue
    return procs


def _cwd(pid) -> str:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return ""


def terminal_dirs(procs: list[dict]) -> list[str]:
    """Working directories of shells running inside gnome-terminal tabs."""
    servers = {p["pid"] for p in procs if p["name"].startswith("gnome-terminal-server") or "gnome-terminal-server" in p["exe"]}
    dirs = []
    for p in procs:
        if p["ppid"] in servers and p["cwd"] and p["cwd"] not in dirs:
            dirs.append(p["cwd"])
    return dirs


# handled separately (browser, VS Code, terminals) or impossible to restore faithfully
HANDLED = ("chrome", "chromium", "brave", "vscode", "com.microsoft.vscode", "code.desktop", "gnome-terminal",
           "nautilus", "org.gnome.nautilus")


def _exec_binary(app_id: str) -> str | None:
    """Real path of the program a .desktop entry runs (skips `env VAR=..` wrappers, resolves $PATH and symlinks)."""
    for d in desktop._app_dirs():
        f = d / f"{app_id}.desktop"
        if not f.exists():
            continue
        m = re.search(r"^Exec=(.*)$", f.read_text(errors="replace"), re.M)
        if not m:
            return None
        for tok in m.group(1).split():
            tok = tok.strip('"')
            if tok == "env" or "=" in tok:
                continue
            path = tok if os.path.isabs(tok) else shutil.which(tok)
            return os.path.realpath(path) if path else None
    return None


def _flatpak_running() -> set[str]:
    if not shutil.which("flatpak"):
        return set()
    try:
        out = subprocess.run(["flatpak", "ps", "--columns=application"], capture_output=True, text=True, timeout=5).stdout
        return {line.strip() for line in out.splitlines() if line.strip()}
    except (OSError, subprocess.SubprocessError):
        return set()


SHARED_DIRS = {"/usr/bin", "/usr/local/bin", "/bin", "/usr/sbin", "/snap/bin", "/usr/lib", "/usr/libexec",
               "/usr/lib/x86_64-linux-gnu", "/usr/games", "/usr/share"}
LIBREOFFICE = {"--calc": "libreoffice-calc", "--writer": "libreoffice-writer", "--impress": "libreoffice-impress",
               "--draw": "libreoffice-draw"}
LO_EXT = {"libreoffice-calc": (".xlsx", ".xls", ".ods", ".csv"), "libreoffice-writer": (".docx", ".doc", ".odt", ".rtf"),
          "libreoffice-impress": (".pptx", ".ppt", ".odp")}


def _file_args(argv: list[str]) -> list[str]:
    out = []
    for a in argv[1:]:
        if a.startswith("file://"):
            a = unquote(urlparse(a).path)
        if a.startswith(str(Path.home()) + "/") and os.path.isfile(a):
            out.append(a)
    return out[:5]


def _libreoffice(p: dict) -> str | None:
    """LibreOffice runs every document in one soffice.bin; work out which component it was started as."""
    if not p["exe"].endswith("soffice.bin"):
        return None
    for flag, app_id in LIBREOFFICE.items():
        if flag in p["argv"]:
            return app_id
    for f in _file_args(p["argv"]):
        for app_id, exts in LO_EXT.items():
            if f.lower().endswith(exts):
                return app_id
    return "libreoffice-startcenter"


def running_apps(procs: list[dict], include_handled: bool = False) -> list[dict]:
    """Installed desktop apps that are currently running (one entry each), with their pids and open files."""
    apps = [a for a in desktop.list_apps() if include_handled or not any(h in a["id"].lower() for h in HANDLED)]
    names = {a["id"]: a["name"] for a in desktop.list_apps()}
    by_path, by_name, by_snap, by_dir = {}, {}, {}, {}
    for app in apps:
        if "_" in app["id"] and Path(f"/snap/{app['id'].split('_')[0]}").exists():
            by_snap.setdefault(app["id"].split("_")[0], app)            # snap: <snapname>_<app>.desktop
        b = _exec_binary(app["id"])
        if b and os.path.basename(b) not in ("snap", "flatpak", "env", "sh", "bash", "gtk-launch"):
            by_path.setdefault(b, app)
            by_name.setdefault(os.path.basename(b), app)
            # launchers are often wrapper scripts next to the real program (/opt/google/chrome/google-chrome ->
            # /opt/google/chrome/chrome): match by the app's own install folder, never a shared bin folder
            d = os.path.dirname(b)
            if d.count("/") >= 3 and d not in SHARED_DIRS:
                by_dir.setdefault(d, app)
    found: dict[str, dict] = {}
    exe_by_pid = {p["pid"]: p["exe"] for p in procs}
    flatpaks = _flatpak_running()
    for app in apps:
        if app["id"] in flatpaks:
            found[app["id"]] = {"id": app["id"], "name": app["name"], "files": [], "pids": []}
    for p in procs:
        if any(a.startswith("--type=") or a == "--gapplication-service" for a in p["argv"]):
            continue                                  # helper processes / background services, not windows
        exe = p["exe"]
        if exe.endswith("/oosplash"):                 # LibreOffice's launcher stub; soffice.bin is the real app
            continue
        lo = _libreoffice(p)
        if lo:
            app = {"id": lo, "name": names.get(lo, "LibreOffice")}
        else:
            app = by_path.get(exe) or by_name.get(os.path.basename(exe)) or by_dir.get(os.path.dirname(exe))
            if not app and exe.startswith("/snap/"):
                app = by_snap.get(exe.split("/")[2])
        if not app:
            continue
        entry = found.setdefault(app["id"], {"id": app["id"], "name": app["name"], "files": [], "pids": [], "roots": []})
        entry["pids"].append(p["pid"])
        # main process = started by something outside the app's own install folder (GNOME, a launcher, systemd)
        if os.path.dirname(exe_by_pid.get(p["ppid"], "")) != os.path.dirname(exe):
            entry["roots"].append(p["pid"])
        entry["files"] = entry["files"] or _file_args(p["argv"])
    return list(found.values())


# ======================================================================= snapshot / restore
def capture() -> dict:
    procs = _proc_info()
    names = {p["name"] for p in procs}
    snap = {"browsers": chrome_sessions() if names & {"chrome", "chromium", "brave"} else [],
            "vscode": vscode_folders() if "code" in names else [],
            "terminals": terminal_dirs(procs),
            "apps": [{k: v for k, v in a.items() if k not in ("pids", "roots")} for a in running_apps(procs)]}
    snap["summary"] = summarize(snap)
    return snap


def summarize(s: dict) -> str:
    parts = []
    for b in s.get("browsers", []):
        tabs = sum(len(w) for w in b["windows"])
        parts.append(f"{b['browser'].replace('google-', '').title()}: {len(b['windows'])} window{'s' * (len(b['windows']) != 1)}, {tabs} tab{'s' * (tabs != 1)}")
    if s.get("vscode"):
        parts.append(f"VS Code: {', '.join(Path(p).name for p in s['vscode'])}")
    if s.get("terminals"):
        parts.append(f"{len(s['terminals'])} terminal{'s' * (len(s['terminals']) != 1)}")
    if s.get("apps"):
        parts.append(", ".join(a["name"] for a in s["apps"]))
    return "; ".join(parts) or "nothing open"


def is_empty(s: dict) -> bool:
    return not (s.get("browsers") or s.get("vscode") or s.get("terminals") or s.get("apps"))


async def save(user_id, reason: str) -> dict:
    snap = await asyncio.to_thread(capture)
    doc = {"user_id": user_id, "reason": reason, "created_at": now(), **snap}
    doc["_id"] = (await db().env_snapshots.insert_one(doc)).inserted_id
    # keep the 30 most recent automatic snapshots; manual/shutdown ones are kept
    autos = [d["_id"] async for d in db().env_snapshots.find({"user_id": user_id, "reason": "auto"}, {"_id": 1})
             .sort("created_at", -1).skip(30)]
    if autos:
        await db().env_snapshots.delete_many({"_id": {"$in": autos}})
    return doc


async def latest(user_id) -> dict | None:
    """Most recent snapshot that actually had something open."""
    async for d in db().env_snapshots.find({"user_id": user_id}).sort("created_at", -1).limit(50):
        if not is_empty(d):
            return d
    return None


def restore(snap: dict) -> list[str]:
    """Reopen everything in a snapshot. Returns what was launched (for the reply)."""
    done = []
    for b in snap.get("browsers", []):
        prof = [f"--profile-directory={b['profile']}"]
        if b.get("restores_itself"):
            desktop.spawn_detached([b["browser"], *prof])          # Chrome reopens its own tabs
        else:
            for win in b["windows"]:
                desktop.spawn_detached([b["browser"], *prof, "--new-window", *win[:60]])
        done.append(f"{len(b['windows'])} browser window{'s' * (len(b['windows']) != 1)}")
    for folder in snap.get("vscode", []):
        if os.path.isdir(folder):
            desktop.spawn_detached(["code", "--new-window", folder])
    if snap.get("vscode"):
        done.append("VS Code (" + ", ".join(Path(p).name for p in snap["vscode"]) + ")")
    for d in snap.get("terminals", []):
        if os.path.isdir(d):
            desktop.spawn_detached(["gnome-terminal", f"--working-directory={d}"])
    if snap.get("terminals"):
        done.append(f"{len(snap['terminals'])} terminal{'s' * (len(snap['terminals']) != 1)}")
    for app in snap.get("apps", []):
        desktop.spawn_detached(["gtk-launch", app["id"], *[f for f in app.get("files", []) if os.path.isfile(f)]])
        done.append(app["name"])
    return done


_frozen_until = 0.0


def freeze_autosave(seconds: float = 900) -> None:
    """After a shutdown/restart/log-out snapshot, don't let an automatic snapshot of half-closed apps replace it."""
    global _frozen_until
    _frozen_until = time.time() + seconds


async def autosave_loop(interval_s: float):
    """Background snapshots so 'open previous environment' works even after a normal shutdown."""
    last_summary = None
    while True:
        await asyncio.sleep(interval_s)
        try:
            if desktop.unavailable() or interval_s <= 0 or time.time() < _frozen_until:
                continue
            owner = await desktop_owner()
            if not owner:
                continue
            snap = await asyncio.to_thread(capture)
            if is_empty(snap) or snap["summary"] == last_summary:
                continue                                         # nothing open, or nothing changed
            await save(owner, "auto")
            last_summary = snap["summary"]
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("environment autosave failed")


async def desktop_owner():
    """Whose desktop is this? The only user, or the one who most recently used desktop features."""
    if await db().users.count_documents({}, limit=2) == 1:
        u = await db().users.find_one({}, {"_id": 1})
        return u["_id"]
    run = await db().tool_runs.find_one({"tool": {"$in": ["open_app", "set_volume", "save_environment", "restore_environment",
                                                          "power_action", "whatsapp_send", "open_with"]}},
                                        sort=[("created_at", -1)])
    return run["user_id"] if run else None
