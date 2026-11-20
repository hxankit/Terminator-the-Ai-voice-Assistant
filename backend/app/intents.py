"""Fast path for everyday commands ("Alexa-style"): deterministic parsing -> one tool call -> short spoken reply.

Runs before the LLM, so common commands are instant even when the model is slow (CPU-only). Anything not
matched with confidence returns None and goes to the normal agent pipeline.
"""
import json, re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable
from . import desktop

NUM_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
             "nine": 9, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "forty five": 45,
             "sixty": 60, "ninety": 90, "half an": 0.5, "half a": 0.5}
NUM = r"(\d+|half an|half a|forty five|" + "|".join(k for k in NUM_WORDS if " " not in k) + ")"
UNIT = r"(seconds?|secs?|minutes?|mins?|hours?|hrs?)"
KNOWN_SITES = {
    "youtube": "https://www.youtube.com", "gmail": "https://mail.google.com", "google": "https://www.google.com",
    "github": "https://github.com", "whatsapp": "https://web.whatsapp.com", "facebook": "https://www.facebook.com",
    "instagram": "https://www.instagram.com", "twitter": "https://x.com", "linkedin": "https://www.linkedin.com",
    "netflix": "https://www.netflix.com", "amazon": "https://www.amazon.in", "chatgpt": "https://chatgpt.com",
    "maps": "https://maps.google.com", "google maps": "https://maps.google.com", "calendar": "https://calendar.google.com",
    "google drive": "https://drive.google.com", "drive": "https://drive.google.com", "spotify": "https://open.spotify.com",
}
WAKE_AND_POLITE = re.compile(
    r"^((hey |ok |okay )?(jarvis|terminator|assistant)[,!.]?\s+)?((please|kindly|can you|could you|would you|will you|"
    r"i want you to|i'd like you to|i want to|i'd like to)\s+)*", re.I)
TRAILING = re.compile(r"(\s+(please|for me|now|right now|thanks|thank you))*[\s.!?]*$", re.I)


@dataclass
class Intent:
    name: str
    tool: str | None = None
    args: dict = field(default_factory=dict)
    say: Callable[[dict], str] = lambda r: "Done."
    reply: str | None = None          # direct answer without a tool
    approved: bool = False            # the user's own words asked for exactly this action (e.g. "send ...")


def clean(text: str) -> str:
    t = re.sub(r"\s+", " ", text.strip())
    t = WAKE_AND_POLITE.sub("", t)
    return TRAILING.sub("", t).strip()


def to_number(s: str) -> float:
    s = s.lower().strip()
    return float(s) if s.isdigit() else NUM_WORDS[s]


def to_seconds(n: str, unit: str) -> int:
    u = unit.lower()
    mult = 3600 if u.startswith(("hour", "hr")) else 60 if u.startswith("min") else 1
    return int(to_number(n) * mult)


def human_duration(seconds: int) -> str:
    parts = []
    for size, name in ((3600, "hour"), (60, "minute"), (1, "second")):
        n, seconds = divmod(seconds, size)
        if n:
            parts.append(f"{n} {name}{'s' if n != 1 else ''}")
    return " and ".join(parts[:2]) or "0 seconds"


def say_time(dt: datetime) -> str:
    return dt.strftime("%-I:%M %p")


def _at_time(h: int, m: int, ampm: str | None, day: str | None, ref: datetime) -> datetime:
    ampm = (ampm or "").replace(".", "").lower()
    if ampm == "pm" and h < 12:
        h += 12
    if ampm == "am" and h == 12:
        h = 0
    if h > 23 or m > 59:
        raise ValueError("bad time")
    target = ref.replace(hour=h, minute=m, second=0, microsecond=0)
    if day and day.lower() == "tomorrow":
        return target + timedelta(days=1)
    if target <= ref and not ampm and h < 12 and target + timedelta(hours=12) > ref:
        return target + timedelta(hours=12)          # "at 6" in the afternoon means 6 pm
    return target if target > ref else target + timedelta(days=1)


