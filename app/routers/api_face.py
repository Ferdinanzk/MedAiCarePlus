import asyncio
import cv2
import numpy as np
from typing import List
from fastapi import APIRouter, UploadFile, File, Form, Depends
from fastapi.responses import JSONResponse
from itsdangerous import URLSafeTimedSerializer
from app.dependencies import get_current_user
from app.config import SECRET_KEY
from app.database import get_pool
from app.services.face_recognition_service import FaceRecognitionService

router = APIRouter(prefix="/api/face", tags=["face-api"])

_face_signer = URLSafeTimedSerializer(SECRET_KEY)


@router.post("/identify")
async def identify_face(
    file: UploadFile = File(...),
):
    """Upload a photo, return face recognition result as JSON.
    Public endpoint — no auth required, used during login flow."""
    image_bytes = await file.read()
    nparr = np.frombuffer(image_bytes, np.uint8)
    frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if frame is None:
        return JSONResponse({"identified": False, "error": "Invalid image"}, status_code=400)

    svc = FaceRecognitionService.get_instance()
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, svc.identify_frame, frame)
    return result


@router.post("/login")
async def face_login(file: UploadFile = File(...)):
    """Identify face and return a signed session token for API access."""
    image_bytes = await file.read()
    nparr = np.frombuffer(image_bytes, np.uint8)
    frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if frame is None:
        return JSONResponse({"identified": False, "error": "Invalid image"}, status_code=400)

    svc = FaceRecognitionService.get_instance()
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, svc.identify_frame, frame)

    if not result.get("identified"):
        return result

    face_label = result.get("name", "").lower()
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT u_id, name FROM "user" WHERE face_label = $1 AND user_active = TRUE',
            face_label,
        )

    if not row:
        return JSONResponse({"identified": False, "error": "User not found in database"}, status_code=404)

    token = _face_signer.dumps({"u_id": row["u_id"], "name": row["name"]})
    return {**result, "token": token, "u_id": row["u_id"]}


@router.post("/identify-bytes")
async def identify_face_bytes():
    """Accept raw JPEG bytes in request body for webcam frames."""
    return {"identified": False, "error": "Use /api/face/identify with multipart upload"}


@router.post("/check-pose")
async def check_pose(file: UploadFile = File(...)):
    """
    Public endpoint — detect face and determine head orientation.
    Used by onboarding face enrollment to guide front/left/right captures.
    Returns: {detected, direction: 'front'|'left'|'right'|'none', yaw, stub}
    """
    image_bytes = await file.read()
    nparr = np.frombuffer(image_bytes, np.uint8)
    frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if frame is None:
        return {"detected": False, "direction": "none", "stub": False}

    svc = FaceRecognitionService.get_instance()
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, svc.get_face_direction, frame)
    return result


@router.post("/enroll")
async def enroll_face(
    photos: List[UploadFile] = File(...),
    user: dict = Depends(get_current_user),
):
    """Save 3 enrollment photos to face gallery.
    face_label is derived from the authenticated user's DB row — never from the request."""
    from app.config import FACE_GALLERY_DIR

    if len(photos) != 3:
        return JSONResponse({"error": "Exactly 3 photos required"}, status_code=400)

    # Resolve the caller's face_label from the DB — never trust client-supplied label
    pool = get_pool()
    async with pool.acquire() as conn:
        if "u_id" in user:
            row = await conn.fetchrow(
                'SELECT u_id, face_label FROM "user" WHERE u_id=$1 AND user_active=TRUE',
                user["u_id"],
            )
        else:
            row = await conn.fetchrow(
                'SELECT u_id, face_label FROM "user" WHERE supabase_id=$1 AND user_active=TRUE',
                user.get("sub"),
            )

    if not row or not row["face_label"]:
        return JSONResponse(
            {"error": "User not found or missing face_label. Complete registration first."},
            status_code=404,
        )

    label = row["face_label"]

    svc = FaceRecognitionService.get_instance()

    FACE_GALLERY_DIR.mkdir(parents=True, exist_ok=True)
    saved = []
    for i, upload in enumerate(photos):
        data = await upload.read()
        nparr = np.frombuffer(data, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None:
            return JSONResponse({"error": f"Cannot decode photo {i}"}, status_code=400)

        # Crop to the detected face before storing. The gallery descriptor is
        # computed over the whole stored image, while login detects + crops the
        # face first — so we must store a tight face crop here for the two
        # descriptors to live in the same space and actually match.
        face_img = img
        try:
            rois = svc.face_det.infer((img,))
            if rois:
                roi = rois[0]
                x, y = int(roi.position[0]), int(roi.position[1])
                w, h = int(roi.size[0]), int(roi.size[1])
                crop = img[max(0, y):y + h, max(0, x):x + w]
                if crop.size:
                    face_img = crop
        except Exception:
            # If detection is unavailable, fall back to the full frame.
            pass

        path = FACE_GALLERY_DIR / f"{label}-{i}.jpg"
        cv2.imwrite(str(path), face_img)
        saved.append(str(path))

    svc.reload_gallery()

    # Persist the backend source of truth so onboarding never asks for a
    # re-enroll after localStorage is lost (logout / new device / cleared cache).
    async with pool.acquire() as conn:
        await conn.execute(
            'UPDATE "user" SET face_enrolled = TRUE WHERE u_id = $1',
            row["u_id"],
        )

    return {"saved": saved, "face_label": label}


@router.get("/enrollment-status")
async def enrollment_status(user: dict = Depends(get_current_user)):
    """Report whether the authenticated user already has an enrolled face.

    Used by the onboarding wizard to skip the 3-pose capture when the gallery
    already holds this user's photos. Checks the DB flag first, then falls back
    to the gallery files on disk (the recognizer's actual source of truth)."""
    from app.config import FACE_GALLERY_DIR

    pool = get_pool()
    async with pool.acquire() as conn:
        if "u_id" in user:
            row = await conn.fetchrow(
                'SELECT face_label, face_enrolled FROM "user" WHERE u_id=$1 AND user_active=TRUE',
                user["u_id"],
            )
        else:
            row = await conn.fetchrow(
                'SELECT face_label, face_enrolled FROM "user" WHERE supabase_id=$1 AND user_active=TRUE',
                user.get("sub"),
            )

    if not row or not row["face_label"]:
        return {"enrolled": False, "count": 0}

    label = row["face_label"]
    count = len(list(FACE_GALLERY_DIR.glob(f"{label}-*.jpg"))) if FACE_GALLERY_DIR.exists() else 0
    enrolled = bool(row["face_enrolled"]) or count > 0
    return {"enrolled": enrolled, "count": count}
