import datetime
import json
import re
from typing import Optional
from pydantic import BaseModel
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from app.dependencies import get_current_user
from app.database import get_pool

router = APIRouter(prefix="/api/medications", tags=["medications-api"])


def _valid_hhmm(s) -> bool:
    """True if s is a valid 'HH:MM' clock time (00:00–23:59)."""
    if not isinstance(s, str):
        return False
    m = re.fullmatch(r"(\d{2}):(\d{2})", s.strip())
    if not m:
        return False
    h, mn = int(m.group(1)), int(m.group(2))
    return 0 <= h <= 23 and 0 <= mn <= 59


def _enabled_slots(schedule_time: dict | None, custom_times: dict | None = None) -> list[dict]:
    """Convert schedule_time booleans into slot objects with clock times.

    A per-slot custom "HH:MM" override (custom_times) takes precedence over the fixed
    _SLOT_TIMES default; a missing/invalid override falls back to the default.
    """
    if not schedule_time:
        return []
    custom_times = custom_times or {}
    slots = []
    for key, default_time in _SLOT_TIMES.items():
        if schedule_time.get(key):
            ct = custom_times.get(key)
            time_str = ct.strip() if _valid_hhmm(ct) else default_time
            slots.append({"key": key, "time": time_str, "label": _slot_label(key)})
    return slots


def _slot_label(key: str) -> str:
    labels = {
        "morning": "早上",
        "noon": "中午",
        "night": "晚上",
        "bedtime": "睡前",
    }
    return labels.get(key, key)


async def _generate_intake_schedule(
    conn, med_id: int, u_id: int, schedule_time: dict | None, use_before: str | None,
    custom_times: dict | None = None,
):
    """Generate intake rows for the next 30 days (or until use_before)."""
    slots = _enabled_slots(schedule_time, custom_times)
    if not slots:
        return

    days = 30
    if use_before:
        try:
            # Try parsing ROC date (e.g. 114年08月22日 → 2025-08-22)
            roc_match = __import__("re").search(r"(\d+)年(\d+)月(\d+)日", use_before)
            if roc_match:
                roc_year, month, day = map(int, roc_match.groups())
                gregorian = datetime.date(roc_year + 1911, month, day)
                days = min((gregorian - datetime.date.today()).days, 30)
                if days <= 0:
                    days = 1
        except Exception:
            pass

    base = datetime.datetime.combine(datetime.date.today(), datetime.time.min)
    values = []
    for offset in range(days):
        date = base + datetime.timedelta(days=offset)
        for slot in slots:
            hour, minute = map(int, slot["time"].split(":"))
            ts = date.replace(hour=hour, minute=minute)
            values.append((u_id, med_id, ts))

    # Bulk insert, ignoring conflicts
    await conn.executemany(
        """
        INSERT INTO intake (u_id, med_id, intake_time_stamp, intake_stats, notify_stats)
        VALUES ($1, $2, $3, 'pending', 'pending')
        ON CONFLICT DO NOTHING
        """,
        values,
    )


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


_SLOT_TIMES = {
    "morning": "08:00",
    "noon": "12:00",
    "night": "20:00",
    "bedtime": "22:00",
}


class MedicationPayload(BaseModel):
    name: str
    dosage: Optional[str] = None
    total_pills: int = 0
    pills_remaining: Optional[int] = None
    instructions: Optional[str] = None
    warning: Optional[str] = None
    pill_description: Optional[str] = None
    use_before: Optional[str] = None
    is_active: bool = True
    schedule_time: Optional[dict] = None
    custom_intake_times: Optional[dict] = None
    prescription_meta: Optional[dict] = None


