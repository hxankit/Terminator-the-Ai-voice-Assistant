# Terminator: a personal AI assistant

A self-hosted assistant with specialized agents, tool use, persistent memory, a document knowledge base (RAG),
and two-way communication: streaming over WebSocket, voice conversation, clarifying questions, and messages
the assistant starts on its own (such as reminders).

It can also act like a smart speaker for your PC. Say **"Hey Jarvis"** and it handles volume, media, apps,
websites, timers, reminders and WhatsApp messages instantly. Other requests go to the AI agents, which can also
run terminal commands after you approve each one.

- **Backend:** Python 3.12 · FastAPI · async PyMongo (MongoDB) · httpx
- **Frontend:** React 19 · TypeScript · Vite · Tailwind CSS 4
- **LLMs:** any OpenAI-compatible endpoint, such as Ollama, Groq, Cloudflare Workers AI, OpenAI, OpenRouter, LM Studio, or vLLM. Each user can configure several, with automatic fallback.

---

## Status

| Area | State |
|---|---|
| Auth (bcrypt, JWT httpOnly cookie or Bearer), per-user data isolation | ✅ working, tested |
| Chat streaming (WebSocket, with SSE fallback), stop, regenerate, history, search | ✅ working, tested (API + browser) |
| Provider/model management, encrypted API keys, model discovery, **verified** tool-calling probe | ✅ working, tested |
| Intent routing → 8 specialized agents | ✅ working, tested |
| Tool framework (validation, risk levels, approval, execution log, untrusted-output wrapping) | ✅ working, tested |
| Clarifying questions (`ask_user`) and approvals that pause and resume a turn | ✅ working, tested (API + browser) |
| Assistant-initiated messages (reminder scheduler, memory-saved notices) pushed live | ✅ working, tested (API + browser) |
| Memory: long-term, preference, project; selective auto-extraction; secret filter; manage UI | ✅ working, tested |
| RAG: PDF/DOCX/TXT/MD/CSV/XLSX/JSON → chunks → embeddings (or keyword fallback) → cited answers | ✅ working, tested |
| Sandboxed Python (bubblewrap: no network, read-only system, rlimits), file outputs | ✅ working, tested |
| Excel/file generation and download | ✅ working, tested |
| Host diagnostics (`system_info`; `run_command`/`read_log` allow-listed and approval-gated) | ✅ working, tested |
| Token usage and latency tracking, request IDs, centralized logging | ✅ working |
| Quick actions: time, volume, media, apps, websites, YouTube, lock, timers, reminders, WhatsApp (no LLM, instant) | ✅ working, tested (fake desktop) |
| PC & Home Control agent: apps, files, find files, volume, media, notifications, timers, contacts, WhatsApp, `run_shell` 🔒 | ✅ working, tested; tool choice verified with real llama3.1 |
| Contacts (nicknames, `.vcf` import) and WhatsApp **prefill** (you press Enter) | ✅ working, tested |
| Always-on voice service (wake word → Whisper → Terminator → Piper), spoken approvals, questions, reminders | ✅ full pipeline verified by **simulation** (synthetic speech); ⚠️ not yet with a live mic (see Microphone) |
| Browser voice mode (Chrome speech recognition, hands-free loop) | ⚠️ built; speech queue unit-tested; not tested with a real mic |
| Keep-warm for local models, prompt-cache-friendly prompts, `llama3.1-8k` Modelfile | ✅ working, measured (see Local models on CPU) |
| Save/restore environment (Chrome tabs, VS Code projects, terminals, apps) + shutdown/restart/log out with confirmation | ✅ working, tested (synthetic session file, fake desktop); capture verified on the real desktop |
| Gmail (app password, IMAP/SMTP) and Instagram DMs (instagrapi, unofficial) | ✅ built; tested with fake servers; ⚠️ need your sign-in in Settings |
| Conversation-aware routing (follow-ups keep their agent) and no approval prompt for sends you asked for | ✅ working, tested |
| Microsoft 365 via Graph: Teams 1:1 chats by name, Outlook send/read, calendar; device-code sign-in | ✅ built; tested against a simulated Graph; ⚠️ needs your app registration + sign-in to use |
| WhatsApp bridge (lharries/whatsapp-mcp): direct send, read chats, QR linking in Settings | ✅ built and running; tested against a fake bridge; ⚠️ real sending not yet tested (needs your QR scan) |
| Cloudflare Workers AI provider (account detection, model discovery) + automatic fallback between providers | ✅ working, tested against a simulated Cloudflare API; ⚠️ not yet with a real Cloudflare account |
| Open a folder in an app ("open inventroy folder in vscode", fuzzy + typo tolerant) | ✅ working, tested; resolution verified on the real home folder |
| Close apps with confirmation (incl. LibreOffice/WPS as "excel"), read-only command runner, system health | ✅ working, tested; app matching verified on the real desktop |
| Wikipedia lookup (no key), user identity and document awareness in prompts, automatic re-indexing | ✅ working, tested |
| Server-side transcription fallback (`/api/voice/transcribe`) | ⚠️ needs a Whisper-compatible provider; only the "unsupported" path is tested |
| Web search (`web_search`) | ⚠️ needs `SEARXNG_URL` or `TAVILY_API_KEY`; not tested against a live service |
| `fetch_url` | ⚠️ SSRF guard tested; live page fetching not covered by tests |
| Real LLM (Ollama llama3.1:8b) | ✅ embeddings, streaming, tool-calling probe and multi-turn tool choice verified against the real model. Automated tests use a scripted mock. |
| Docker image / PM2 config | ⚠️ written, **not run** (no Docker permission or PM2 on the build machine) |
| Email and calendar integration | ❌ not implemented (the Productivity agent says so if asked) |
| OCR for scanned PDFs, password reset, email verification | ❌ not implemented |