def _reminder(what: str, due: datetime, ref: datetime) -> Intent:
    what = re.sub(r"^(to|about)\s+", "", what.strip(), flags=re.I)
    when = say_time(due) + ("" if due.date() == ref.date() else " tomorrow" if (due.date() - ref.date()).days == 1
                            else due.strftime(" on %A"))
    return Intent("reminder", "task_create", {"title": what[:1].upper() + what[1:], "due_at": due.isoformat()},
                  lambda r, w=what, t=when: f"Okay, I'll remind you to {w} at {t}.")


def _volume(r: dict) -> str:
    return "Muted." if r.get("muted") else f"Volume is at {r.get('percent')} percent."


def match(text: str, now: datetime | None = None, email_names: set[str] | None = None,
          ms_connected: bool = False, gmail_connected: bool = False, insta_connected: bool = False) -> Intent | None:
    """email_names: lowercased names/nicknames of the user's contacts that have an email (Teams prefill).
    ms_connected: Microsoft 365 is signed in, so Teams/Outlook can be used by name and send directly."""
    t = clean(text)
    if not t or len(t) > 300:
        return None
    # message bodies keep trailing words like "now" / "thanks"; only leading wake/polite words are removed
    t_msg = WAKE_AND_POLITE.sub("", re.sub(r"\s+", " ", text.strip())).rstrip(" .!?")
    fm = lambda pat: re.fullmatch(pat, t_msg, re.I | re.S)   # like f(), but keeps message text intact
    ref = (now or datetime.now()).astimezone()
    f = lambda pat: re.fullmatch(pat, t, re.I)

    # --- time & date
    if f(r"(what('s| is) the )?time( is it)?|what time is it|tell me the time|time please"):
        return Intent("time", reply=f"It's {say_time(ref)}.")
    if f(r"(what('s| is) )?(the |today's )?date( today)?|what day is (it|today)|what('s| is) today('s date)?"):
        return Intent("date", reply=f"Today is {ref.strftime('%A, %-d %B %Y')}.")
    if f(r"(never ?mind|cancel|forget it|stop|shut up|be quiet|nothing)"):
        return Intent("cancel", reply="Okay.")

    # --- small talk (instant; no reason to wake a slow model for a greeting)
    if f(r"(h+i+|he+y+|hello+|hiya|yo|namaste|hola|greetings|good (morning|afternoon|evening))( there| terminator| jarvis| buddy| friend)?[!.]*"):
        part = "morning" if ref.hour < 12 else "afternoon" if ref.hour < 17 else "evening"
        return Intent("greeting", reply=f"Good {part}! How can I help?")
    if f(r"(thanks|thank you|thank u|thx|ty|cheers)( (so|very) much| a lot| a ton)?( (terminator|jarvis))?[!.]*"):
        return Intent("thanks", reply="You're welcome!")
    if f(r"how are you( doing)?( today)?|how('s| is) it going|how are things|what'?s up|sup"):
        return Intent("how_are_you", reply="I'm doing well, thanks for asking! What can I do for you?")
    if f(r"(bye|good ?bye|see you( later)?|see ya|good night|that'?s all( for now)?)[!.]*"):
        return Intent("bye", reply="Good night!" if "night" in t.lower() else "Goodbye!")

    # --- volume
    if m := f(r"(set |change |turn |put )?(the )?(volume|sound) (to |at )?(?P<n>\d{1,3})( ?%| percent)?"):
        return Intent("volume", "set_volume", {"level": int(m["n"])}, _volume)
    if m := f(r"((turn|pump) (up )?(the )?(volume|sound)( up)?|turn it up|(increase|raise) (the )?(volume|sound)|(the )?volume up|louder|make it louder)"
              r"( by (?P<n>\d{1,3})( ?%| percent)?)?"):
        return Intent("volume", "set_volume", {"change": int(m["n"] or 10)}, _volume)
    if m := f(r"((turn) (down )?(the )?(volume|sound)( down)?|turn it down|(decrease|lower|reduce) (the )?(volume|sound)|(the )?volume down|quieter|softer|make it quieter)"
              r"( by (?P<n>\d{1,3})( ?%| percent)?)?"):
        return Intent("volume", "set_volume", {"change": -int(m["n"] or 10)}, _volume)
    if m := f(r"(?P<un>un)?mute( (the )?(volume|sound|audio|speakers?|computer|pc))?"):
        return Intent("volume", "set_volume", {"mute": not m["un"]}, _volume)

    # --- media
    if f(r"pause( (the )?(music|song|video|playback|media))?|stop (the )?(music|song|video|playback|media)"):
        return Intent("media", "media_control", {"action": "pause"}, lambda r: "Paused.")
    if f(r"(resume|unpause|continue)( (the )?(music|song|video|playback))?|play( (the )?(music|song|video))?"):
        return Intent("media", "media_control", {"action": "play"}, lambda r: "Playing.")
    if f(r"(next|skip)( (this|the))?( song| track| video)?|play (the )?next( song| track| video)?"):
        return Intent("media", "media_control", {"action": "next"}, lambda r: "Next track.")
    if f(r"(previous|last)( song| track| video)|go back a (song|track)|play (the )?previous( song| track| video)?"):
        return Intent("media", "media_control", {"action": "previous"}, lambda r: "Previous track.")
    if m := f(r"play (?P<q>.+?) on youtube|(search )?youtube (for )?(?P<q2>.+)|play (?P<q3>.+)"):
        q = m["q"] or m["q2"] or m["q3"]
        return Intent("youtube", "play_youtube", {"query": q}, lambda r, q=q: f"Playing {q} on YouTube.")

    # --- screen
    if f(r"lock( the| my)? (screen|computer|pc|system|laptop|desktop)"):
        return Intent("lock", "lock_screen", {}, lambda r: "Locking the screen.")

    # --- timers
    if m := (f(rf"(set |start )?(a |an )?{NUM}[ -]{UNIT}[ -]timer( (for|called|named) (?P<label>.+))?") or
             f(rf"(set |start )?(a |an )?timer (for )?{NUM} ?{UNIT}( (for|called|named) (?P<label>.+))?")):
        g = [x for x in m.groups() if x]
        n = next(x for x in g if re.fullmatch(NUM, x, re.I))
        unit = next(x for x in g if re.fullmatch(UNIT, x, re.I))
        secs = to_seconds(n, unit)
        label = (m["label"] or "Timer").strip()
        return Intent("timer", "set_timer", {"seconds": secs, "label": label[:1].upper() + label[1:]},
                      lambda r, s=secs: f"Timer set for {human_duration(s)}.")

    # --- reminders
    if m := (f(rf"remind me (?P<what>.+?) in {NUM} {UNIT}") or f(rf"remind me in {NUM} {UNIT} (?P<what>.+)")):
        g = [x for x in m.groups() if x]
        n = next(x for x in g if re.fullmatch(NUM, x, re.I) and x != m["what"])
        unit = next(x for x in g if re.fullmatch(UNIT, x, re.I))
        return _reminder(m["what"], ref + timedelta(seconds=to_seconds(n, unit)), ref)
    if m := (f(r"remind me (?P<what>.+?) at (?P<h>\d{1,2})(:(?P<m>\d{2}))? ?(?P<ap>am|pm|a\.m\.|p\.m\.)?( (?P<day>today|tomorrow))?") or
             f(r"remind me (?P<day>tomorrow |today )?at (?P<h>\d{1,2})(:(?P<m>\d{2}))? ?(?P<ap>am|pm|a\.m\.|p\.m\.)?( (?P<day2>tomorrow|today))? (?P<what>.+)")):
        try:
            due = _at_time(int(m["h"]), int(m["m"] or 0), m["ap"],
                           (m["day"] or "").strip() or (m.groupdict().get("day2") or None), ref)
        except ValueError:
            return None
        return _reminder(m["what"], due, ref)

    # --- Microsoft Teams (prefilled; the user presses Enter). Only when we already know the person's email,
    #     otherwise the agent asks for it and saves the contact.
    MSG = r"(message|messgae|mesage|massage|msg|send|text|ping|tell|write|draft|prepare|type|say)"
    TEAMS = r"(ms |microsoft )?teams"
    if m := (fm(rf"{MSG}( a)? (?P<msg>.+?) to (?P<to>[^,:]+?) (in|on|via|using|over) {TEAMS}") or
             fm(rf"(send )?(a )?{TEAMS}( message| msg)? to (?P<to>[^,:]+?)(,|:| saying| that says| -) ?(?P<msg>.+)") or
             fm(rf"{MSG} (?P<to>[^,:]+?) (in|on) {TEAMS}(,|:| saying| that)? (?P<msg>.+)")):
        to = m["to"].strip().lower()
        known = bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", to)) or to in (email_names or set())
        if ms_connected or (desktop.unavailable() is None and known):
            msg = re.sub(r"^that\s+", "", m["msg"].strip(), flags=re.I)
            draft = bool(re.match(r"\s*(draft|prepare|type|write)\b", t_msg, re.I))
            return Intent("teams", "teams_message", {"to": m["to"].strip(), "message": msg[:1].upper() + msg[1:], "draft": draft},
                          lambda r: f"Sent to {r.get('to')} on Teams." if r.get("sent") else
                          f"Teams is open with your message to {r.get('to')}. Press Enter to send it.", approved=True)
        return None

    # --- Instagram DMs (connected): "message riya on instagram saying hi", "dm riya on insta: hi"
    IG = r"(instagram|insta|ig)"
    if insta_connected and (m := (
            fm(rf"{MSG}( a)? (?P<msg>.+?) to (?P<to>[^,:]+?) (in|on|via|over) {IG}") or
            fm(rf"(send |dm )?(an? )?{IG}( message| msg| dm)? to (?P<to>[^,:]+?)(,|:| saying| that says| -) ?(?P<msg>.+)") or
            fm(rf"({MSG}|dm) (?P<to>[^,:]+?) (in|on|via) {IG}(,|:| saying| that)? (?P<msg>.+)"))):
        msg = re.sub(r"^that\s+", "", m["msg"].strip(), flags=re.I)
        return Intent("instagram", "instagram_send", {"to": m["to"].strip(), "message": msg[:1].upper() + msg[1:]},
                      lambda r: f"Sent to {r.get('to')} on Instagram.", approved=True)

    # --- email: Outlook (Microsoft 365) and/or Gmail
    acct = "gmail" if re.search(r"\bgmail\b", t_msg, re.I) else "outlook" if re.search(r"\boutlook\b", t_msg, re.I) else "auto"
    if (ms_connected or gmail_connected) and (m := (
            fm(r"(send |draft |write )?(an |a )?(e-?mail|gmail|mail) (to )?(?P<to>[^,:]+?) (about|regarding|re|with (the )?subject|subject) "
               r"(?P<subj>.+?)(,|:| saying| that says| -) ?(?P<body>.+)") or
            fm(r"(send |draft |write )?(an |a )?(e-?mail|gmail|mail) (to )?(?P<to>[^,:]+?)(,|:| saying| that says| -) ?(?P<body>.+)"))):
        to = re.sub(r"\s+(on|via|from|using) (my )?(gmail|outlook)$", "", m["to"].strip(), flags=re.I)
        body = re.sub(r"^that\s+", "", m["body"].strip(), flags=re.I)
        subj = (m.groupdict().get("subj") or " ".join(body.split()[:6])).strip()
        draft = bool(re.match(r"\s*(draft|write)\b", t_msg, re.I))
        return Intent("email", "email_send", {"to": to, "subject": subj[:1].upper() + subj[1:], "body": body[:1].upper() + body[1:],
                                              "draft": draft, "account": acct},
                      lambda r: f"Email sent to {r.get('to')}." if r.get("sent") else
                      f"The email to {r.get('to')} is ready to send.", approved=True)
    if (ms_connected or gmail_connected) and f(r"(any |do i have )?(new |unread )?(e-?mails?|mails?|gmails?)( today)?|(check|read)( my)? (e-?mails?|mails?|inbox|gmail)"):
        return Intent("email_list", "email_list", {"unread_only": True, "limit": 5, "account": acct}, _say_inbox)

    # --- Outlook calendar (Microsoft 365 connected)
    if ms_connected:
        if dm := f(r"(what('s| is)( on)? my (calendar|schedule|agenda)|(do i have |any |my )?meetings|am i free)"
                   r"( for| on)?( (?P<day>today|tomorrow|this week))?"):
            day = {"this week": "week"}.get(dm["day"] or "today", dm["day"] or "today")
            return Intent("calendar", "calendar_list", {"day": day}, _say_calendar)
    # --- WhatsApp (prefilled; the user presses Enter)
    if m := (fm(rf"{MSG}( a)? (?P<msg>.+?) to (?P<to>[^,:]+?) (in|on|via|using|over) whats ?app") or
             fm(r"(send |draft |prepare |type |write )?(a )?(whats ?app|wa)( message| msg| text)? to (?P<to>[^,:]+?)(,|:| saying| that says| -) ?(?P<msg>.+)") or
             fm(r"(message|text|tell) (?P<to>[^,:]+?) on whats ?app(,|:| saying| that)? (?P<msg>.+)") or
             fm(r"whats ?app (?P<to>[^,:]+?)(,|:| saying) ?(?P<msg>.+)")):
        to = m["to"].strip()
        msg = re.sub(r"^that\s+", "", m["msg"].strip(), flags=re.I)
        draft = bool(re.match(r"\s*(draft|prepare|type|write)\b", t_msg, re.I))
        return Intent("whatsapp", "whatsapp_send", {"to": to, "message": msg[:1].upper() + msg[1:], "draft": draft},
                      lambda r: f"Sent to {r.get('to')}." if r.get("sent") else
                      f"WhatsApp is open with your message to {r.get('to')}. Press Enter to send it.", approved=True)

    # --- system health (real numbers, instantly)
    if f(r"(system|computer|pc|laptop|machine|server)( health| status| stats| info| performance| check| report)"
         r"|(check|how is|how's|hows)( the| my)? (system|computer|pc|laptop|machine)( health| doing| running| status)?"
         r"|(is )?(my |the )?(system|computer|pc|laptop) (slow|ok|okay|fine|healthy)"
         r"|(cpu|ram|memory|disk|storage) (usage|status|space|left|free|used)|how much (ram|memory|disk|space|storage)( is)?( left| free| used)?"
         r"|battery( level| status)?|health check|uptime"):
        from .tools.system import describe_health
        return Intent("system_health", "system_info", {}, lambda r: describe_health(r) if r.get("cpu") else "Done.")

    # --- environment: save / restore ("open previous environment"; typos like "envirnment" are fine)
    ENV = r"(env\w*|session|workspace|setup|work ?space|tabs)"
    if f(rf"(save|remember|snapshot|store)( my| the| this| current)*( current)? {ENV}( for later| for tomorrow)?"):
        return Intent("save_env", "save_environment", {},
                      lambda r: f"Saved. I'll remember: {r.get('summary', 'what is open')}.")
    if f(rf"(open|restore|bring back|reopen|load|resume|start|get back)( my| the)? (previous|last|old|saved|earlier|yesterday'?s?)( {ENV})?"
         rf"|(restore|reopen|bring back)( my| the)? {ENV}"):
        return Intent("restore_env", "restore_environment", {},
                      lambda r: (f"Restoring what you had open {r.get('saved', 'last time')}: " +
                                 ", ".join(r.get("restored", [])) + ".") if r.get("restored") else "Restored.")

    # --- power (approval is always required; a stray match just asks a question)
    TARGET = r"( (the|thw|my|this))?( (system|zystewm|sytem|computer|pc|laptop|machine|desktop))"
    POWER_SAY = {"shutdown": "Shutting down in a few seconds", "restart": "Restarting in a few seconds",
                 "logout": "Logging you out in a few seconds", "suspend": "Going to sleep in a few seconds"}
    def power(action):
        return Intent("power", "power_action", {"action": action, "save_environment": action != "suspend"},
                      lambda r, a=action: POWER_SAY[a] + (f". I saved: {r['saved']}. Say 'open previous environment' "
                                                          "when you're back." if r.get("saved") else "."))
    if f(rf"(shut ?d[ow]\w*|power ?off)({TARGET})?( now)?|(turn|switch) off{TARGET}( now)?"):
        return power("shutdown")
    if f(rf"(restart|reboot)({TARGET})?( now)?"):
        return power("restart")
    if f(rf"(log ?out|sign ?out|log me out)({TARGET})?"):
        return power("logout")
    if f(rf"(suspend({TARGET})?|put{TARGET} to sleep|go to sleep)"):
        return power("suspend")

    # --- close a running app (approval first; unsaved work could be lost)
    if m := f(r"(?P<force>force )?(close|quit|exit|kill|stop)( the| my)? (?P<what>[\w .+-]+?)( app| application| window| program)?"):
        if desktop.unavailable() is None:
            from .tools.desktop import find_running
            if find_running(m["what"]):
                return Intent("close_app", "close_app", {"name": m["what"], "force": bool(m["force"])},
                              lambda r: ((f"Closed {', '.join(r['closed'])}." if r.get("closed") else "") +
                                         (f" {', '.join(r['still_open'])} is still open; it may be asking you to save."
                                          if r.get("still_open") else "")).strip() or "Done.")

    # --- open a folder in an app ("open inventroy folder in vscode")
    if m := f(r"(open|launch|start|load)( the| my)? (?P<what>.+?)( folder| directory| dir| project| repo| repository| workspace)?"
              r" (in|with|using|on) (?P<app>[\w .+-]+?)( app| application| editor)?"):
        if desktop.unavailable() is None:
            app, _ = desktop.match_app(m["app"])
            if app:
                path, _ = desktop.resolve_dir(m["what"])
                if path:
                    return Intent("open_with", "open_with", {"app": m["app"], "path": m["what"]},   # tool resolves it
                                  lambda r, n=app["name"], p=path: f"Opening {p.rstrip('/').split('/')[-1]} in {n}.")

    # --- open websites / apps
    if m := f(r"(open|go to|visit|browse to) (?P<site>[\w-]+(\.[\w-]+)+(/\S*)?)"):
        site = m["site"]
        return Intent("open_url", "open_url", {"url": site}, lambda r, s=site: f"Opening {s}.")
    if m := f(r"(open|launch|start|run|show)( the| my)? (?P<what>[\w .+-]+?)( app| application| website| site)?"):
        what = m["what"].strip()
        if desktop.unavailable() is None:
            app, _ = desktop.match_app(what)
            if app:
                return Intent("open_app", "open_app", {"name": what}, lambda r, n=app["name"]: f"Opening {n}.")
        if url := KNOWN_SITES.get(what.lower()):
            return Intent("open_url", "open_url", {"url": url}, lambda r, n=what: f"Opening {n}.")
    return None


