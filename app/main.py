from app import ml_stubs; ml_stubs.install()  # dev: stubs for heavy ML packages

from contextlib import asynccontextmanager
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse

from app.database import init_pool, close_pool
from app.config import FRONTEND_URL
from app.services.face_recognition_service import FaceRecognitionService
from app.services.emotion_service import EmotionService
from app.services.ocr_service import OCRService
from app.services.line_service import LineService
from app.services.intake_detection import IntakeDetectionService
from app.routers import auth, emotion, ocr, medicines, notifications, display
from app.routers import api_face, api_ocr as api_ocr_router, api_emotion as api_emotion_router, api_notify, api_auth, api_family, api_medications, api_history, api_intake, api_analytics
from app.routers.api_notify import line_router
from app.jobs.scheduler import start_scheduler, stop_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_pool()
    FaceRecognitionService.get_instance()
    EmotionService.get_instance()
    OCRService.get_instance()
    LineService.get_instance()
    IntakeDetectionService.get_instance()
    start_scheduler()
    yield
    stop_scheduler()
    await close_pool()


app = FastAPI(title="MedAiCarePlus", version="1.0.0", lifespan=lifespan)

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
app.include_router(api_analytics.router,     tags=["api-analytics"])
app.include_router(line_router,              tags=["line-webhook"])


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
    }


# ── React SPA serving ────────────────────────────────────────────────────────
# Serve the built React app for all non-API routes.
WEB_DIR = "static/web"

@app.get("/")
async def root():
    return FileResponse(os.path.join(WEB_DIR, "index.html"))

@app.get("/{full_path:path}")
async def serve_spa(full_path: str):
    # Don't intercept API routes — they are handled by included routers above.
    # Serve actual files (assets, favicon, etc.) if they exist.
    file_path = os.path.join(WEB_DIR, full_path)
    if os.path.isfile(file_path):
        return FileResponse(file_path)
    # SPA fallback: return index.html so React Router handles the path.
    return FileResponse(os.path.join(WEB_DIR, "index.html"))
