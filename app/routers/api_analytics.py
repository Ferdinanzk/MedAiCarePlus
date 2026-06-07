from fastapi import APIRouter, Depends, HTTPException, Query
from app.dependencies import get_current_user
from app.database import get_pool

router = APIRouter(prefix="/api/analytics", tags=["analytics-api"])


async def _get_u_id(user: dict) -> int | None:
    if "u_id" in user:
        return user["u_id"]
    pool = get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            'SELECT u_id FROM "user" WHERE supabase_id = $1 AND user_active = TRUE',
            user.get("sub"),
        )


@router.get("/emotion-trend")
async def emotion_trend(
    range: str = Query("week", pattern="^(week|month)$"),
    user: dict = Depends(get_current_user),
):
    """Per-day mood + medication adherence over the last week (7d) or month (30d).

    Mood is a valence scale averaged per day: Angry=1, Sad=2, Neutral=3, Happy=4.
    Returns one point per calendar day in the range (continuous timeline):
      - avg_mood: null when there is no emotion reading that day (line gap)
      - dominant_emotion: most frequent emotion that day
      - taken / total / adherence_pct: scheduled-intake adherence (total=0 if none)
    """
    u_id = await _get_u_id(user)
    if not u_id:
        raise HTTPException(status_code=401, detail="User not found")

    days = 7 if range == "week" else 30

    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            WITH days AS (
                SELECT generate_series(
                    (CURRENT_DATE - (($2::int - 1) * INTERVAL '1 day'))::date,
                    CURRENT_DATE,
                    INTERVAL '1 day'
                )::date AS day
            ),
            emo AS (
                SELECT time_stamp::date AS day,
                       AVG(CASE emotion_type
                             WHEN 'Happy' THEN 4 WHEN 'Neutral' THEN 3
                             WHEN 'Sad' THEN 2 WHEN 'Angry' THEN 1 END)::float AS avg_mood,
                       COUNT(*) AS mood_samples,
                       MODE() WITHIN GROUP (ORDER BY emotion_type) AS dominant_emotion
                FROM emotion
                WHERE u_id = $1
                  AND time_stamp >= (CURRENT_DATE - (($2::int - 1) * INTERVAL '1 day'))
                GROUP BY time_stamp::date
            ),
            intk AS (
                SELECT i.intake_time_stamp::date AS day,
                       COUNT(*) FILTER (WHERE i.intake_stats = 'taken') AS taken,
                       COUNT(*) AS total
                FROM intake i
                JOIN medication m ON m.med_id = i.med_id
                WHERE m.u_id = $1
                  AND i.intake_time_stamp >= (CURRENT_DATE - (($2::int - 1) * INTERVAL '1 day'))
                GROUP BY i.intake_time_stamp::date
            )
            SELECT d.day AS date,
                   e.avg_mood,
                   e.mood_samples,
                   e.dominant_emotion,
                   COALESCE(k.taken, 0) AS taken,
                   COALESCE(k.total, 0) AS total
            FROM days d
            LEFT JOIN emo e  ON e.day = d.day
            LEFT JOIN intk k ON k.day = d.day
            ORDER BY d.day
            """,
            u_id, days,
        )

    points = []
    for r in rows:
        total = r["total"] or 0
        taken = r["taken"] or 0
        points.append({
            "date": r["date"].isoformat(),
            "avg_mood": round(r["avg_mood"], 2) if r["avg_mood"] is not None else None,
            "mood_samples": r["mood_samples"] or 0,
            "dominant_emotion": r["dominant_emotion"],
            "taken": taken,
            "total": total,
            "adherence_pct": round(taken / total * 100) if total > 0 else None,
        })
    return {"range": range, "days": days, "points": points}