def _say_calendar(r: dict) -> str:
    ev = r.get("events") or []
    when = {"today": "today", "tomorrow": "tomorrow", "week": "this week"}.get(r.get("day"), "today")
    if not ev:
        return f"Nothing on your calendar {when}."
    items = "; ".join(f"{e['start'] if r.get('day') == 'week' else e['start'].split(' ', 3)[-1]} {e['subject']}" for e in ev[:6])
    return f"You have {len(ev)} event{'s' * (len(ev) != 1)} {when}: {items}." + (" And more." if len(ev) > 6 else "")


def _say_inbox(r) -> str:
    mails = r if isinstance(r, list) else []
    if not mails:
        return "No unread emails."
    items = "; ".join(f"{m.get('from')}: {m.get('subject')}" for m in mails[:5])
    return f"Latest unread: {items}."


def reply_for(intent: Intent, result: dict) -> str:
    """Spoken reply for a finished fast-path action (tool result dict from tools.execute)."""
    if result["status"] != "ok":
        msg = result["output"].removeprefix("Tool '" + (intent.tool or "") + "' is not available: ")
        if "isn't linked" in msg or "isn't connected" in msg or "bridge" in msg:
            return msg                                   # already a full, friendly explanation
        return f"Sorry, I couldn't do that: {msg}"
    try:
        data = json.loads(result["output"])
    except (json.JSONDecodeError, TypeError):
        data = {}
    return intent.say(data if isinstance(data, (dict, list)) else {})
