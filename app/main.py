from contextlib import asynccontextmanager
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

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
from app.routers.api_notify import line_router
from app.jobs.scheduler import start_scheduler, stop_scheduler
from app.database import get_pool
from app.services.legal_service import register_documents
from app.startup_checks import run_startup_checks, check_restore_state
from app.config import DEVICE_PORT
from app.services.outbox_dispatcher import start_dispatcher, stop_dispatcher


@asynccontextmanager
async def lifespan(app: FastAPI):
    run_startup_checks()
    await init_pool()
    async with get_pool().acquire() as conn:
        await check_restore_state(conn)
        await register_documents(conn)
    FaceRecognitionService.get_instance()
    EmotionService.get_instance()
    LineService.get_instance()
    IntakeDetectionService.get_instance()
    OCRService.get_instance()
    LandmarkService.get_instance()
    start_scheduler()
    await start_dispatcher()
    yield
    await stop_dispatcher()
    stop_scheduler()
    await close_pool()


app = FastAPI(title="MedAiCarePlus", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def device_port_isolation(request, call_next):
    """Device routes exist only on the private port; device tokens never work on the public one."""
    server = request.scope.get("server")
    on_device_port = bool(server) and server[1] == DEVICE_PORT
    path = request.url.path
    if path.startswith("/api/device/") != on_device_port:
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    if not on_device_port and request.headers.get("authorization", "").lower().startswith("bearer rdv1."):
        return JSONResponse({"detail": "Invalid or expired token"}, status_code=401)
    return await call_next(request)

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


# ── Health check (must be before catch-all) ──────────────────────────────────
@app.get("/health")
async def health():
    """Health check for deployments."""
    return {
        "status": "ok",
        "face_recognition": FaceRecognitionService._available,
        "emotion": EmotionService._available,
        "ocr": OCRService._available,
        "line": LineService._available,
        "intake_detection": IntakeDetectionService._available,
        "landmarks": LandmarkService._available,
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
