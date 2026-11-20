"""FastAPI application entrypoint."""
import asyncio, logging, time, uuid
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from .config import get_settings
from .db import ensure_indexes, close, db
from .tools import web, data, personal, system, desktop as desktop_tools, session as session_tools  # noqa: F401
from .tools import sandbox
from .routers import auth, chat, models, data as data_routes, realtime as realtime_routes
from .realtime import reminder_loop
from . import orchestrator, environment

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    await ensure_indexes()
    log.info("started env=%s db=%s sandbox=%s", s.app_env, s.mongo_db, sandbox.backend())
    reminders = asyncio.create_task(reminder_loop(s.reminder_interval_s))
    warmer = asyncio.create_task(orchestrator.keep_warm_loop())
    autosave = asyncio.create_task(environment.autosave_loop(s.env_autosave_minutes * 60)) if s.env_autosave_minutes > 0 else None
    from .rag.service import reindex_keyword_only

    async def _reindex_later():
        await asyncio.sleep(5)
        try:
            await reindex_keyword_only()
        except Exception:
            log.exception("startup reindex failed")
    reindexer = asyncio.create_task(_reindex_later())
    yield
    reminders.cancel()
    warmer.cancel()
    if autosave:
        autosave.cancel()
    reindexer.cancel()
    await close()


app = FastAPI(title="Terminator AI Assistant", version="0.1.0", lifespan=lifespan,
              docs_url="/api/docs", openapi_url="/api/openapi.json")
s = get_settings()
app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in s.cors_origins.split(",") if o.strip()],
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def trace(request: Request, call_next):
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    t = time.perf_counter()
    try:
        resp = await call_next(request)
    except Exception:
        log.exception("unhandled rid=%s %s %s", rid, request.method, request.url.path)
        resp = JSONResponse({"detail": "Internal server error", "request_id": rid}, status_code=500)
    resp.headers["X-Request-ID"] = rid
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    if request.url.path.startswith("/api"):
        log.info("rid=%s %s %s %s %.0fms", rid, request.method, request.url.path, resp.status_code,
                 (time.perf_counter() - t) * 1000)
    return resp


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    errs = [{"field": ".".join(str(x) for x in e["loc"][1:]), "message": e["msg"]} for e in exc.errors()]
    return JSONResponse({"detail": "Invalid request", "errors": errs}, status_code=422)


@app.get("/api/health")
async def health():
    try:
        await db().command("ping")
        mongo = "ok"
    except Exception as e:
        mongo = f"error: {type(e).__name__}"
    return {"status": "ok" if mongo == "ok" else "degraded", "mongo": mongo, "sandbox": sandbox.backend(),
            "web_search": bool(s.searxng_url or s.tavily_api_key)}


for r in (auth.router, chat.router, models.router, data_routes.router, realtime_routes.router):
    app.include_router(r)

# Serve the built frontend in production (single process deploy)
DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        f = DIST / path
        return FileResponse(f if path and f.is_file() and DIST in f.resolve().parents else DIST / "index.html")
