from fastapi import APIRouter, Depends, HTTPException
from app.dependencies import get_current_user
from app.database import get_pool

router = APIRouter(prefix="/api/history", tags=["history-api"])


async def _get_u_id(user: dict) -> int | None:
    if "u_id" in user:
        return user["u_id"]
    pool = get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            'SELECT u_id FROM "user" WHERE supabase_id = $1 AND user_active = TRUE',
            user.get("sub"),
        )


@router.get("/intakes")
async def get_intake_history(user: dict = Depends(get_current_user)):
    u_id = await _get_u_id(user)
    if not u_id:
        raise HTTPException(status_code=401, detail="User not found")
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT i.intk_id AS id,
                   m.med_name AS medication_name,
                   m.dosage,
                   i.intake_time_stamp AS scheduled_time,
                   i.intake_stats AS status,
                   i.detection_confidence,
                   i.detection_method
            FROM intake i
            JOIN medication m ON m.med_id = i.med_id
            WHERE m.u_id = $1
            ORDER BY i.intake_time_stamp DESC
            LIMIT 100
            """,
            u_id,
        )
    return [dict(r) for r in rows]


@router.get("/emotions")
async def get_emotion_history(user: dict = Depends(get_current_user)):
    u_id = await _get_u_id(user)
    if not u_id:
        raise HTTPException(status_code=401, detail="User not found")
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT emot_id AS id,
                   emotion_type,
                   emotion_score,
                   time_stamp AS recorded_at
            FROM emotion
            WHERE u_id = $1
            ORDER BY time_stamp DESC
            LIMIT 50
            """,
            u_id,
        )
    return [dict(r) for r in rows]
