from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse

from app.database import init_pool, close_pool
from app.services.face_recognition_service import FaceRecognitionService
from app.services.emotion_service import EmotionService
from app.services.ocr_service import OCRService
from app.routers import auth, emotion, ocr, medicines, notifications, display


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_pool()
    FaceRecognitionService.get_instance()
    EmotionService.get_instance()
    OCRService.get_instance()
    yield
    await close_pool()


app = FastAPI(title="MedAiCarePlus", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(auth.router,          prefix="/auth",          tags=["auth"])
app.include_router(emotion.router,       prefix="/emotion",       tags=["emotion"])
app.include_router(ocr.router,           prefix="/ocr",           tags=["ocr"])
app.include_router(medicines.router,     prefix="/medicines",     tags=["medicines"])
app.include_router(notifications.router, prefix="/notifications", tags=["notifications"])
app.include_router(display.router,       prefix="/display",       tags=["display"])


@app.get("/")
async def root():
    return RedirectResponse(url="/auth/login")
