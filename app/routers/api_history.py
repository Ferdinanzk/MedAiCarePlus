import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from app.config import MEDCARE_TIMEZONE
from app.dependencies import get_consented_user
from app.database import get_pool
from app.services import adherence

router = APIRouter(prefix="/api/history", tags=["history-api"])
_TZ = ZoneInfo(MEDCARE_TIMEZONE)
DEFAULT_SUMMARY_DAYS = 30


async def _get_u_id(user: dict) -> int | None:
    if "u_id" in user:
        return user["u_id"]
    pool = get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            'SELECT u_id FROM "user" WHERE supabase_id = $1 AND user_active = TRUE',
            user.get("sub"),
        )


def _local_midnight(day: datetime.date) -> datetime.datetime:
    return datetime.datetime.combine(day, datetime.time.min, tzinfo=_TZ)


def _range(start: Optional[datetime.date], end: Optional[datetime.date], today: datetime.date,
           default_days: int | None) -> tuple[datetime.datetime | None, datetime.datetime | None]:
    """[start 00:00, end+1 00:00) in local time; end defaults to today, start to default_days back (or open)."""
    end = end or today
    if start is None and default_days is not None:
        start = end - datetime.timedelta(days=default_days - 1)
    if start is not None and start > end:
        raise HTTPException(status_code=422, detail="start must not be after end")
    return (_local_midnight(start) if start else None), _local_midnight(end + datetime.timedelta(days=1))


@router.get("/intakes")
async def get_intake_history(
    user: dict = Depends(get_consented_user),
    start: Optional[datetime.date] = None,
    end: Optional[datetime.date] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Past doses only (scheduled up to now), newest first, paged; upcoming doses are /upcoming."""
    u_id = await _get_u_id(user)
    if not u_id:
        raise HTTPException(status_code=401, detail="User not found")
    now = datetime.datetime.now(_TZ)
    range_start, range_end = _range(start, end, now.date(), None)
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT i.intk_id AS id,
                   m.med_name AS medication_name,
                   m.dosage,
                   i.intake_time_stamp AS scheduled_time,
                   i.actual_intake_time AS taken_at,
                   i.intake_stats AS status,
                   i.detection_confidence,
                   i.detection_method,
                   COUNT(*) OVER () AS total
            FROM intake i
            JOIN medication m ON m.med_id = i.med_id
            WHERE m.u_id = $1
              AND i.intake_time_stamp <= $2
              AND ($3::timestamptz IS NULL OR i.intake_time_stamp >= $3)
              AND i.intake_time_stamp < $4
            ORDER BY i.intake_time_stamp DESC, i.intk_id DESC
            LIMIT $5 OFFSET $6
            """,
            u_id, now, range_start, range_end, limit, offset,
        )
    items = [{key: value for key, value in dict(r).items() if key != "total"} for r in rows]
    total = int(rows[0]["total"]) if rows else 0
    if not rows and offset:
        # Past the last page: report the real total so the client can step back.
        async with pool.acquire() as conn:
            total = await conn.fetchval(
                "SELECT COUNT(*) FROM intake i JOIN medication m ON m.med_id = i.med_id "
                "WHERE m.u_id = $1 AND i.intake_time_stamp <= $2 "
                "AND ($3::timestamptz IS NULL OR i.intake_time_stamp >= $3) AND i.intake_time_stamp < $4",
                u_id, now, range_start, range_end) or 0
    return {"items": items, "total": total, "limit": limit, "offset": offset,
            "has_more": offset + len(items) < total}


@router.get("/upcoming")
async def get_upcoming(user: dict = Depends(get_consented_user), days: int = Query(7, ge=1, le=31)):
    """Scheduled doses from now on, for active medications."""
    u_id = await _get_u_id(user)
    if not u_id:
        raise HTTPException(status_code=401, detail="User not found")
    now = datetime.datetime.now(_TZ)
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT i.intk_id AS id, m.med_id, m.med_name AS medication_name, m.dosage,
                   i.intake_time_stamp AS scheduled_time, i.intake_stats AS status
            FROM intake i
            JOIN medication m ON m.med_id = i.med_id
            WHERE m.u_id = $1 AND m.is_active = TRUE
              AND i.intake_time_stamp > $2 AND i.intake_time_stamp <= $3
            ORDER BY i.intake_time_stamp, m.med_name
            LIMIT 500
            """,
            u_id, now, now + datetime.timedelta(days=days),
        )
    return [dict(r) for r in rows]


@router.get("/summary")
async def get_summary(
    user: dict = Depends(get_consented_user),
    start: Optional[datetime.date] = None,
    end: Optional[datetime.date] = None,
):
    """Adherence over [start, end] (default: the last 30 days), per-day counts, and the current streak."""
    u_id = await _get_u_id(user)
    if not u_id:
        raise HTTPException(status_code=401, detail="User not found")
    now = datetime.datetime.now(_TZ)
    today = now.date()
    range_start, range_end = _range(start, end, today, DEFAULT_SUMMARY_DAYS)
    streak_start = _local_midnight(today - datetime.timedelta(days=adherence.STREAK_LOOKBACK_DAYS))
    async with get_pool().acquire() as conn:
        days = await adherence.daily_counts(conn, u_id, range_start, range_end, now)
        recent = await adherence.daily_counts(conn, u_id, streak_start,
                                              _local_midnight(today + datetime.timedelta(days=1)), now)
    return {
        "start": range_start.date().isoformat(),
        "end": (range_end - datetime.timedelta(days=1)).date().isoformat(),
        **adherence.totals(days),
        "streak_days": adherence.streak(recent, today),
        "days": [{**day, "day": day["day"].isoformat(),
                  "due": sum(day[s] for s in adherence.STATUSES)} for day in days],
    }


@router.get("/emotions")
async def get_emotion_history(user: dict = Depends(get_consented_user)):
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