@router.get("/today")
async def today_medications(user: dict = Depends(get_current_user), date: Optional[str] = None):
    u_id = await _get_u_id(user)
    if not u_id:
        return []
    target_date = datetime.date.today()
    if date:
        try:
            target_date = datetime.date.fromisoformat(date)
        except ValueError:
            pass
    today = target_date
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                i.intk_id        AS intake_id,
                m.med_id         AS med_id,
                m.med_name       AS name,
                m.dosage,
                m.pills_remaining,
                m.pill_description,
                m.warning,
                m.use_before,
                m.schedule_time,
                i.intake_time_stamp AS scheduled_time,
                i.intake_stats   AS status,
                i.intk_id        AS id
            FROM medication m
            JOIN intake i ON i.med_id = m.med_id
            WHERE m.u_id = $1
              AND m.is_active = TRUE
              AND i.intake_time_stamp::date = $2
            ORDER BY i.intake_time_stamp ASC
            """,
            u_id, today,
        )
    result = []
    for r in rows:
        row = dict(r)
        # use_before warning
        warning = None
        if row.get("use_before"):
            try:
                import re
                roc_match = re.search(r"(\d+)年(\d+)月(\d+)日", row["use_before"])
                if roc_match:
                    roc_year, month, day = map(int, roc_match.groups())
                    gregorian = datetime.date(roc_year + 1911, month, day)
                    days_left = (gregorian - today).days
                    if days_left < 0:
                        warning = "Expired"
                    elif days_left <= 7:
                        warning = f"Expires in {days_left} days"
            except Exception:
                pass
        row["use_before_warning"] = warning
        # slot label
        schedule_raw = row.get("schedule_time") or {}
        if isinstance(schedule_raw, str):
            try:
                schedule = json.loads(schedule_raw)
            except Exception:
                schedule = {}
        else:
            schedule = schedule_raw
        slot_label = ""
        ts = row.get("scheduled_time")
        if ts:
            for key, time_str in _SLOT_TIMES.items():
                if time_str in ts.strftime("%H:%M") and schedule.get(key):
                    slot_label = _slot_label(key)
                    break
        row["slot_label"] = slot_label
        result.append(row)
    return result


@router.patch("/intake/{intk_id}")
async def update_intake_status(
    intk_id: int,
    payload: dict,
    user: dict = Depends(get_current_user),
):
    """
    Update an intake record's status (e.g., mark as 'taken').
    Also decrements pills_remaining on the associated medication if marking as taken.
    """
    u_id = await _get_u_id(user)
    if not u_id:
        return JSONResponse({"detail": "User not found"}, status_code=404)

    new_status = payload.get("status")
    if new_status not in ("taken", "skipped", "missed", "pending"):
        return JSONResponse({"detail": "Invalid status"}, status_code=400)

    pool = get_pool()
    async with pool.acquire() as conn:
        # Verify ownership and get med_id
        row = await conn.fetchrow(
            "SELECT intk_id, med_id, intake_stats FROM intake WHERE intk_id = $1 AND u_id = $2",
            intk_id, u_id,
        )
        if not row:
            return JSONResponse({"detail": "Intake record not found"}, status_code=404)

        # Update intake status
        await conn.execute(
            "UPDATE intake SET intake_stats = $1, actual_intake_time = NOW() WHERE intk_id = $2",
            new_status, intk_id,
        )

        # If marking as taken, decrement pills_remaining
        if new_status == "taken":
            await conn.execute(
                "UPDATE medication SET pills_remaining = GREATEST(0, pills_remaining - 1) WHERE med_id = $1",
                row["med_id"],
            )

    return {"intk_id": intk_id, "status": new_status}


@router.get("")
async def list_medications(user: dict = Depends(get_current_user)):
    u_id = await _get_u_id(user)
    if not u_id:
        return []
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT med_id AS id, med_name AS name, dosage, pill_prescribed AS total_pills,
                   pills_remaining, instructions, warning, pill_description, use_before,
                   is_active, schedule_time, custom_intake_times, prescription_meta, created_at
            FROM medication
            WHERE u_id = $1
            ORDER BY is_active DESC, created_at DESC
            """,
            u_id,
        )
    return [dict(r) for r in rows]


