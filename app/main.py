from contextlib import asynccontextmanager
import logging
import os
from fastapi import FastAPI
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.datastructures import Headers

from app import config, startup_report
from app.database import init_pool, close_pool
from app.config import FRONTEND_URL
from app.services.face_recognition_service import FaceRecognitionService
from app.services.emotion_service import EmotionService
from app.services.ocr_service import OCRService
from app.services.line_service import LineService
from app.services.intake_detection import IntakeDetectionService
from app.services.landmark_service import LandmarkService
from app.routers import auth, emotion, ocr, medicines, notifications, display
from app.routers import api_face, api_ocr as api_ocr_router, api_emotion as api_emotion_router, api_notify, api_auth, api_family, api_medications, api_history, api_intake, api_monitor
from app.routers import api_legal, api_consent, api_account, api_reachy, api_device, api_conversations, api_memory
from app.routers import api_ai, api_dose_video
from app.routers.api_notify import line_router
from app.jobs.scheduler import start_scheduler, stop_scheduler
from app.database import get_pool
from app.services.legal_service import register_documents
from app.services import ai_settings, ai_store, reminders
from app.startup_checks import run_startup_checks, check_ai_settings, check_restore_state
from app.config import DEVICE_PORT
from app.services.outbox_dispatcher import start_dispatcher, stop_dispatcher
from app.services.schedule import DoseRefused

# Nothing else configures logging, and Python drops INFO by default: the server's own INFO lines (robot identity
# checks, the .env key fill) never reached `docker compose logs`. Give the root logger a handler and let "app.*" log INFO.
logging.basicConfig(format="%(levelname)s:%(name)s:%(message)s")
logging.getLogger("app").setLevel(logging.INFO)

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Each stage logs one [STARTUP] line with the real reason when it fails (app/startup_report.py); GET /health
    # carries the same report for scripts/medcare-boot.ps1, the host's boot script
    startup_report.begin()
    problems = run_startup_checks()
    startup_report.mark("environment", True, startup_report.environment(problems))
    try:
        attempt = await init_pool()   # retried: postgres may still be starting after a reboot
    except Exception as error:
        startup_report.mark("database", False, f"{type(error).__name__}: {error}")
        raise
    startup_report.mark("database", True, f"postgres answered on attempt {attempt}; sql/init.sql applied")
    async with get_pool().acquire() as conn:
        await check_restore_state(conn)
        await ai_store.seed_from_env(conn)   # first start only; checks .env (check_ai_settings) before copying it
        await ai_store.fill_openrouter_keys_from_env(config.OPENROUTER_API_KEY)   # every start: keyless ones only
        ai_settings.set_current(await ai_store.load(conn))
        unusable = []
        for job in ai_settings.JOBS:   # before check_ai_settings, which can stop a prod start
            if not ai_settings.job_usable(ai_settings.current(), job):
                unusable.append(job)
                log.warning("AI job %s has no usable model (a tested model whose connection has a key that can be "
                            "read), so it can't run; choose one on the Settings page's AI connections card", job)
        startup_report.mark("ai_settings", not unusable, startup_report.ai_jobs(ai_settings.current(), unusable),
                            warn=True)
        live_status, live_detail = startup_report.gpt_live(ai_settings.current())
        startup_report.mark("gpt_live", live_status == "ok", live_detail,
                            pending=live_status in ("no_check", "check_failed"), warn=True)
        check_ai_settings(ai_settings.current())
        await register_documents(conn)   # after the AI settings: the notices name the services in use
    FaceRecognitionService.get_instance()
    EmotionService.get_instance()
    OCRService.get_instance()
    LineService.get_instance()
    IntakeDetectionService.get_instance()
    LandmarkService.get_instance()
    models = {name: bool(getattr(service, "_available", False)) for name, service in (
        ("face_recognition", FaceRecognitionService), ("emotion", EmotionService), ("ocr", OCRService),
        ("intake_detection", IntakeDetectionService), ("landmarks", LandmarkService))}
    startup_report.mark("models", all(models.values()),
                        ", ".join(f"{name} {'ok' if ok else 'MISSING'}" for name, ok in models.items()), warn=True)
    start_scheduler()
    from app.jobs.scheduler import scheduler
    jobs = [job.id for job in scheduler.get_jobs()]
    startup_report.mark("scheduler", bool(jobs), f"{len(jobs)} jobs: {', '.join(jobs)}")
    await start_dispatcher()
    startup_report.mark("outbox", True, "dispatcher started (rows left in 'sending' are re-queued)")
    line_ready = bool(getattr(LineService, "_available", False))
    startup_report.mark("line", line_ready,
                        "channel token set" if line_ready else
                        "LINE_CHANNEL_ACCESS_TOKEN is empty: no LINE messages go out", warn=True)
    counts = await reminders.reload()   # pending reminders a previous process left: armed again (services/reminders.py)
    startup_report.mark("reminders", counts is not None,
                        (f"{counts['armed']} pending re-armed, {counts['expired']} expired (over "
                         f"{reminders.LATE_FIRE_SECONDS // 60} min late), {counts['undelivered']} given up"
                         if counts else "the start-up sweep failed (see the traceback above)"))
    startup_report.finish_in_background(ai_settings.current(), live_status=live_status)
    yield
    await stop_dispatcher()
    stop_scheduler()
    await close_pool()