---

## Quick start (development)

Prerequisites: Python 3.12, Node 20+, MongoDB running locally, and `bubblewrap` (`sudo apt install bubblewrap`) for the code sandbox.

```bash
# backend
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env                      # dev defaults work; see Configuration
.venv/bin/uvicorn app.main:app --reload --port 8000

# frontend (second terminal)
cd frontend && npm install && npm run dev  # http://localhost:5173 (proxies /api and the WebSocket)
```

Register in the UI, then open **Models & settings → Add provider**. For a free local model:

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull llama3.1:8b && ollama pull nomic-embed-text
```

Then choose the **Ollama (local)** preset (`http://localhost:11434/v1`) and press **Test**. Test sends a real prompt and
probes tool calling. A model that fails the probe is marked `tools: no` and chats without tools. Grounding still works,
because knowledge-base passages are injected into the prompt.

> Hardware: 8B models need about 6–8 GB of RAM/VRAM. Smaller models (3B) are faster but less reliable at tool calling;
> check the Test result.

---

## Alexa mode: voice and PC control

**Quick actions** run without the LLM, so they're instant even on a slow CPU. You can type them or say them:

| Say | Does |
|---|---|
| "what time is it", "what's the date" | answers |
| "volume to 40", "turn it up", "louder", "mute" | `wpctl` on the default speaker |
| "pause", "play", "next song", "previous track" | the active media player (MPRIS: Spotify, browser video, etc.) |
| "open chrome", "open calculator", "open youtube", "go to github.com" | launches the app or website |
| "play imagine dragons on youtube", "play lo-fi beats" | opens the top YouTube result |
| "lock the screen" | GNOME screen lock |
| "set a timer for 5 minutes", "10 minute timer for pasta" | timer, then a spoken and desktop alert |
| "remind me to call mom at 6 pm", "remind me in 20 minutes to stretch" | reminder |
| "send a WhatsApp to Mom saying I'm running late", "whatsapp Rahul: meeting at 5" | opens WhatsApp Web with the chat and text filled in; **you press Enter** |
| "open inventory folder in vscode", "open terminator project in files" | finds the folder by name (typos are fine) and opens it in that app |
| "save my environment" | snapshots open Chrome tabs, VS Code projects, terminals and apps |
| "shut down the system", "restart", "log out", "suspend" | **asks first**, saves your environment, then quits apps gracefully and powers off |
| "open previous environment" | reopens everything from the last snapshot |
| "hi", "thanks", "how are you", "bye" | answered instantly |
| "system health", "is my pc slow", "how much disk space is left", "battery" | real CPU, memory, disk, swap, temperature and busiest apps, with warnings |
| "close excel", "close teams", "force close chrome" | **asks first** (unsaved work could be lost), then asks the app to quit; "excel/word/powerpoint" map to your installed office apps |

