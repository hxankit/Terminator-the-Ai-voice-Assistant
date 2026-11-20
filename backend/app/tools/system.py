"""Host system tools.
- system_info / inspect_system: read-only, run automatically. inspect_system only accepts commands that pass a
  strict read-only check (allow-listed programs and subcommands, pipes only, no secrets), then runs them via bash.
- run_command / read_log (SysAdmin) and run_shell (PC Control): need the user's approval every time."""
import asyncio, os, platform, re, shlex, shutil
from pathlib import Path
from pydantic import BaseModel, Field
from ..config import get_settings
from .base import tool, ToolError, ToolContext

BLOCKED_ARGS = {"rm", "mkfs", "dd", "shutdown", "reboot", "kill", "stop", "restart", "delete", "drop", ">", "|", ";", "&&"}
LOG_ROOTS = ("/var/log", str(Path.home()))


class Empty(BaseModel):
    pass


def _meminfo() -> dict:
    out = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":")
            out[k] = int(v.split()[0]) * 1024
    except (OSError, ValueError):
        pass
    return out


def _battery() -> dict | None:
    for b in sorted(Path("/sys/class/power_supply").glob("BAT*")):
        try:
            return {"percent": int((b / "capacity").read_text()), "status": (b / "status").read_text().strip()}
        except (OSError, ValueError):
            continue
    return None


def _cpu_temp() -> float | None:
    best = None
    for z in Path("/sys/class/thermal").glob("thermal_zone*"):
        try:
            kind = (z / "type").read_text().strip()
            t = int((z / "temp").read_text()) / 1000
        except (OSError, ValueError):
            continue
        if kind in ("x86_pkg_temp", "cpu-thermal", "k10temp", "coretemp") or best is None:
            best = t if kind in ("x86_pkg_temp", "cpu-thermal", "k10temp", "coretemp") else (best or t)
    return round(best, 1) if best else None


async def _top(by: str, n: int = 3) -> list[dict]:
    proc = await asyncio.create_subprocess_exec("ps", "-eo", "comm,%cpu,%mem,rss", f"--sort=-%{by}",
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    rows = []
    for line in out.decode().splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 4:
            rows.append({"name": " ".join(parts[:-3]), "cpu": float(parts[-3]), "mem": float(parts[-2]),
                         "mb": round(int(parts[-1]) / 1024)})
    merged: dict[str, dict] = {}                      # group helper processes (chrome x28 -> one line)
    for r in rows:
        m = merged.setdefault(r["name"], {"name": r["name"], "cpu": 0.0, "mem": 0.0, "mb": 0})
        m["cpu"] += r["cpu"]; m["mem"] += r["mem"]; m["mb"] += r["mb"]
    return sorted(merged.values(), key=lambda r: -r[by])[:n]


async def health_snapshot() -> dict:
    gb = lambda b: round(b / 1e9, 1)
    cpus = os.cpu_count() or 1
    load1, load5, _ = os.getloadavg()
    mem = _meminfo()
    total, avail = mem.get("MemTotal", 0), mem.get("MemAvailable", 0)
    swap_used = mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)
    du = shutil.disk_usage("/")
    try:
        up_s = float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError):
        up_s = 0
    snap = {
        "os": platform.freedesktop_os_release().get("PRETTY_NAME", platform.platform()) if hasattr(platform, "freedesktop_os_release") else platform.platform(),
        "cpu": {"cores": cpus, "load_1m": round(load1, 2), "load_5m": round(load5, 2), "load_percent": min(100, round(load1 / cpus * 100))},
        "memory": {"total_gb": gb(total), "used_gb": gb(total - avail), "used_percent": round((total - avail) / total * 100) if total else None},
        "swap_used_gb": gb(swap_used),
        "disk_root": {"total_gb": gb(du.total), "used_gb": gb(du.used), "free_gb": gb(du.free), "used_percent": round(du.used / du.total * 100)},
        "uptime_hours": round(up_s / 3600, 1),
        "battery": _battery(), "cpu_temp_c": _cpu_temp(),
        "top_cpu": await _top("cpu"), "top_memory": await _top("mem"),
    }
    warn = []
    if snap["cpu"]["load_percent"] >= 85:
        warn.append(f"CPU is very busy ({snap['cpu']['load_percent']}% load)")
    if (snap["memory"]["used_percent"] or 0) >= 85:
        warn.append(f"memory is nearly full ({snap['memory']['used_percent']}%)")
    if snap["disk_root"]["used_percent"] >= 90:
        warn.append(f"disk is nearly full ({snap['disk_root']['free_gb']} GB free)")
    if snap["swap_used_gb"] >= 2:
        warn.append(f"{snap['swap_used_gb']} GB of swap in use (the system may feel slow)")
    if snap["cpu_temp_c"] and snap["cpu_temp_c"] >= 85:
        warn.append(f"CPU is hot ({snap['cpu_temp_c']} °C)")
    if snap["battery"] and snap["battery"]["percent"] <= 15 and snap["battery"]["status"] != "Charging":
        warn.append(f"battery is low ({snap['battery']['percent']}%)")
    snap["warnings"] = warn
    return snap