app = FastAPI(title="MedAiCarePlus", version="1.0.0", lifespan=lifespan)


@app.exception_handler(DoseRefused)
async def dose_refused(request, exc: DoseRefused):
    """Every route (both ports) refuses a dose overdose protection stops the same way: 409 with the reason
    (dose_not_due_yet, dose_too_soon, daily_max_reached, dose_expired), its times and the patient's sentence. On the
    device port the sentence also comes as a `clip` in the live voice (services/speech_clips.py): its spoken form in
    Traditional, issued to the refused dose's patient, who is the device's own."""
    body = exc.body()
    if request.url.path.startswith("/api/device/") and exc.u_id is not None:
        from app.services import dose_safety, speech_clips

        clip = speech_clips.issue(exc.u_id, dose_safety.reply_text(exc, body["language"], spoken=True),
                                  body["language"])
        if clip is not None:
            body["clip"] = clip
    return JSONResponse(body, status_code=409)


@app.exception_handler(RequestValidationError)
async def invalid_request(request, exc: RequestValidationError):
    """FastAPI's 422 copies the request input back. On the AI card that input holds a plain API key, so the card
    answers a bare code. A device route answers without the input: a robot's NaN or Infinity cannot be written back
    as JSON, and the default answer then failed with a 500. Every other route keeps the default answer."""
    if request.url.path.startswith("/api/ai/"):
        return JSONResponse({"detail": "invalid_request"}, status_code=422)
    if request.url.path.startswith("/api/device/"):
        errors = [{key: value for key, value in error.items() if key != "input"} for error in exc.errors()]
        return JSONResponse({"detail": jsonable_encoder(errors)}, status_code=422)
    return await request_validation_exception_handler(request, exc)


