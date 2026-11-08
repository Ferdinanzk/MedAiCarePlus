import asyncio
import logging
import cv2
import numpy as np
from typing import List
from fastapi import APIRouter, UploadFile, File, Form, Depends
from fastapi.responses import JSONResponse
from itsdangerous import URLSafeTimedSerializer
from app.dependencies import get_consented_user
from app.config import SECRET_KEY, FACE_LOGIN_FRAMES
from app.database import get_pool
from app.services import face_vectors
from app.services.face_login import LoginDecision
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
# The stored face vectors could not be read before the duplicate check: nothing is stored.
FACE_CHECK_UNAVAILABLE = "Face check unavailable. Please try again later."


MAX_FRAME_BYTES = 2 * 1024 * 1024


class FrameUploadError(Exception):
    """Why a face login upload was refused: the HTTP status and the answer's error text."""

    def __init__(self, status: int, error: str):
        super().__init__(error)
        self.status = status
        self.error = error


async def read_login_frames(files: List[UploadFile]) -> list[np.ndarray | None]:
    """Decode 1 to FACE_LOGIN_FRAMES uploads. An upload that is not an image becomes None, which login judges
    as a frame without a face. Raises FrameUploadError: 422 too_many_frames, 413 for a file over 2 MB,
    400 Invalid image when no upload is an image."""
    if len(files) > FACE_LOGIN_FRAMES:
        raise FrameUploadError(422, "too_many_frames")
    frames: list[np.ndarray | None] = []
    for upload in files:
        data = await upload.read(MAX_FRAME_BYTES + 1)
        if len(data) > MAX_FRAME_BYTES:
            raise FrameUploadError(413, "frame_too_large")
        frame = None
        if data:
            try:
                frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
            except cv2.error:
                frame = None
        frames.append(frame)
    if all(frame is None for frame in frames):
        raise FrameUploadError(400, "Invalid image")
    return frames


async def run_login(frames: list[np.ndarray | None]) -> LoginDecision:
    svc = FaceRecognitionService.get_instance()
    return await asyncio.get_running_loop().run_in_executor(None, svc.login, frames)


def login_answer(decision: LoginDecision) -> dict:
    """The face answer's keys: today's (identified, name, distance, face_count, error) plus reason and frames."""
    return {
        "identified": decision.identified,
        "name": decision.label,
        "distance": decision.distance,
        "face_count": decision.face_count,
        "error": None if FaceRecognitionService._available else "Model not loaded",
        "reason": decision.reason,
        "frames": decision.frames,
    }


def log_login(u_id: int | None, reason: str) -> None:
    """The route's one line per login: the account id (or -), never the name."""
    logger.info("face login: account=%s reason=%s", u_id if u_id is not None else "-", reason)


@router.post("/identify")
async def identify_face(files: List[UploadFile] = File(...)):
    """Up to FACE_LOGIN_FRAMES photos, return the face login decision as JSON.
    Public endpoint — no auth required, used during login flow."""
    try:
        frames = await read_login_frames(files)
    except FrameUploadError as exc:
        return JSONResponse({"identified": False, "error": exc.error}, status_code=exc.status)
    return login_answer(await run_login(frames))


@router.post("/login")
async def face_login(files: List[UploadFile] = File(...)):
    """Decide the face login from up to FACE_LOGIN_FRAMES photos; when identified, return a signed session
    token for API access."""
    try:
        frames = await read_login_frames(files)
    except FrameUploadError as exc:
        return JSONResponse({"identified": False, "error": exc.error}, status_code=exc.status)
    decision = await run_login(frames)
    result = login_answer(decision)

    if not decision.identified:
        log_login(None, decision.reason)
        return result

    face_label = decision.label.lower()
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT u_id, name FROM "user" WHERE face_label = $1 AND user_active = TRUE',
            face_label,
        )

    if not row:
        log_login(None, decision.reason)
        return JSONResponse({"identified": False, "error": "User not found in database",
                             "reason": decision.reason, "frames": decision.frames}, status_code=404)

    log_login(row["u_id"], decision.reason)
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
    """Store the face vectors of 3 enrollment photos (face_vector rows; the photos are not kept).
    face_label is derived from the authenticated user's DB row — never from the request.
    409 (nothing saved) when any photo's face is one login would take for another gallery label:
    face_already_registered when an active account has that label, face_registered_without_account when
    none does. Replacing your own vectors is allowed (save_enrollment). The answer's saved holds the 3 new
    rows' ids as text: never a path or a vector."""
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
    saved = await save_enrollment(photos, label, row["u_id"], caller=f"u_id {row['u_id']}")
    if isinstance(saved, JSONResponse):
        return saved

    # Persist the backend source of truth so onboarding never asks for a
    # re-enroll after localStorage is lost (logout / new device / cleared cache).
    async with pool.acquire() as conn:
        await conn.execute(
            'UPDATE "user" SET face_enrolled = TRUE WHERE u_id = $1',
            row["u_id"],
        )

    return {"saved": [str(vector_id) for vector_id in saved], "face_label": label}