def describe_health(s: dict) -> str:
    """Short spoken summary of a health snapshot."""
    parts = [f"CPU load {s['cpu']['load_percent']}%",
             f"memory {s['memory']['used_gb']} of {s['memory']['total_gb']} GB used",
             f"disk {s['disk_root']['used_gb']} of {s['disk_root']['total_gb']} GB used",
             f"up {s['uptime_hours']} hours"]
    if s.get("cpu_temp_c"):
        parts.append(f"CPU at {s['cpu_temp_c']} °C")
    if s.get("battery"):
        parts.append(f"battery {s['battery']['percent']}% ({s['battery']['status'].lower()})")
    busy = ", ".join(f"{p['name']} ({p['mb']} MB)" for p in s.get("top_memory", [])[:3])
    head = ("Heads up: " + "; ".join(s["warnings"]) + ". ") if s.get("warnings") else "Your system looks healthy. "
    return head + ", ".join(parts) + "." + (f" Using the most memory: {busy}." if busy else "")


@tool("system_info", "Health snapshot of THIS computer: CPU load, memory, disk, swap, uptime, battery, temperature, "
      "the programs using the most CPU and memory, and warnings. Use it for 'system health', 'is my PC slow', etc.", Empty)
async def system_info(a: Empty, ctx: ToolContext):
    return await health_snapshot()


# ---------------- read-only commands that run without approval ----------------
READONLY = {"uptime", "free", "df", "du", "uname", "hostname", "hostnamectl", "whoami", "id", "date", "lsb_release",
            "lscpu", "lsblk", "lsusb", "lspci", "lsof", "ss", "netstat", "ps", "pgrep", "pidof", "nproc", "sensors",
            "vmstat", "iostat", "mpstat", "which", "whereis", "w", "who", "last", "ls", "stat", "file", "wc", "head",
            "tail", "cat", "grep", "egrep", "sort", "uniq", "cut", "tr", "column", "nl", "find", "top", "ip", "nmcli",
            "systemctl", "journalctl", "docker", "pm2", "git", "node", "npm", "python3", "python", "nvidia-smi",
            "ping", "dig", "nslookup", "host", "timedatectl", "loginctl", "dpkg", "apt", "snap", "flatpak", "wpctl",
            "pactl", "xdg-mime", "gsettings", "printf", "echo", "basename", "dirname", "realpath", "readlink"}
