import asyncio
import cv2
import numpy as np
from fastapi import APIRouter, UploadFile, File, Depends
from fastapi.responses import JSONResponse
from app.dependencies import get_current_user
from app.services.emotion_service import EmotionService

router = APIRouter(prefix="/api/emotion", tags=["emotion-api"])


@router.post("/analyze")
async def analyze_emotion(
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
):
    """Upload a photo, return detected emotion and confidence scores."""
    image_bytes = await file.read()
    nparr = np.frombuffer(image_bytes, np.uint8)
    frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if frame is None:
        return JSONResponse({"detected": False, "error": "Invalid image"}, status_code=400)

    svc = EmotionService.get_instance()
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, svc.predict_frame, frame)
    return result
