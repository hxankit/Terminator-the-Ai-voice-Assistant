"""Environment save/restore and power actions (shutdown, restart, log out, suspend)."""
import shutil
from datetime import datetime
from typing import Literal
from pydantic import BaseModel
from .. import desktop, environment
from .base import tool, ToolError, ToolContext

NEEDS_DESKTOP = desktop.unavailable
POWER_DELAY_S = 6        # time for the reply to be shown / spoken before the session ends


class Empty(BaseModel):
    pass


def _when(dt: datetime) -> str:
    local = dt.astimezone()
    today = datetime.now().astimezone().date()
    day = "today" if local.date() == today else "yesterday" if (today - local.date()).days == 1 else local.strftime("%A %-d %B")
    return f"{day} at {local.strftime('%-I:%M %p')}"


@tool("save_environment", "Save what is open right now (browser windows and tabs, VS Code projects, terminals, apps) "
      "so it can be reopened later with restore_environment.", Empty, risk="write", requires=NEEDS_DESKTOP)
async def save_environment(a: Empty, ctx: ToolContext):
    doc = await environment.save(ctx.user["_id"], "manual")
    if environment.is_empty(doc):
        raise ToolError("Nothing is open that I can save right now.")
    return {"saved": True, "summary": doc["summary"]}


@tool("restore_environment", "Reopen the previously saved environment (browser tabs, VS Code projects, terminals, apps), "
      "e.g. when the user says 'open previous environment' after a restart.", Empty, risk="write", requires=NEEDS_DESKTOP)
async def restore_environment(a: Empty, ctx: ToolContext):
    snap = await environment.latest(ctx.user["_id"])
    if not snap:
        raise ToolError("I don't have a saved environment yet. Say 'save my environment', or shut down through me, "
                        "and I'll remember what was open.")
    opened = environment.restore(snap)
    return {"restored": opened, "saved": _when(snap["created_at"]), "summary": snap["summary"]}


class PowerArgs(BaseModel):
    action: Literal["shutdown", "restart", "logout", "suspend"]
    save_environment: bool = True


COMMANDS = {
    "shutdown": ("gnome-session-quit --power-off --no-prompt", "systemctl poweroff"),
    "restart": ("gnome-session-quit --reboot --no-prompt", "systemctl reboot"),
    "logout": ("gnome-session-quit --logout --no-prompt", "loginctl terminate-user $USER"),
    "suspend": ("systemctl suspend", "systemctl suspend"),
}


@tool("power_action", "Shut down, restart, log out or suspend this computer. First saves the open environment so it can "
      "be reopened with 'open previous environment'. Always requires the user's approval.", PowerArgs,
      risk="approval", requires=NEEDS_DESKTOP)
async def power_action(a: PowerArgs, ctx: ToolContext):
    saved = None
    if a.save_environment and a.action != "suspend":
        doc = await environment.save(ctx.user["_id"], a.action)
        saved = None if environment.is_empty(doc) else doc["summary"]
        environment.freeze_autosave()           # apps are about to close: keep this snapshot as the latest
    graceful, fallback = COMMANDS[a.action]
    if not shutil.which(graceful.split()[0]):
        graceful = fallback
    # graceful first: GNOME asks apps to quit (Chrome and VS Code save their state); plain systemd as a fallback
    desktop.spawn_detached(["bash", "-c", f"sleep {POWER_DELAY_S}; {graceful} || {fallback}"])
    return {"action": a.action, "in_seconds": POWER_DELAY_S, "saved": saved}