SUBCOMMANDS = {   # program -> allowed first argument (subcommand)
    "systemctl": {"status", "is-active", "is-enabled", "is-failed", "list-units", "list-unit-files", "list-timers", "show", "cat", "--user"},
    "docker": {"ps", "images", "stats", "logs", "inspect", "version", "info", "top", "port"},
    "pm2": {"list", "ls", "status", "jlist", "describe", "show", "logs"},
    "git": {"status", "log", "branch", "diff", "remote", "show", "rev-parse", "describe", "shortlog", "tag", "-C"},
    "npm": {"-v", "--version", "ls", "list", "outdated", "view", "--prefix"},
    "node": {"-v", "--version"}, "python3": {"--version", "-V"}, "python": {"--version", "-V"},
    "ip": {"a", "addr", "address", "r", "route", "l", "link", "n", "neigh", "-br", "-c", "-4", "-6", "-s"},
    "nmcli": {"general", "device", "connection", "radio", "-t", "-f", "g", "d", "c", "dev", "con"},
    "dpkg": {"-l", "--list", "-s", "--status", "-L", "--listfiles", "-S", "--search"},
    "apt": {"list", "show", "policy", "search"}, "snap": {"list", "info", "version"}, "flatpak": {"list", "info", "ps"},
    "wpctl": {"status", "get-volume", "inspect"}, "pactl": {"list", "info", "get-sink-volume", "get-default-sink"},
    "gsettings": {"get", "list-keys", "list-recursively"}, "timedatectl": {"status", "show"},
    "loginctl": {"list-sessions", "show-session", "session-status", "show-user", "user-status"},
    "hostnamectl": {"status"}, "xdg-mime": {"query"},
}
FORBIDDEN_ARGS = {"set", "add", "del", "delete", "flush", "change", "replace", "modify", "up", "down", "connect",
                  "disconnect", "on", "off", "start", "stop", "restart", "reload", "enable", "disable", "mask", "kill",
                  "rm", "rmi", "prune", "exec", "-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fls",
                  "--vacuum-size", "--vacuum-time", "--vacuum-files", "--rotate", "--flush", "-f", "--follow",
                  "push", "pull", "fetch", "checkout", "reset", "commit", "merge", "rebase", "install", "uninstall",
                  "remove", "purge", "upgrade", "update", "reset-failed", "edit", "set-sink-volume", "set-volume"}
SECRET_HINTS = (".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker/config", ".netrc", ".pgpass", ".env", "id_rsa",
                "id_ed25519", "id_ecdsa", ".pem", ".key", "shadow", "sudoers", "keyring", "login data", "cookies",
                "google-chrome", "chromium", "credentials", "secret", "token", ".git-credentials", "/.config/terminator",
                "terminator-voice", "backend/data", "history", "wallet", ".password", "passwd")


def readonly_problem(command: str) -> str | None:
    """None if `command` is a read-only inspection pipeline; otherwise why it isn't."""
    cmd = re.sub(r"\s(2)?>\s*/dev/null|\s2>&1", " ", command)          # allow discarding errors
    if re.search(r"[;&<>`]|\$\(|\|\||\n", cmd):
        return "only single commands or pipes (|) are allowed - no ;, &&, redirection or $(...)"
    try:
        segments = [shlex.split(seg) for seg in cmd.split("|")]
    except ValueError:
        return "could not parse the command"
    for argv in segments:
        if not argv:
            return "empty pipeline segment"
        prog = os.path.basename(argv[0])
        if prog in ("sudo", "su", "doas", "pkexec"):
            return "no sudo - read-only commands only"
        if prog not in READONLY:
            return f"'{prog}' isn't on the read-only list"
        args = argv[1:]
        lowered = [x.lower() for x in args]
        if prog in SUBCOMMANDS:
            first = next((x for x in args if not x.startswith("-") or x in SUBCOMMANDS[prog]), None)
            if first is not None and first not in SUBCOMMANDS[prog] and not (prog in ("git", "npm") and first.startswith("-")):
                return f"'{prog} {first}' isn't a read-only subcommand"
        if any(x in FORBIDDEN_ARGS for x in lowered):
            return f"'{prog}' with those arguments could change something"
        if prog == "top" and "-b" not in args:
            return "top needs -b -n 1 (batch mode)"
        if prog == "tail" and any(x.startswith("-f") or x == "--follow" for x in args):
            return "tail -f would never finish"
        if prog == "ping" and not ({"-c"} & set(args)):
            return "ping needs -c (a count)"
        if prog == "docker" and "stats" in args and "--no-stream" not in args:
            return "docker stats needs --no-stream"
        if prog == "pm2" and "logs" in args and "--nostream" not in args:
            return "pm2 logs needs --nostream"
        if prog == "grep" and any(x in ("-r", "-R", "--recursive", "-rn", "-rl", "-Rn") for x in args):
            return "recursive grep isn't allowed (it could read private files)"
        if any(h in x for x in lowered for h in SECRET_HINTS):
            return "that touches private files (keys, tokens, browser data or app secrets)"
    return None