# One refusal sentence per reason the main-face rule gives (as face login's): the photo must hold one clear face.
_PHOTO_REFUSALS = {
    "no_face": "No face detected in photo {i}",
    "two_faces": "Two faces in photo {i}",
    "come_closer": "Face too small in photo {i}",
}


async def save_enrollment(photos: List[UploadFile], label: str, u_id: int, *,
                          caller: str) -> "list[int] | JSONResponse":
    """Check 3 enrollment photos and store each one's face vector as a face_vector row of u_id, replacing
    u_id's earlier rows, then reload face login's gallery from the rows.

    Returns the 3 new vector_ids, or a JSONResponse saying why nothing was stored. Every photo is checked
    against the gallery as it is before anything is stored, leaving out label's own vectors (these replace
    them); a face login would take for another gallery label is refused (_known_face_refusal). The vector is
    the main face's descriptor computed on the full photo, as face login computes it (match_enrollment_face).
    No photo is written anywhere. Vectors are never logged. caller is for the log only."""
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
    pool = get_pool()
    # One enrollment at a time, from the first check until the gallery holds the new vectors, so two
    # accounts enrolling the same face at once cannot both pass the check below.
    async with _enroll_lock:
        # Check against the vectors as stored, not only this process's gallery: rows another process stored, or
        # rows whose reload after their commit failed, must refuse the same face too. Without them the check
        # cannot be trusted, so a failed read stores nothing (fail closed).
        try:
            await face_vectors.reload(pool)
        except Exception as exc:
            logger.error("Face enrollment refused for %s: the stored face vectors could not be read (%s)",
                         caller, type(exc).__name__)
            return JSONResponse({"error": FACE_CHECK_UNAVAILABLE}, status_code=503)
        vectors = []
        for i, img in enumerate(images):
            # Find the face this photo stores, its descriptor, and which other label the gallery (as it is
            # before these vectors are stored) would take it for, by login's own matching
            # (match_enrollment_face, under the service lock like identify_faces).
            found = await loop.run_in_executor(None, svc.match_enrollment_face, img, label)
            if found["error"]:
                return JSONResponse(
                    {"error": f"Face detection failed for photo {i}"},
                    status_code=503,
                )
            if found["reason"] != "ok" or found["box"] is None:
                return JSONResponse(
                    {"error": _PHOTO_REFUSALS.get(found["reason"], "No face detected in photo {i}").format(i=i)},
                    status_code=400,
                )
            if found["label"]:
                return await _known_face_refusal(found["label"], found["distance"], i, caller)
            try:
                vectors.append(face_vectors.to_bytes(found["descriptor"]))
            except (TypeError, ValueError):
                # Fail closed: a face without a usable descriptor is never stored.
                return JSONResponse(
                    {"error": f"Face detection failed for photo {i}"},
                    status_code=503,
                )

        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM face_vector WHERE u_id = $1", u_id)
                vector_ids = [
                    await conn.fetchval(
                        "INSERT INTO face_vector (u_id, face_label, vector, source) "
                        "VALUES ($1, $2, $3, 'enroll') RETURNING vector_id",
                        u_id, label.lower(), vector,
                    )
                    for vector in vectors
                ]
        # Still under _enroll_lock: the next registration's check must already see these vectors.
        await face_vectors.reload(pool)

    return vector_ids


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


@router.get("/enrollment-status")
async def enrollment_status(user: dict = Depends(get_consented_user)):
    """Report whether the authenticated user already has an enrolled face.

    Used by the onboarding wizard to skip the 3-pose capture when the account already has its face vectors.
    count is the account's face_vector rows (face login's source of truth); face_enrolled also counts."""
    pool = get_pool()
    async with pool.acquire() as conn:
        if "u_id" in user:
            row = await conn.fetchrow(
                'SELECT u_id, face_label, face_enrolled FROM "user" WHERE u_id=$1 AND user_active=TRUE',
                user["u_id"],
            )
        else:
            row = await conn.fetchrow(
                'SELECT u_id, face_label, face_enrolled FROM "user" WHERE supabase_id=$1 AND user_active=TRUE',
                user.get("sub"),
            )
        if not row or not row["face_label"]:
            return {"enrolled": False, "count": 0}
        count = await conn.fetchval("SELECT count(*) FROM face_vector WHERE u_id = $1", row["u_id"])

    enrolled = bool(row["face_enrolled"]) or count > 0
    return {"enrolled": enrolled, "count": count}
