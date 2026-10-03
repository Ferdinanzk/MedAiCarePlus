from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional

from app.dependencies import get_consented_user
from app.database import get_pool
from app.services.intake_detection import IntakeDetectionService
from app.services.intake_repository import transition_intake
from app.services.line_service import LineService

router = APIRouter(prefix="/api/intake", tags=["api-intake"])


class DetectPayload(BaseModel):
    session_id: str
    width: int = 640
    height: int = 480
    face_landmarks: list = []
    hand_landmarks: list = []
    timestamp: float = 0.0


class RecordPayload(BaseModel):
    intk_id: int
    detection_confidence: Optional[float] = None
    detection_method: Optional[str] = None


async def _get_u_id(user: dict) -> int | None:
    if "u_id" in user:
        return user["u_id"]
    pool = get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            'SELECT u_id FROM "user" WHERE supabase_id = $1 AND user_active = TRUE',
            user.get("sub"),
        )


@router.post("/detect")
async def detect_intake(payload: DetectPayload, user: dict = Depends(get_consented_user)):
    return JSONResponse({"detail": "Use the authenticated /api/intake/monitor session"}, status_code=410)


@router.post("/record")
async def record_intake(payload: RecordPayload, user: dict = Depends(get_consented_user)):
    u_id = await _get_u_id(user)
    if not u_id:
        return JSONResponse({"detail": "User not found"}, status_code=404)

    if payload.detection_method != "manual":
        return JSONResponse({"detail": "Automatic intake requires a monitored event"}, status_code=400)
    # A dose overdose protection refuses raises a schedule.DoseRefused in transition_intake: 409 with its reason
    # (dose_not_due_yet, dose_too_soon, daily_max_reached, dose_expired) and the patient's sentence (main.py).
    try:
        result = await transition_intake(u_id, payload.intk_id, "taken")
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=409)
    return {"success": True, **result}


@router.post("/skip")
async def skip_intake(payload: RecordPayload, user: dict = Depends(get_consented_user)):
    u_id = await _get_u_id(user)
    if not u_id:
        return JSONResponse({"detail": "User not found"}, status_code=404)

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT intk_id, med_id FROM intake WHERE intk_id = $1 AND u_id = $2",
            payload.intk_id, u_id,
        )
        if not row:
            return JSONResponse({"detail": "Intake record not found"}, status_code=404)

        try:
            await transition_intake(u_id, payload.intk_id, "skipped")
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=409)

        # Notify family contacts with notify_skipped = TRUE
        med_row = await conn.fetchrow(
            """
            SELECT m.med_name, u.name AS patient_name, u.line_id AS patient_line_id
            FROM medication m
            JOIN "user" u ON u.u_id = $1
            WHERE m.med_id = $2
            """,
            u_id, row["med_id"],
        )

        family = await conn.fetch(
            """
            SELECT line_id, name
            FROM family_contacts
            WHERE u_id = $1 AND notify_skipped = TRUE AND verified = TRUE
              AND relationship IS DISTINCT FROM 'user'
            """,
            u_id,
        )

        line_svc = LineService.get_instance()
        for contact in family:
            if contact["line_id"]:
                line_svc.send_text(
                    contact["line_id"],
                    (
                        f"⚠️ 用藥提醒\n"
                        f"{med_row['patient_name']} 主動跳過了用藥\n"
                        f"藥物: {med_row['med_name']}\n"
                        f"請關心確認狀況。"
                    ),
                )

    return {"success": True, "intk_id": payload.intk_id, "status": "skipped"}


@router.post("/end")
async def end_session(payload: dict, user: dict = Depends(get_consented_user)):
    u_id = await _get_u_id(user)
    if not u_id:
        return JSONResponse({"detail": "User not found"}, status_code=404)

    session_id = payload.get("session_id", "")
    svc = IntakeDetectionService.get_instance()
    await svc.end_session(u_id, session_id)
    return {"success": True}