class DevicePortIsolation:
    """Device routes exist only on the private port; device tokens never work on the public one.

    A plain ASGI middleware, so the rule covers WebSockets too: an @app.middleware("http") never sees them (spec
    2026-10-12-live-voice-2a-design.md A2, flaw F22). HTTP: 404 on the wrong port, then 401 for a device token on the
    public port, as before. A WebSocket on the wrong port, or with a device token on the public port, is closed with
    1008 before its handshake completes (uvicorn answers it with HTTP 403). scope["path"] is the path the router
    matches."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        server = scope.get("server")
        on_device_port = bool(server) and server[1] == DEVICE_PORT
        wrong_port = scope["path"].startswith("/api/device/") != on_device_port
        device_token = (not on_device_port
                        and Headers(scope=scope).get("authorization", "").lower().startswith("bearer rdv1."))
        if scope["type"] == "websocket" and (wrong_port or device_token):
            await send({"type": "websocket.close", "code": 1008})
            return
        if wrong_port:
            await JSONResponse({"detail": "Not Found"}, status_code=404)(scope, receive, send)
            return
        if device_token:
            await JSONResponse({"detail": "Invalid or expired token"}, status_code=401)(scope, receive, send)
            return
        await self.app(scope, receive, send)


app.add_middleware(DevicePortIsolation)   # where the http-only rule was, so CORS still runs outside it

# CORS for React frontend (local dev + Vercel production)
_cors_origins = [FRONTEND_URL, "http://localhost:5173", "http://localhost:3000"]
# Auto-detect Vercel production URLs
_vercel_url = os.getenv("VERCEL_URL")
if _vercel_url:
    _cors_origins.append(f"https://{_vercel_url}")
# Also allow any medaicareplus vercel subdomain
if "medaicareplus" in FRONTEND_URL:
    _cors_origins.append(FRONTEND_URL)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(set(_cors_origins)),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Legacy static assets (CSS, JS for Jinja2 templates)
app.mount("/static", StaticFiles(directory="static"), name="static")

# ── Legacy HTML routes (Jinja2 templates) ──────────────────────────────────────
app.include_router(auth.router,          prefix="/auth",          tags=["auth"])
app.include_router(emotion.router,       prefix="/emotion",       tags=["emotion"])
app.include_router(ocr.router,           prefix="/ocr",           tags=["ocr"])
app.include_router(medicines.router,     prefix="/medicines",     tags=["medicines"])
app.include_router(notifications.router, prefix="/notifications", tags=["notifications"])
app.include_router(display.router,       prefix="/display",       tags=["display"])

# ── New JSON API routes for React frontend ───────────────────────────────────
app.include_router(api_face.router,          tags=["api-face"])
app.include_router(api_ocr_router.router,    tags=["api-ocr"])
app.include_router(api_emotion_router.router, tags=["api-emotion"])
app.include_router(api_notify.router,       tags=["api-notify"])
app.include_router(api_auth.router,          tags=["api-auth"])
app.include_router(api_family.router,        tags=["api-family"])
app.include_router(api_medications.router,   tags=["api-medications"])
app.include_router(api_history.router,       tags=["api-history"])
app.include_router(api_intake.router,        tags=["api-intake"])
app.include_router(api_monitor.router,       tags=["intake-monitor"])
app.include_router(line_router,              tags=["line-webhook"])
app.include_router(api_legal.router,         tags=["legal"])
app.include_router(api_consent.router,       tags=["consent"])
app.include_router(api_account.router,       tags=["account"])
app.include_router(api_reachy.router,        tags=["reachy"])
app.include_router(api_device.router,        tags=["device"])
app.include_router(api_conversations.router, tags=["conversations"])
app.include_router(api_memory.router,        tags=["memory"])
app.include_router(api_dose_video.router,    tags=["dose-video"])
app.include_router(api_dose_video.media_router, tags=["dose-video"])
app.include_router(api_ai.router,            tags=["ai-connections"])


# ── Health check (must be before catch-all) ──────────────────────────────────
@app.get("/health")
async def health():
    """Health check for deployments."""
    await startup_report.refresh_line()
    return {
        "status": "ok",
        "face_recognition": FaceRecognitionService._available,
        "emotion": EmotionService._available,
        "ocr": OCRService._available,
        "line": LineService._available,
        "intake_detection": IntakeDetectionService._available,
        "landmarks": LandmarkService._available,
        "startup": startup_report.summary(),   # the [STARTUP] stages, for scripts/medcare-boot.ps1
    }


# ── React SPA serving ────────────────────────────────────────────────────────
# Serve the built React app for all non-API routes.
WEB_DIR = "static/web"

@app.get("/")
async def root():
    return FileResponse(os.path.join(WEB_DIR, "index.html"))

@app.get("/{full_path:path}")
async def serve_spa(full_path: str):
    # Unknown API endpoints must not look like a successful SPA response.
    if full_path == "api" or full_path.startswith("api/"):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    # Serve actual files (assets, favicon, etc.) if they exist.
    file_path = os.path.join(WEB_DIR, full_path)
    if os.path.isfile(file_path):
        return FileResponse(file_path)
    # SPA fallback: return index.html so React Router handles the path.
    return FileResponse(os.path.join(WEB_DIR, "index.html"))
