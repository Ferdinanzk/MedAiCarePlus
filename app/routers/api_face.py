import asyncio
import cv2
import numpy as np
from typing import List
from fastapi import APIRouter, UploadFile, File, Depends
from fastapi.responses import JSONResponse
from app.dependencies import get_current_user
from app.services.face_recognition_service import FaceRecognitionService

router = APIRouter(prefix="/api/face", tags=["face-api"])


@router.post("/identify")
async def identify_face(
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
):
    """Upload a photo, return face recognition result as JSON."""
    image_bytes = await file.read()
    nparr = np.frombuffer(image_bytes, np.uint8)
    frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if frame is None:
        return JSONResponse({"identified": False, "error": "Invalid image"}, status_code=400)

    svc = FaceRecognitionService.get_instance()
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, svc.identify_frame, frame)
    return result


@router.post("/identify-bytes")
async def identify_face_bytes(
    user: dict = Depends(get_current_user),
):
    """Accept raw JPEG bytes in request body for webcam frames."""
    from fastapi import Request
    # FastAPI doesn't support raw bytes in POST without special handling
    # This endpoint is a placeholder; the actual implementation uses the file upload endpoint
    return {"identified": False, "error": "Use /api/face/identify with multipart upload"}


@router.post("/enroll")
async def enroll_face(
    face_label: str,
    photos: List[UploadFile] = File(...),
    user: dict = Depends(get_current_user),
):
    """Save 3 enrollment photos to face gallery."""
    from app.config import FACE_GALLERY_DIR

    label = face_label.strip().lower()
    if not label or "/" in label or "\\" in label:
        return JSONResponse({"error": "Invalid face_label"}, status_code=400)
    if len(photos) != 3:
        return JSONResponse({"error": "Exactly 3 photos required"}, status_code=400)

    FACE_GALLERY_DIR.mkdir(parents=True, exist_ok=True)
    saved = []
    for i, upload in enumerate(photos):
        data = await upload.read()
        nparr = np.frombuffer(data, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None:
            return JSONResponse({"error": f"Cannot decode photo {i}"}, status_code=400)
        path = FACE_GALLERY_DIR / f"{label}-{i}.jpg"
        cv2.imwrite(str(path), img)
        saved.append(str(path))

    return {"saved": saved, "face_label": label}
