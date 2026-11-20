"""When does an 'approval' tool really need the user's OK?

Modes (user preference `approval_mode`):
  ask    - every approval tool asks (strictest)
  risky  - default: only risky actions ask (deleting/moving files, sudo, installs, services, kill, git push/reset,
           uploads, piping into a shell, force-closing apps, shutdown/restart/log out). Unparseable commands ask.
  never  - nothing asks. Catastrophic commands stay hard-blocked inside run_shell regardless.
"""
import os, re, shlex

MODES = ("ask", "risky", "never")
DEFAULT_MODE = "risky"

# programs that delete/move data, escalate privileges, change the system or install software
RISKY_PROGRAMS = {
    "rm", "rmdir", "shred", "mv", "dd", "truncate", "chmod", "chown", "chgrp", "unlink", "wipefs",
    "sudo", "su", "pkexec", "doas", "visudo",
    "apt", "apt-get", "aptitude", "dpkg", "snap", "flatpak", "pip", "pip3", "pipx", "gem", "cargo", "go",
    "service", "reboot", "shutdown", "poweroff", "halt", "init", "telinit",
    "kill", "killall", "pkill", "xkill",
    "crontab", "ufw", "iptables", "ip6tables", "nft", "mount", "umount", "mkfs", "fdisk", "parted", "gparted",
    "useradd", "userdel", "usermod", "groupadd", "passwd", "chpasswd",
    "scp", "rsync", "ssh", "sftp", "ftp", "eval", "exec", "source",
}
# program -> subcommands/flags that make it risky (the rest of its uses are fine)
RISKY_SUB = {
    "git": {"push", "reset", "clean", "rebase", "filter-branch", "filter-repo", "gc", "prune", "restore"},
    "systemctl": {"start", "stop", "restart", "reload", "enable", "disable", "mask", "unmask", "kill", "daemon-reload",
                  "edit", "set-property", "isolate", "poweroff", "reboot", "suspend", "hibernate"},
    "docker": {"rm", "rmi", "prune", "stop", "kill", "run", "system", "volume", "network", "compose", "exec"},
    "podman": {"rm", "rmi", "prune", "stop", "kill", "run", "system", "volume", "network", "exec"},
    "npm": {"install", "i", "uninstall", "remove", "rm", "ci", "update", "publish", "link", "exec", "x"},
    "pnpm": {"install", "add", "remove", "update", "publish", "dlx"}, "yarn": {"add", "remove", "install", "upgrade", "publish", "dlx"},
    "npx": None,                                   # runs arbitrary downloaded code: always ask
    "pm2": {"delete", "kill", "stop", "flush", "reset"},
    "nmcli": {"modify", "delete", "add", "down", "up", "connect", "disconnect", "off", "on"},
    "ip": {"set", "add", "del", "delete", "flush", "change", "replace"},
    "gsettings": {"set", "reset", "reset-recursively"}, "dconf": {"write", "reset", "load"},
    "timedatectl": {"set-time", "set-timezone", "set-ntp", "set-local-rtc"}, "hostnamectl": {"set-hostname", "hostname"},
    "find": {"-delete", "-exec", "-execdir", "-ok", "-okdir"},
    "sed": {"-i", "--in-place"}, "perl": {"-i", "-pi"},
}
UPLOAD_FLAGS = {"-d", "--data", "--data-binary", "--data-raw", "--data-urlencode", "-F", "--form", "-T", "--upload-file",
                "--post-data", "--post-file", "--body-data", "--body-file"}
SHELLS = {"sh", "bash", "zsh", "dash", "fish", "python", "python3", "perl", "ruby", "node"}


def risky_command(command: str) -> str | None:
    """Why a shell command needs approval in 'risky' mode, or None if it can run straight away."""
    if "$(" in command or "`" in command:
        return "uses command substitution"
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return "could not be parsed"
    seg, segments, after_pipe = [], [], []
    for tok in tokens + [";"]:
        if tok in (";", "&&", "||", "|", "&", "\n"):
            if seg:
                segments.append(seg)
                after_pipe.append(False)
            if tok == "|" and segments:
                after_pipe[-1] = True        # the NEXT segment receives piped input
            seg = []
        elif tok in (">", ">|"):
            seg.append("__overwrite__")
        else:
            seg.append(tok)
    piped = False
    for i, argv in enumerate(segments):
        words = [w for w in argv if not re.fullmatch(r"\w+=.*", w)]   # skip VAR=value prefixes
        if not words:
            continue
        if "__overwrite__" in words:
            target = words[words.index("__overwrite__") + 1] if words.index("__overwrite__") + 1 < len(words) else ""
            if target not in ("/dev/null", "&1", "&2", "2"):
                return "overwrites a file (>)"
        names = [os.path.basename(w) for w in words]
        prog = names[0]
        if piped and prog in SHELLS:
            return "pipes data into a shell or interpreter"
        hit = next((n for n in names if n in RISKY_PROGRAMS), None)   # also catches sudo/xargs/nohup wrappers
        if hit:
            return f"uses {hit}"
        for n in names:
            if n in RISKY_SUB:
                subs = RISKY_SUB[n]
                if subs is None:
                    return f"uses {n}"
                rest = names[names.index(n) + 1:]
                if any(a in subs or a.split("=")[0] in subs for a in rest):
                    return f"{n} {next(a for a in rest if a in subs or a.split('=')[0] in subs)}"
                if n == "git" and "checkout" in rest and "--" in rest:
                    return "git checkout -- (discards changes)"
                if n == "git" and "branch" in rest and ({"-D", "-d", "--delete"} & set(rest)):
                    return "deletes a git branch"
                if n == "git" and "stash" in rest and ({"drop", "clear"} & set(rest)):
                    return "drops git stashes"
        if prog in ("curl", "wget") and (set(words) & UPLOAD_FLAGS or {"-X", "--request"} & set(words)
                                         and any(w.upper() in ("POST", "PUT", "DELETE", "PATCH") for w in words)):
            return "uploads data"
        piped = after_pipe[i]
    return None


def needs_approval(tool: str, args: dict, user: dict, ctx=None) -> bool:
    mode = (user.get("prefs") or {}).get("approval_mode", DEFAULT_MODE)
    if mode == "never":
        return False
    if mode != "risky":
        return True
    if tool == "run_shell":
        return risky_command(str(args.get("command", ""))) is not None
    if tool == "close_app":
        return bool(args.get("force"))
    if tool in ("run_command", "read_log"):        # allow-listed / read-only by construction
        return False
    if tool in ("whatsapp_send", "teams_message", "email_send", "instagram_send"):
        if args.get("draft") or getattr(ctx, "user_requested_send", False):
            return False                        # a draft, or the user asked for this message in their own words
        return True                             # the model decided to send on its own: ask
    return True                                    # power_action and anything new: ask