class InspectArgs(BaseModel):
    command: str = Field(min_length=1, max_length=500,
                         description="A READ-ONLY command or pipeline, e.g. 'df -h', 'free -h', "
                                     "'ps aux --sort=-%mem | head -n 6', 'systemctl status nginx', 'du -sh ~/Downloads'")


@tool("inspect_system", "Run a READ-ONLY shell command to look at this computer and get real data: disk, memory, "
      "processes, services, network, files and folders, versions, git status. Runs immediately (no approval). "
      "Only inspection commands and pipes are accepted; anything that changes the system must use run_shell instead.",
      InspectArgs)
async def inspect_system(a: InspectArgs, ctx: ToolContext):
    if why := readonly_problem(a.command):
        raise ToolError(f"Not run: {why}. If a change is really needed, use run_shell (the user approves it).")
    proc = await asyncio.create_subprocess_exec("/bin/bash", "-c", a.command, cwd=str(Path.home()),
                                                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT, start_new_session=True)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 20)
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
        raise ToolError("The command took longer than 20 seconds and was stopped; try a narrower command.")
    text = out.decode(errors="replace")
    if len(text) > 8000:
        text = text[:6000] + "\n...[output truncated]...\n" + text[-1500:]
    return f"$ {a.command}\n(exit {proc.returncode})\n{text}"


class CmdArgs(BaseModel):
    command: str = Field(min_length=1, max_length=500, description="e.g. 'pm2 list', 'df -h', 'systemctl status nginx'")
    reason: str = Field(max_length=300, description="Why this command is needed (shown to the user for approval)")


@tool("run_command", "Run a diagnostic command on the HOST server. Requires the user's approval each time. "
      "Only allow-listed, non-destructive commands; no pipes/redirects.", CmdArgs, risk="approval")
async def run_command(a: CmdArgs, ctx: ToolContext):
    try:
        argv = shlex.split(a.command)
    except ValueError as e:
        raise ToolError(f"Cannot parse command: {e}")
    allow = {c.strip() for c in get_settings().system_command_allowlist.split(",") if c.strip()}
    if not argv or argv[0] not in allow:
        raise ToolError(f"'{argv[0] if argv else ''}' is not in SYSTEM_COMMAND_ALLOWLIST")
    if any(x in BLOCKED_ARGS for x in argv[1:]):
        raise ToolError("Command contains a blocked (potentially destructive) argument")
    proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                                                stdin=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 30)
    except asyncio.CancelledError:
        proc.kill()
        raise
    except asyncio.TimeoutError:
        proc.kill()
        raise ToolError("Command timed out after 30s")
    return f"$ {a.command}\n(exit {proc.returncode})\n{out.decode(errors='replace')[-10000:]}"


class LogArgs(BaseModel):
    path: str = Field(max_length=500)
    lines: int = Field(200, ge=1, le=2000)


@tool("read_log", "Read the last N lines of a log file on the host (under /var/log or the app user's home). "
      "Requires approval.", LogArgs, risk="approval")
async def read_log(a: LogArgs, ctx: ToolContext):
    p = Path(a.path).expanduser().resolve()
    if not any(str(p).startswith(r + "/") for r in LOG_ROOTS):
        raise ToolError("Path outside allowed log directories")
    if not p.is_file():
        raise ToolError("File not found")
    with p.open("rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 400_000))
        data = f.read().decode(errors="replace").splitlines()[-a.lines:]
    return "\n".join(data)
