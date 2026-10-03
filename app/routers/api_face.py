import asyncio
import logging
import re
import cv2
import numpy as np
import uuid
from pathlib import Path
from typing import List
from fastapi import APIRouter, UploadFile, File, Form, Depends
from fastapi.responses import JSONResponse
from itsdangerous import URLSafeTimedSerializer
from app.dependencies import get_consented_user
from app.config import SECRET_KEY
from app.database import get_pool
from app.services.face_recognition_service import FaceRecognitionService

router = APIRouter(prefix="/api/face", tags=["face-api"])
logger = logging.getLogger(__name__)

_face_signer = URLSafeTimedSerializer(SECRET_KEY)
_enroll_lock = asyncio.Lock()

FACE_ALREADY_REGISTERED = ("This face is already registered to another account. Sign in to that account "
                           "with face login (or ask its owner) instead of creating a new account.")
FACE_WITHOUT_ACCOUNT = ("This face is already in the face gallery, but no account uses those photos, so face "
                        "login cannot sign it in. Ask your caregiver or whoever runs this app to remove the "
                        "old face photos, then enroll again.")
# Legacy /auth/register-photos only: it has no login, so it may not write under a label someone has.
FACE_LABEL_TAKEN = ("This face ID is already in use. Choose another name, or sign in to that account and "
                    "enroll the face there.")


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
    user: dict = Depends(get_consented_user),
):
    """Save 3 enrollment photos to face gallery.
    face_label is derived from the authenticated user's DB row — never from the request.
    409 (nothing saved) when any photo's face is one login would take for another gallery label:
    face_already_registered when an active account has that label, face_registered_without_account when
    none does. Replacing your own photos is allowed (save_enrollment)."""
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
    saved = await save_enrollment(photos, label, caller=f"u_id {row['u_id']}")
    if isinstance(saved, JSONResponse):
        return saved

    # Persist the backend source of truth so onboarding never asks for a
    # re-enroll after localStorage is lost (logout / new device / cleared cache).
    async with pool.acquire() as conn:
        await conn.execute(
            'UPDATE "user" SET face_enrolled = TRUE WHERE u_id = $1',
            row["u_id"],
        )

    return {"saved": [str(path) for path in saved], "face_label": label}


