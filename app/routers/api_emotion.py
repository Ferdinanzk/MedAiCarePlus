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

    # Aggregate by emotion type
    emotion_scores = defaultdict(list)
    detected_count = 0
    for r in all_results:
        if r.get("detected") and r.get("emotion_type"):
            detected_count += 1
            emotion_scores[r["emotion_type"].capitalize()].append(r["emotion_score"])

    if detected_count == 0:
        return {"detected": False, "frame_count": len(frames), "all_results": all_results, "aggregated": {}}

    # Pick emotion with highest average confidence
    best_emotion = max(
        emotion_scores.keys(),
        key=lambda e: sum(emotion_scores[e]) / len(emotion_scores[e]))
    best_score = sum(emotion_scores[best_emotion]) / len(emotion_scores[best_emotion])

    return {
        "detected": True,
        "emotion_type": best_emotion,
        "emotion_score": round(best_score, 4),
        "frame_count": len(frames),
        "all_results": all_results,
        "aggregated": {
            e: {"count": len(s), "avg": round(sum(s)/len(s), 4), "max": round(max(s), 4)}
            for e, s in emotion_scores.items()
        },
    }