Anything else goes to the agents. For questions about the computer they **find the right commands and run them**
with `inspect_system`, which needs no approval but only accepts read-only programs and subcommands. It allows pipes,
but not `;`, `&&`, redirection, `$(…)` or `sudo`, and never touches private files (keys, `.env`, browser profiles).
Agents answer from the real output. The **PC & Home Control** agent can also find and open files, use contacts, and
run **any terminal command after you approve that exact command**. A few catastrophic patterns (`rm -rf ~`, `mkfs`,
`dd of=/dev/…`, fork bombs) are blocked even if you approve them. Disable desktop control with `DESKTOP_CONTROL=false`;
it switches itself off on machines without a graphical session.

**WhatsApp (real sending):** Terminator uses the Go bridge from
[lharries/whatsapp-mcp](https://github.com/lharries/whatsapp-mcp) in `integrations/whatsapp-mcp`. It links to your
account like WhatsApp Web (whatsmeow) and runs as the `terminator-whatsapp` user service. Link it once under
**Settings → WhatsApp** by scanning the QR code with your phone (WhatsApp → Linked devices → Link a device).

- "send a WhatsApp to Arjun saying hi" and "message hi to mom on whatsapp" **send directly**; people and groups are found by the names saved on your phone.
- "draft a whatsapp to … saying …" only types it into WhatsApp Web.
- If the model decides to send something on its own, it asks first.
- If the bridge is offline, Terminator falls back to typing the message into WhatsApp Web.
- `whatsapp_read` and `whatsapp_chats` let it read your recent chats.
- **Unofficial client:** WhatsApp could restrict the number.

Local changes to the upstream code:
- The API listens on `127.0.0.1` only. Upstream listened on all interfaces with no auth, which let anyone on your network send messages and read files.
- Session keys are private (`store/` is 0700).
- The QR code is handed to the Settings page.
- Updated to the current whatsmeow library, since the March 2025 version is rejected by WhatsApp. Building needs Go 1.24+.

**Microsoft 365 (Teams chats, Outlook mail, calendar):** set up under **Settings → Microsoft 365**. You create an
app registration in your organization's Entra portal (the steps are shown there; delegated permissions only, no
secret) and sign in with a device code. Terminator then acts **as you**:
- "say hi to Arjun Mehta on teams" finds the colleague in your directory and sends a 1:1 chat.
- "email Arjun about release saying the build is green" sends the email.
- "what's on my calendar today" and "any new emails" summarize your calendar and inbox.

The token cache is stored encrypted in MongoDB. Your own "send" commands go straight out; if the model decides
to send on its own, it asks first. Without sign-in, Teams and Outlook fall back to opening the message pre-filled
for you to send.

**Gmail:** connect under **Settings → Gmail** with a Google **app password**. It needs 2-Step Verification on your
Google Account, and the app password is stored encrypted. Terminator then uses IMAP/SMTP:
- "send a gmail to Priya saying …" sends, finding the address by name in your mail history.
- "any new gmails" summarizes unread mail.
- `email_list` searches with Gmail's own search syntax.

When both Outlook and Gmail are connected, "email …" uses Outlook unless you say "gmail".

**Instagram (unofficial):** connect under **Settings → Instagram** with your username and password. The password is
used once and never stored; 2FA and security codes are entered in the same card. It uses instagrapi, Instagram's
private app API, which is against Instagram's terms: security challenges, temporary blocks or a ban are possible.
- "message riya on instagram saying …" and "dm riya on insta: …" send a DM. Recipients are matched only among your DM threads and the accounts you follow, never Instagram-wide search.
- `instagram_read` shows recent chats or one conversation.