async def save_enrollment(photos: List[UploadFile], label: str, *, caller: str,
                          new_label_only: bool = False) -> "list[Path] | JSONResponse":
    """Check 3 enrollment photos and store their face crops as <label>-<i>.jpg, then reload the gallery.

    Shared by POST /api/face/enroll and the legacy POST /auth/register-photos. Returns the saved paths, or
    a JSONResponse saying why nothing was saved. Every photo is checked against the gallery as it is before
    any of them is saved, leaving out label's own photos (these replace them); a face login would take for
    another gallery label is refused (_known_face_refusal). new_label_only is for the legacy route, which has
    no login: it also refuses a label that already has gallery photos or an account (409 face_label_taken),
    so that route can never replace anyone's photos. caller is for the log only."""
    from app.config import FACE_GALLERY_DIR

    svc = FaceRecognitionService.get_instance()
    if not FaceRecognitionService._available:
        return JSONResponse(
            {"error": "Face recognition model is unavailable"},
            status_code=503,
        )

    images = []
    for i, upload in enumerate(photos):
        data = await upload.read()
        nparr = np.frombuffer(data, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None:
            return JSONResponse({"error": f"Cannot decode photo {i}"}, status_code=400)
        images.append(img)

    loop = asyncio.get_running_loop()
    # One enrollment at a time, from the first check until the gallery holds the new photos, so two
    # accounts enrolling the same face at once cannot both pass the check below.
    async with _enroll_lock:
        if new_label_only and await _label_in_use(label, FACE_GALLERY_DIR):
            logger.warning("Face enrollment refused for %s: its face label already has gallery photos "
                           "or an account", caller)
            return JSONResponse({"error": "face_label_taken", "detail": FACE_LABEL_TAKEN}, status_code=409)
        face_imgs = []
        for i, img in enumerate(images):
            # Find the face this photo stores and which other label the gallery (as it is before these
            # photos are saved) would take it for, by login's own matching (match_enrollment_face). It holds
            # the service lock like identify_frame/identify_faces, and reuses the detection the crop below
            # needs anyway.
            found = await loop.run_in_executor(None, svc.match_enrollment_face, img, label)
            if found["error"]:
                return JSONResponse(
                    {"error": f"Face detection failed for photo {i}"},
                    status_code=503,
                )
            if found["box"] is None:
                return JSONResponse(
                    {"error": f"No face detected in photo {i}"},
                    status_code=400,
                )
            if found["label"]:
                return await _known_face_refusal(found["label"], found["distance"], i, caller)
            # Crop to the detected face before storing. The gallery descriptor is
            # computed over the whole stored image, while login detects + crops the
            # face first — so we must store a tight face crop here for the two
            # descriptors to live in the same space and actually match.
            x, y, w, h = found["box"]
            crop = img[max(0, y):y + h, max(0, x):x + w]
            face_imgs.append(crop if crop.size else img)

        FACE_GALLERY_DIR.mkdir(parents=True, exist_ok=True)
        staged: list[tuple[Path, Path]] = []
        final_paths = []
        for i, face_img in enumerate(face_imgs):
            temp_path = FACE_GALLERY_DIR / f".{label}-{uuid.uuid4().hex}-{i}.jpg"
            if not cv2.imwrite(str(temp_path), face_img):
                for old_temp, _ in staged:
                    old_temp.unlink(missing_ok=True)
                temp_path.unlink(missing_ok=True)
                return JSONResponse({"error": f"Cannot save photo {i}"}, status_code=500)
            final_path = FACE_GALLERY_DIR / f"{label}-{i}.jpg"
            staged.append((temp_path, final_path))
            final_paths.append(final_path)

        for temp_path, final_path in staged:
            temp_path.replace(final_path)

        await loop.run_in_executor(None, svc.reload_gallery)

    return final_paths


async def _known_face_refusal(other_label: str, distance: float, photo: int, caller: str) -> JSONResponse:
    """409 for a photo whose face login would take for other_label; never names that account.

    3 Oct 2026: one face enrolled under two accounts, and Reachy could no longer verify the patient. When
    other_label belongs to an active account (face login's own lookup), the person should sign in to it.
    When none does (photos left by the legacy route, or a deactivated account's), face login answers 404 for
    this face, so that advice would not work: the stale photos have to be removed first."""
    pool = get_pool()
    async with pool.acquire() as conn:
        owner = await conn.fetchrow(
            'SELECT u_id FROM "user" WHERE face_label = $1 AND user_active = TRUE',
            other_label.lower(),
        )
    if owner:
        logger.warning("Face enrollment refused for %s: photo %d matches another account's face "
                       "(distance %.3f)", caller, photo, distance)
        return JSONResponse(
            {"error": "face_already_registered", "detail": FACE_ALREADY_REGISTERED},
            status_code=409,
        )
    # No account behind this label, so naming it in the log identifies nobody; the operator needs it to
    # find the stale photos.
    logger.warning("Face enrollment refused for %s: photo %d matches gallery label %r, which no active "
                   "account uses (distance %.3f); remove its stale photos from the face gallery",
                   caller, photo, other_label, distance)
    return JSONResponse(
        {"error": "face_registered_without_account", "detail": FACE_WITHOUT_ACCOUNT},
        status_code=409,
    )


def _gallery_labels(gallery_dir: Path) -> set[str]:
    """The labels FacesDatabase reads from this folder: each .jpg/.png file's name without its extension
    and a trailing -<number>, in lower case."""
    if not gallery_dir.exists():
        return set()
    return {re.split(r"-\d+$", path.stem)[0].lower() for path in gallery_dir.iterdir()
            if path.name.split(".")[-1] in ("jpg", "png")}


async def _label_in_use(label: str, gallery_dir: Path) -> bool:
    """Whether label already has gallery photos or an account (active or not: face_label is UNIQUE)."""
    if label.lower() in _gallery_labels(gallery_dir):
        return True
    pool = get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow('SELECT 1 FROM "user" WHERE face_label = $1', label) is not None


@router.get("/enrollment-status")
async def enrollment_status(user: dict = Depends(get_consented_user)):
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