@router.post("")
async def create_medication(
    payload: MedicationPayload,
    user: dict = Depends(get_current_user),
):
    u_id = await _get_u_id(user)
    if not u_id:
        return JSONResponse(
            {"detail": "Local user not found. Please link your account first."},
            status_code=404,
        )
    remaining = payload.pills_remaining if payload.pills_remaining is not None else payload.total_pills
    pool = get_pool()
    async with pool.acquire() as conn:
        try:
            med_id = await conn.fetchval(
                """
                INSERT INTO medication
                    (u_id, med_name, dosage, pill_prescribed, pills_remaining,
                     instructions, warning, pill_description, use_before,
                     is_active, schedule_time, custom_intake_times, prescription_meta)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                RETURNING med_id
                """,
                u_id,
                payload.name.strip(),
                payload.dosage,
                payload.total_pills,
                remaining,
                payload.instructions,
                payload.warning,
                payload.pill_description,
                payload.use_before,
                payload.is_active,
                json.dumps(payload.schedule_time) if payload.schedule_time else None,
                json.dumps(payload.custom_intake_times) if payload.custom_intake_times else None,
                json.dumps(payload.prescription_meta) if payload.prescription_meta else None,
            )
            # Auto-generate intake schedule rows
            await _generate_intake_schedule(
                conn, med_id, u_id, payload.schedule_time, payload.use_before,
                payload.custom_intake_times,
            )
        except Exception as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
    return {"id": med_id, "name": payload.name.strip()}


@router.patch("/{med_id}")
async def update_medication(
    med_id: int,
    payload: MedicationPayload,
    user: dict = Depends(get_current_user),
):
    u_id = await _get_u_id(user)
    if not u_id:
        return JSONResponse({"detail": "User not found"}, status_code=404)
    pool = get_pool()
    async with pool.acquire() as conn:
        updated = await conn.fetchval(
            """
            UPDATE medication
            SET med_name=$1, dosage=$2, pill_prescribed=$3, pills_remaining=$4,
                instructions=$5, warning=$6, pill_description=$7, use_before=$8,
                is_active=$9, schedule_time=$10, custom_intake_times=$11, prescription_meta=$12
            WHERE med_id=$13 AND u_id=$14
            RETURNING med_id
            """,
            payload.name.strip(),
            payload.dosage,
            payload.total_pills,
            payload.pills_remaining if payload.pills_remaining is not None else payload.total_pills,
            payload.instructions,
            payload.warning,
            payload.pill_description,
            payload.use_before,
            payload.is_active,
            json.dumps(payload.schedule_time) if payload.schedule_time else None,
            json.dumps(payload.custom_intake_times) if payload.custom_intake_times else None,
            json.dumps(payload.prescription_meta) if payload.prescription_meta else None,
            med_id,
            u_id,
        )
        if not updated:
            return JSONResponse({"detail": "Medication not found"}, status_code=404)

        # Re-time future doses so schedule/custom-time edits take effect. Only drop
        # not-yet-acted-on (pending) intakes; taken/missed/skipped history is preserved.
        await conn.execute(
            "DELETE FROM intake WHERE med_id=$1 AND u_id=$2 AND intake_stats='pending'",
            med_id, u_id,
        )
        await _generate_intake_schedule(
            conn, med_id, u_id, payload.schedule_time, payload.use_before,
            payload.custom_intake_times,
        )
    return {"id": updated}


@router.delete("/{med_id}")
async def delete_medication(med_id: int, user: dict = Depends(get_current_user)):
    u_id = await _get_u_id(user)
    if not u_id:
        return JSONResponse({"detail": "User not found"}, status_code=404)
    pool = get_pool()
    async with pool.acquire() as conn:
        deleted = await conn.fetchval(
            "DELETE FROM medication WHERE med_id=$1 AND u_id=$2 RETURNING med_id",
            med_id, u_id,
        )
    if not deleted:
        return JSONResponse({"detail": "Medication not found"}, status_code=404)
    return {"deleted": deleted}