**Approvals for messages:** when *your* message asks to contact someone ("ask Mom on WhatsApp…", "tell her…",
"email Priya…"), the send goes straight out. A send the model starts on its own asks first.

Two MCP servers were evaluated and not used:
- **marlonluo2018/outlook-mcp-server** automates the Outlook *desktop* app through Windows COM (`pywin32`), so it is Windows-only.
- **InditexTech/mcp-teams-server** posts as a bot into one configured channel (Azure Bot plus a custom Teams app). It can't send private chats.

### Voice service (always-on wake word)

Fully offline after setup: openWakeWord ("hey jarvis"), faster-whisper `base` (int8, CPU), and Piper (`en_US-lessac-medium`).

```bash
cd voice
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/pip install --no-deps openwakeword
./terminator-voice login            # your Terminator email + password; stores a revocable device key only
./terminator-voice test-mic         # checks the microphone signal is usable
./terminator-voice run              # foreground; or install as a background service:
./terminator-voice install-service --enable    # systemd --user, starts at login; logs: journalctl --user -u terminator-voice -f
```

Models download on first use (wake word about 7 MB, Whisper base about 145 MB, Piper voice 63 MB) into
`~/.local/share/terminator-voice`. The Python environment takes about 730 MB.

How it behaves:
- A chime means it's listening. It stops recording when you stop talking.
- Replies are spoken sentence by sentence while they stream.
- If the model is slow it says "Working on it."
- Say the wake word while it's talking to interrupt.
- Questions and approvals are asked aloud; answer "yes" or "no". An unclear answer counts as no.
- If a reply ends with a question, you can answer without the wake word.
- Reminders and timers are announced.
- Everything said lands in a daily "Voice <date>" conversation in the app.

Test without a microphone: `./terminator-voice simulate recording.wav` runs a WAV file through the same pipeline.

### Microphone

`./terminator-voice test-mic` flags a dead (silent) or saturated input. On the original build machine the
analog input was **pinned at full scale** (input gain and Mic Boost at maximum, likely no mic connected). Fix it by
connecting a mic or headset, then lowering **Settings → Sound → Input volume** to about 60–70% and Mic Boost to 0–10 dB
(`alsamixer` → F4 → *Mic Boost*). A USB headset or webcam mic usually just works.

### Environment snapshots

