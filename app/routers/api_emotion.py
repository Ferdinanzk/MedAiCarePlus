import asyncio
import cv2
import numpy as np
from typing import Optional
from collections import defaultdict
from fastapi import APIRouter, UploadFile, File, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from app.dependencies import get_current_user
from app.database import get_pool
from app.services.emotion_service import EmotionService

router = APIRouter(prefix="/api/emotion", tags=["emotion-api"])

_BATCH_MIN_CONFIDENCE = 0.30


def _confidence_tier(confidence: float | None) -> int:
    """Tiered point system from FaceEmotionDetector: 3/2/1 points, skip <30%."""
    if confidence is None:
        return 0
    if confidence >= 0.70:
        return 3
    if confidence >= 0.50:
        return 2
    if confidence >= _BATCH_MIN_CONFIDENCE:
        return 1
    return 0


def _aggregate_batch_results(all_results: list[dict]) -> dict:
    """Aggregate per-frame emotion predictions with confidence-tiered weighting."""
    emotion_scores = defaultdict(lambda: {
        "count": 0,
        "scored_count": 0,
        "skipped_low_confidence": 0,
        "points": 0,
        "weighted_sum": 0.0,
        "confidences": [],
    })
    detected_count = 0
    scored_count = 0

    for result in all_results:
        emotion_type = result.get("emotion_type")
        if not (result.get("detected") and emotion_type):
            continue

        detected_count += 1
        emotion = emotion_type.capitalize()
        try:
            confidence = float(result.get("emotion_score") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0

        points = _confidence_tier(confidence)
        bucket = emotion_scores[emotion]
        bucket["count"] += 1
        bucket["confidences"].append(confidence)

        if points == 0:
            bucket["skipped_low_confidence"] += 1
            continue

        bucket["scored_count"] += 1
        bucket["points"] += points
        bucket["weighted_sum"] += confidence * points
        scored_count += 1

    aggregated = {}
    for emotion, bucket in emotion_scores.items():
        confidences = bucket["confidences"]
        points = bucket["points"]
        weighted_avg = bucket["weighted_sum"] / points if points else 0.0
        aggregated[emotion] = {
            "count": bucket["count"],
            "scored_count": bucket["scored_count"],
            "skipped_low_confidence": bucket["skipped_low_confidence"],
            "points": points,
            "weighted_score": round(bucket["weighted_sum"], 4),
            "weighted_avg": round(weighted_avg, 4),
            "max": round(max(confidences), 4) if confidences else 0.0,
        }

    if detected_count == 0:
        return {"detected": False, "detected_count": 0, "scored_count": 0, "aggregated": aggregated}
    if scored_count == 0:
        return {"detected": False, "detected_count": detected_count, "scored_count": 0, "aggregated": aggregated}

    best_emotion = max(
        aggregated,
        key=lambda emotion: (
            aggregated[emotion]["weighted_score"],
            aggregated[emotion]["points"],
            aggregated[emotion]["max"],
            aggregated[emotion]["count"],
        ),
    )
    return {
        "detected": True,
        "emotion_type": best_emotion,
        "emotion_score": aggregated[best_emotion]["weighted_avg"],
        "detected_count": detected_count,
        "scored_count": scored_count,
        "aggregated": aggregated,
    }


class EmotionLogPayload(BaseModel):
    emotion_type: str
    emotion_score: float = 0.5
    note: str = ""
    context: Optional[str] = None


async def _get_u_id(user: dict) -> int | None:
    # Face-auth tokens carry u_id directly
    if "u_id" in user:
        return user["u_id"]
    # Supabase tokens need lookup by supabase_id
    pool = get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            'SELECT u_id FROM "user" WHERE supabase_id = $1 AND user_active = TRUE',
            user.get("sub"),
        )


@router.post("/log")
async def log_emotion(payload: EmotionLogPayload, user: dict = Depends(get_current_user)):
    u_id = await _get_u_id(user)
    if not u_id:
        return JSONResponse({"detail": "User not found"}, status_code=404)

    # FIX: Normalize emotion type to Title Case
    emotion_type = payload.emotion_type.capitalize() if payload.emotion_type else "Neutral"
    valid_emotions = {"Angry", "Happy", "Neutral", "Sad"}
    if emotion_type not in valid_emotions:
        return JSONResponse(
            {"detail": f"Invalid emotion_type '{emotion_type}'. Must be one of: {valid_emotions}"},
            status_code=400
        )

    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO emotion (u_id, emotion_type, emotion_score, note, context) VALUES ($1, $2, $3, $4, $5)",
            u_id, emotion_type, payload.emotion_score, payload.note, payload.context,
        )
    return {"success": True}


@router.get("/history")
async def emotion_history(user: dict = Depends(get_current_user)):
    u_id = await _get_u_id(user)
    if not u_id:
        return []
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT emot_id AS id, emotion_type, emotion_score, time_stamp AS recorded_at
            FROM emotion
            WHERE u_id = $1
            ORDER BY time_stamp DESC
            LIMIT 14
            """,
            u_id,
        )
    return [dict(r) for r in rows]


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

    # FIX: Defensively normalize emotion_type to Title Case
    if result.get("emotion_type"):
        result["emotion_type"] = result["emotion_type"].capitalize()
    return result


@router.post("/analyze-batch")
async def analyze_emotion_batch(
    frames: list[UploadFile] = File(...),
    context: str = "",
    user: dict = Depends(get_current_user),
):
    """Analyze multiple frames from a video stream, return aggregated best emotion."""
    svc = EmotionService.get_instance()
    if not EmotionService._available:
        return {"detected": False, "error": "Model not loaded", "frame_count": 0}

    # Reset rotation smoothing for each new video stream
    svc.reset_session()

    all_results = []
    for file in frames:
        image_bytes = await file.read()
        nparr = np.frombuffer(image_bytes, np.uint8)
        frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if frame is None:
            continue

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, svc.predict_frame, frame)
        all_results.append(result)

    batch_result = _aggregate_batch_results(all_results)
    batch_result["frame_count"] = len(frames)
    batch_result["all_results"] = all_results
    if not batch_result["detected"] and batch_result.get("scored_count") == 0:
        batch_result.setdefault("error", f"No frame reached {_BATCH_MIN_CONFIDENCE:.0%} confidence")
    return batch_result


@router.post("/analyze-debug")
async def analyze_emotion_debug(
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
):
    """Debug endpoint — returns per-frame debug info (rotation angles, crop coords, etc.)."""
    image_bytes = await file.read()
    nparr = np.frombuffer(image_bytes, np.uint8)
    frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if frame is None:
        return JSONResponse({"detected": False, "error": "Invalid image"}, status_code=400)

    svc = EmotionService.get_instance()
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, svc.predict_frame_debug, frame)

    if result.get("emotion_type"):
        result["emotion_type"] = result["emotion_type"].capitalize()
    return result