A snapshot records your Chrome windows and tabs (read from Chrome's own session file, any profile), the
projects open in VS Code, the working folders of gnome-terminal tabs, and other running desktop apps, including
files they have open. It's taken when you shut down, restart or log out through Terminator, when you say
"save my environment", and **automatically every 10 minutes** (`ENV_AUTOSAVE_MINUTES`), so "open previous
environment" also works after a normal shutdown. Snapshots stay in your local MongoDB. Browse and restore them
under **Agents & activity → Saved environments**. Not captured: Files (Nautilus) windows (GNOME doesn't expose
them) and unsaved editor contents.

## Cloud models (Groq, Cloudflare Workers AI) with local fallback

**Groq** (fastest): create a key at console.groq.com/keys, then go to **Settings → Add provider → Groq**, paste the key,
**Save**, **Discover** (pick a current model), **Test**, and **Set default**. Groq also hosts Whisper, so
`/api/voice/transcribe` uses it automatically for browsers without built-in speech recognition. The free tier limits
tokens per minute; when you hit it, Terminator retries on your local model.

**Cloudflare Workers AI:**

A cloud model answers in seconds where a CPU-only local model takes tens of seconds to minutes.

1. In Cloudflare, go to **My Profile → API Tokens → Create Token** and give it the **Workers AI** permission.
2. In Terminator, go to **Settings → Add provider → Cloudflare Workers AI**, paste the token, then click **Detect account**. The base URL with your account ID is filled in.
3. Keep `@cf/meta/llama-3.3-70b-instruct-fp8-fast`, or click **Discover** to choose another model. Press **Test**; it verifies that tool calling works.
4. Click **Set default** on the Cloudflare model.

Keep the Ollama provider as well. If Cloudflare fails (daily free allowance used up, network down, outage),
Terminator **automatically retries the same message on your local model**. Choose a specific fallback model
under **Settings → Preferences**. Prompts sent to Cloudflare leave your machine; quick actions never do.

## Local models on CPU

Measured with llama3.1:8b on an i5-11400F (6 cores, no usable GPU), Turbo Boost **off**:

| Situation | Time |
|---|---|
| Cold prompt (model just loaded, ~2,200 tokens of instructions and tools) | 3–5 minutes |
| Warm follow-up turn (prompt cache hit) | 18–30 s |
| Quick action (no LLM) | under 1 s |

What Terminator does about it:
- **`llama3.1-8k`** ([deploy/ollama/llama3.1-8k.Modelfile](deploy/ollama/llama3.1-8k.Modelfile)) sets an 8192-token window. The default 4096 silently truncates prompts with tools. It also renders tool definitions in the system section, so they stay in the KV cache instead of being re-read every turn. Create it with `ollama create llama3.1-8k -f deploy/ollama/llama3.1-8k.Modelfile` and use that model name in Settings.
- **Stable prompts:** system prompt and tools are byte-identical across turns. Time, memories and document passages ride along with the latest message.
- **Keep-warm** (`KEEP_WARM_HOURS`, default 8): replays your last conversation to local models every 4 minutes with a one-token budget, so Ollama doesn't unload the model and drop the cache.
- **No hidden extra calls:** LLM routing is opt-in (Settings → Smart routing), and memory extraction only runs when a message looks memorable ("remember…", "I prefer…").

What you can do:
- Enable **Turbo Boost**. It was off on the build machine (`/sys/devices/system/cpu/intel_pstate/no_turbo = 1`, so the CPU never left 2.6 GHz).
- Switch the power profile to **Performance** (Settings → Power).
- Close memory-heavy apps. The model needs about 6 GB of free RAM.
- For Alexa-like speed on complex requests, use a fast cloud model (Groq, OpenAI, OpenRouter) as the provider.

## Configuration

All settings come from environment variables or `backend/.env`. See [backend/.env.example](backend/.env.example).

| Variable | Purpose |
|---|---|
| `APP_ENV` | `production` requires `JWT_SECRET` (≥32 chars) and `SECRET_KEY`, and enables `Secure` cookies |
| `JWT_SECRET` | Session signing key. In dev a random one is generated per start, so sessions reset on restart |
| `SECRET_KEY` | Fernet key that encrypts provider API keys at rest. In dev it's generated once into `data/.dev_fernet_key`. **Keep it stable** |
| `MONGO_URI`, `MONGO_DB` | Database |
| `CORS_ORIGINS` | Allowed browser origins; also used by the WebSocket origin check |
| `DEFAULT_PROVIDER_BASE_URL`, `DEFAULT_PROVIDER_API_KEY`, `DEFAULT_MODEL`, `DEFAULT_EMBEDDING_MODEL` | Optional provider shared by all users (read-only in the UI) |
| `SEARXNG_URL` or `TAVILY_API_KEY` | Enables `web_search` |
| `SYSTEM_COMMAND_ALLOWLIST` | Commands `run_command` may run (each run still needs approval) |
| `ALLOW_REGISTRATION` | Set `false` once your account exists |
| `MAX_UPLOAD_MB`, `RATE_LIMIT_PER_MINUTE`, `AUTH_RATE_LIMIT_PER_MINUTE`, `SANDBOX_TIMEOUT_S`, `REMINDER_INTERVAL_S` | Limits |
| `DESKTOP_CONTROL`, `SHELL_TIMEOUT_S` | Desktop control on/off; time limit for approved terminal commands |
| `KEEP_WARM_HOURS`, `KEEP_WARM_INTERVAL_S` | Keep local models loaded and cached after use (0 disables) |
| `ENV_AUTOSAVE_MINUTES` | Automatic environment snapshots (0 disables) |
| `TURN_TIMEOUT_S` | Safety stop for a reply that runs too long (default 30 min) |

Missing integrations never crash anything: tools that need configuration report why they're unavailable, both in
**Agents & activity** and to the model.

---

## Architecture

```
Browser (React)                                  FastAPI (single process)
┌──────────────────────┐   WebSocket /api/ws   ┌──────────────────────────────────────────────┐
│ Chat · Voice loop    │◀════════════════════▶│ realtime.py  hub · notifications · scheduler │
│ Approvals/Questions  │   REST /api/*  (SSE)  │ routers/     auth · chat · models · data     │
│ KB · Memory · Tasks  │──────────────────────▶│ orchestrator.py                              │
└──────────────────────┘                       │   route → agent → memory+KB → LLM⇄tools loop │
                                               │ agents/      8 agents (prompt + tool subset) │
                                               │ tools/       registry · risk · sandbox · web │
                                               │ rag/         extract · chunk · retrieve      │
                                               │ llm/         OpenAI-compatible client        │
                                               └───────┬─────────────────────┬────────────────┘
                                                       │                     │
                                                    MongoDB          LLM providers (Ollama, cloud)
```

**A turn** ([backend/app/orchestrator.py](backend/app/orchestrator.py)):
1. Persist the user message; inline small attachments, which are marked untrusted.
2. **Route**: keyword scoring first, then an LLM classifier if the scores are ambiguous. The user can also force an agent.
3. **Context**: preferences, relevant long-term and project memories, optional KB passages, and short-term history trimmed to about 60% of the model's context window.
4. **Loop** (up to 8 steps): stream the LLM. On tool calls, validate the arguments, check permissions, and execute. Results are wrapped as `trust="untrusted"` data and fed back.
5. An `approval` or `question` tool pauses the turn: the pending call is stored, and the turn resumes with `mode: "continue"` once it's resolved.
6. Save the answer with citations and usage. Background memory extraction may save one to three durable facts and notify you.

Turns run as their own asyncio task that publishes to a queue. A disconnect or stop never loses partial output, and a
reconnecting client reloads the persisted result.

### Agents and tools

| Agent | Tools (besides memory and `ask_user`) |
|---|---|
| General | KB, web search/fetch, task_create |
| Software Developer | KB, `run_python`, web, create_file |
| AI/ML Engineer | KB, `run_python`, web, create_file |
| Researcher | web_search, fetch_url, create_file |
| Data Analyst | KB, profile_table, `run_python`, create_excel, create_file |
| System Administrator | KB, system_info, **run_command 🔒**, **read_log 🔒**, web |
| PC & Home Control | open_app, open_url, play_youtube, open_file, find_files, set_volume, media_control, lock_screen, show_notification, set_timer, task_create, contacts_search, whatsapp_send, system_info, **run_shell 🔒** |
| Productivity | task_create / list / update |
| Writer | KB, create_file |

Risk levels ([backend/app/tools/base.py](backend/app/tools/base.py)): `read` and `write` (your own app data) run
automatically; `sandboxed` runs isolated; `approval` 🔒 needs your click every time; `question` waits for your answer.
Every run is logged in `tool_runs` and shown in **Agents & activity**.

### Two-way communication

| Feature | How |
|---|---|
| Realtime channel | `GET /api/ws`, authenticated by session cookie (or `?token=`), with an Origin check. Client sends `chat`, `stop`, `decision`, `ping`; server sends every turn event tagged with `conversation_id`, plus `notification`, `resolved`, `error`. Auto-reconnect with backoff; the UI falls back to SSE if the socket is down. |
| Assistant-initiated | `realtime.reminder_loop` claims due tasks atomically and calls `notify()`, which stores the message and pushes it live: toast, bell, and a desktop notification if you allow it. Memory saves notify too. |
| Clarifying dialogue | Agents call `ask_user(question, options)`; the UI shows a card, and your answer becomes the tool result. |
| Voice | Headphones button = hands-free loop: listen → send → speak the reply sentence by sentence *while it streams* (code blocks skipped) → listen again. Tap to interrupt. Questions are read aloud and answered by voice. Uses the browser's speech recognition (Chrome/Edge); other browsers record audio and use `/api/voice/transcribe`. |

### Data model (MongoDB)

`users` · `conversations` · `messages` (user, assistant with `tool_calls`, tool; grouped by `turn_id`) · `providers`
(encrypted keys, per-model config and verified capabilities) · `memories` · `projects` · `documents` · `chunks` (text,
location, optional embedding) · `tasks` · `tool_runs` · `notifications` · `usage` · `generated_files`.
Indexes are created at startup ([backend/app/db.py](backend/app/db.py)).

---

## API

Interactive OpenAPI docs: `http://localhost:8000/api/docs`.

| Area | Endpoints |
|---|---|
| Auth | `POST /api/auth/register` · `POST /api/auth/login` · `POST /api/auth/logout` · `GET /api/auth/me` · `PATCH /api/auth/prefs` |
| Conversations | `GET/POST /api/conversations` (search `?q=`, `?project_id=`) · `GET/PATCH/DELETE /api/conversations/{id}` · `POST /api/conversations/{id}/chat` (SSE; `mode` = send, continue, regenerate) · `POST /api/conversations/{id}/stop` |
| Realtime | `WS /api/ws` · `GET /api/notifications` · `POST /api/notifications/read` · `POST /api/voice/transcribe` |
| Models | `GET/POST /api/providers` · `PUT/DELETE /api/providers/{id}` · `GET /api/providers/{id}/discover` · `POST /api/providers/{id}/models/{model}/verify` |
| Tools | `GET /api/agents` · `GET /api/tools/runs` · `POST /api/tools/runs/{id}/decision` (`{approve}` or `{answer}`) |
| Knowledge | `POST/GET /api/documents` · `DELETE /api/documents/{id}` · `POST /api/kb/search` · `GET /api/files/{id}` |
| Memory, projects, tasks | `GET/POST /api/memories`, `PUT/DELETE /api/memories/{id}` · `/api/projects[/{id}]` · `/api/tasks[/{id}]` |
| Contacts | `GET/POST /api/contacts` (`?q=` fuzzy) · `PUT/DELETE /api/contacts/{id}` · `POST /api/contacts/import` (.vcf) |
| Devices | `POST/GET /api/auth/device-tokens` · `DELETE /api/auth/device-tokens/{id}` (long-lived keys for the voice service) |
| Environments | `GET /api/environments` · `POST /api/environments/{id}/restore` · `DELETE /api/environments/{id}` |
| Cloudflare | `POST /api/providers/cloudflare/accounts` (finds the account ID for a token; the token isn't stored by this call) |
| Ops | `GET /api/health` · `GET /api/usage` |

---

## Security model

- **Secrets**: provider keys are Fernet-encrypted and never returned by the API. The memory system refuses anything that looks like a credential.
- **Sessions**: bcrypt (cost 12), HS256 JWT in an `httpOnly`, `SameSite=Lax` cookie (`Secure` in production). An explicit `Authorization: Bearer` header wins over the cookie.
- **Abuse limits**: per-user and per-route rate limits, plus stricter per-IP limits on login and register.
- **Validation**: every request body and tool argument goes through Pydantic. Uploads are checked for extension, size, and magic bytes; filenames are sanitized.
- **Prompt injection**: tool output, web pages, documents, and KB passages are wrapped as untrusted data. The system prompt forbids following instructions inside them, and agents can only call tools in their own subset.
- **Code execution**: never on the host. `bwrap --unshare-all` gives no network, a read-only `/usr`, a tmpfs work dir, a cleared environment, the venv mounted read-only at `/venv`, and 1 GB/64-process/50 MB-file rlimits plus a wall-clock timeout. Docker (`--network none`) is used only if bwrap is missing.
- **Host actions**: `run_command` uses no shell, an allow-list plus blocked destructive arguments, and approval for every run; `read_log` is limited to `/var/log` and the app user's home. `run_shell` (PC Control) runs any bash command, but only after you approve the exact command, with a timeout and a hard block on catastrophic patterns. Desktop file tools are confined to your home folder and external drives.
- **Messaging**: WhatsApp is prefill-only, so you always press Send yourself.
- **Device keys**: the voice service uses a random device token, stored hashed and revocable in Settings. Your password is never stored.
- **SSRF**: `fetch_url` resolves every redirect hop and refuses non-public IPs.
- **Headers**: `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy`, request IDs on every response, and a WebSocket Origin check.

## Testing

```bash
cd backend && .venv/bin/python -m pytest -q      # 227 tests, about 1–2 min (fake desktop: nothing on your PC is touched, nothing shuts down)
cd voice && .venv/bin/python -m pytest -q        # 7 voice-logic tests (splitting, yes/no, approvals, questions)
```

The tests use your local MongoDB (database `terminator_test`, dropped on start) and a **scripted OpenAI-compatible
mock** ([backend/tests/mock_llm.py](backend/tests/mock_llm.py)). A message like `TOOL:<name> {json}` makes the
mock call that tool, so tool paths are tested deterministically. The sandbox, file generation, RAG, approvals, questions,
WebSocket, and reminder push all run for real. To click through the UI against the mock:

```bash
cd backend
.venv/bin/python -c "import uvicorn; from tests.mock_llm import app; uvicorn.run(app, port=11500)" &
MONGO_DB=terminator_e2e .venv/bin/uvicorn app.main:app --port 8000   # serves frontend/dist
# add provider: base URL http://127.0.0.1:11500/v1, model mock-1, embedding model mock-embed
```

## Deployment

```bash
npm --prefix frontend ci && npm --prefix frontend run build   # FastAPI serves frontend/dist
cp backend/.env.example backend/.env                           # set APP_ENV=production, JWT_SECRET, SECRET_KEY
pm2 start ecosystem.config.cjs && pm2 save                     # or: docker compose up -d --build
# desktop "Alexa" setup instead: systemd user services (run inside your graphical session)
cp deploy/systemd/terminator-api.service ~/.config/systemd/user/ && systemctl --user daemon-reload && systemctl --user enable --now terminator-api
voice/terminator-voice install-service --enable
```

Put nginx in front for TLS; [deploy/nginx.conf](deploy/nginx.conf) includes the WebSocket upgrade and disables
buffering for SSE.

**Docker:** the bubblewrap sandbox needs user namespaces inside the container, so `docker-compose.yml` relaxes
seccomp and AppArmor for the app container. If you'd rather not, remove those lines: everything else works, and
`run_python` reports "unavailable".

**Scaling:** run **one** process (the default). The rate limiter, stop flags, and WebSocket hub are in memory. Async
FastAPI handles many concurrent users in one process. For multiple instances, move those to Redis (pub/sub for the hub)
and use sticky sessions for `/api/ws`. Vector search is brute-force cosine similarity over MongoDB, which is fine up to
tens of thousands of chunks; beyond that, put Qdrant or Atlas Vector Search behind `rag/service.retrieve()`.

## Project layout

```
backend/app/
  main.py            app, middleware, lifespan (indexes + reminder scheduler), static frontend
  config.py db.py security.py files.py memory.py realtime.py orchestrator.py
  llm/               provider.py (streaming, retries, embeddings, tool probe) · registry.py
  agents/            registry.py (8 agents, prompts) · router.py
  tools/             base.py (registry, risk, execution) · web · data · personal · system · sandbox
  rag/               extract.py · service.py
  routers/           auth · chat · models · data · realtime
backend/app/         + desktop.py (session bridge) · intents.py (quick actions) · contacts.py
backend/app/tools/   + desktop.py (PC control, WhatsApp, run_shell)
backend/tests/       mock_llm.py · conftest.py · test_api.py · test_desktop.py
voice/               terminator_voice.py (wake word → STT → Terminator → TTS) · terminator-voice · tests/
deploy/              nginx.conf · systemd/terminator-api.service · ollama/llama3.1-8k.Modelfile
frontend/src/
  api.ts  lib/realtime.ts (WebSocket)  lib/voice.ts (STT/TTS)
  views/  Chat · Knowledge · Memory · Tasks · Settings · Activity · Auth
```
