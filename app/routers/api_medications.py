import datetime
import json
from decimal import Decimal
from typing import Optional
from zoneinfo import ZoneInfo
from pydantic import BaseModel, field_validator
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from app.dependencies import get_consented_user
from app.database import get_pool
from app.config import MEDCARE_TIMEZONE

router = APIRouter(prefix="/api/medications", tags=["medications-api"])
_MEDCARE_TZ = ZoneInfo(MEDCARE_TIMEZONE)


def _enabled_slots(schedule_time: dict | None) -> list[dict]:
    """Convert schedule_time booleans into slot objects with clock times."""
    if not schedule_time:
        return []
    slots = []
    for key, time_str in _SLOT_TIMES.items():
        if schedule_time.get(key):
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


def _normalized_schedule(schedule_time: dict | str | None) -> tuple[tuple[str, bool], ...]:
    """Return the schedule fields that actually create intake rows.

    The UI also sends display-only keys such as ``before_meals``.  Ignoring
    those keys prevents a no-op edit from rebuilding the medication's future
    intake rows.
    """
    if isinstance(schedule_time, str):
        try:
            schedule_time = json.loads(schedule_time)
        except (TypeError, ValueError):
            schedule_time = None
    schedule_time = schedule_time or {}
    return tuple((key, bool(schedule_time.get(key))) for key in _SLOT_TIMES)


async def _generate_intake_schedule(
    conn,
    med_id: int,
    u_id: int,
    schedule_time: dict | None,
    use_before: str | None,
    start_at: datetime.datetime | None = None,
):
    """Generate future intake rows for the next 30 days (or until use_before)."""
    slots = _enabled_slots(schedule_time)
    if not slots:
        return

    now = start_at or datetime.datetime.now(_MEDCARE_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_MEDCARE_TZ)

    days = 30
    if use_before:
        try:
            # Try parsing ROC date (e.g. 114年08月22日 → 2025-08-22)
            roc_match = __import__("re").search(r"(\d+)年(\d+)月(\d+)日", use_before)
            if roc_match:
                roc_year, month, day = map(int, roc_match.groups())
                gregorian = datetime.date(roc_year + 1911, month, day)
                days = min((gregorian - now.date()).days, 30)
                if days <= 0:
                    days = 1
        except Exception:
            pass

    base = datetime.datetime.combine(now.date(), datetime.time.min, tzinfo=_MEDCARE_TZ)
    values = []
    for offset in range(days):
        date = base + datetime.timedelta(days=offset)
        for slot in slots:
            hour, minute = map(int, slot["time"].split(":"))
            ts = date.replace(hour=hour, minute=minute)
            if ts > now:
                values.append((u_id, med_id, ts))

    # Bulk insert, ignoring conflicts
    if values:
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

DOSE_FORMS = ("solid_oral", "liquid", "inhaler", "injection", "topical", "other")


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
    prescription_meta: Optional[dict] = None
    # None = keep the stored value on update, schema default on create.
    dose_form: Optional[str] = None
    units_per_dose: Optional[Decimal] = None

    @field_validator("dose_form")
    @classmethod
    def _check_dose_form(cls, value):
        if value is not None and value not in DOSE_FORMS:
            raise ValueError(f"dose_form must be one of {', '.join(DOSE_FORMS)}")
        return value

    @field_validator("units_per_dose")
    @classmethod
    def _check_units(cls, value):
        # NUMERIC(4,2): positive, below 100, at most two decimals.
        if value is not None and (not value.is_finite() or value <= 0 or value >= 100
                                  or value != value.quantize(Decimal("0.01"))):
            raise ValueError("units_per_dose must be > 0 and < 100 with at most two decimals")
        return value


def _units_json(value) -> float | None:
    return float(value) if value is not None else None


@router.get("/today")
async def today_medications(user: dict = Depends(get_consented_user), date: Optional[str] = None):
    u_id = await _get_u_id(user)
    if not u_id:
        return []
    target_date = datetime.datetime.now(_MEDCARE_TZ).date()
    if date:
        try:
            target_date = datetime.date.fromisoformat(date)
        except ValueError:
            pass
    today = target_date
    local_start = datetime.datetime.combine(today, datetime.time.min, tzinfo=_MEDCARE_TZ)
    local_end = local_start + datetime.timedelta(days=1)
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
              AND i.intake_time_stamp >= $2
              AND i.intake_time_stamp < $3
            ORDER BY i.intake_time_stamp ASC
            """,
            u_id, local_start, local_end,
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


@router.post("/{med_id}/intake-now")
async def intake_now(med_id: int, user: dict = Depends(get_consented_user)):
    """Resolve a medication card's Take Now action to a pending intake row.

    Scheduled rows whose time has arrived (including missed rows) are reused.
    If the next scheduled slot is still in the future, or the medication is
    unscheduled, create one ad-hoc row at the current time.  The medication
    row lock makes repeated/concurrent clicks return the same pending row
    instead of creating duplicate doses.  Nothing is marked taken here;
    monitor/commit_monitored owns that transition after camera evidence.
    """
    u_id = await _get_u_id(user)
    if not u_id:
        return JSONResponse({"detail": "User not found"}, status_code=404)

    now = datetime.datetime.now(_MEDCARE_TZ)
    local_start = datetime.datetime.combine(now.date(), datetime.time.min, tzinfo=_MEDCARE_TZ)
    recent_cutoff = now - datetime.timedelta(minutes=5)
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            medication = await conn.fetchrow(
                """
                SELECT med_id, med_name, dosage, pills_remaining, warning,
                       use_before, is_active
                FROM medication
                WHERE med_id=$1 AND u_id=$2
                FOR UPDATE
                """,
                med_id,
                u_id,
            )
            if not medication:
                return JSONResponse({"detail": "Medication not found"}, status_code=404)
            if not medication["is_active"]:
                return JSONResponse({"detail": "Medication is inactive"}, status_code=409)
            if (medication["pills_remaining"] or 0) <= 0:
                return JSONResponse({"detail": "No pills remain for this medication"}, status_code=409)

            # A due row is the medication's existing scheduled dose. Future
            # rows are intentionally not selected: Take Now means the dose
            # starts now even when the next scheduled slot is later today.
            row = await conn.fetchrow(
                """
                SELECT i.intk_id AS id, i.med_id, m.med_name AS name, m.dosage,
                       i.intake_time_stamp AS scheduled_time, i.intake_stats AS status,
                       m.pills_remaining, m.warning
                FROM intake i
                JOIN medication m ON m.med_id=i.med_id
                WHERE i.intk_id IN (
                    SELECT intk_id
                    FROM intake
                    WHERE med_id=$1 AND u_id=$2
                      AND intake_stats IN ('pending', 'missed')
                      AND intake_time_stamp >= $3
                      AND intake_time_stamp <= $4
                    ORDER BY intake_time_stamp DESC
                    LIMIT 1
                )
                FOR UPDATE
                """,
                med_id,
                u_id,
                local_start,
                now,
            )
            if not row:
                # Prevent an immediate second click after a monitored dose
                # has committed from manufacturing another dose. A later
                # intentional dose still has its scheduled pending row (or
                # can be started after this short duplicate window).
                recent_taken = await conn.fetchval(
                    """
                    SELECT 1 FROM intake
                    WHERE med_id=$1 AND u_id=$2 AND intake_stats='taken'
                      AND actual_intake_time >= $3
                    LIMIT 1
                    """,
                    med_id,
                    u_id,
                    recent_cutoff,
                )
                if recent_taken:
                    return JSONResponse({"detail": "This medication was just recorded"}, status_code=409)
                row = await conn.fetchrow(
                    """
                    INSERT INTO intake (u_id, med_id, intake_time_stamp, intake_stats, notify_stats)
                    VALUES ($1, $2, $3, 'pending', 'pending')
                    RETURNING intk_id AS id, med_id, intake_time_stamp AS scheduled_time,
                              intake_stats AS status
                    """,
                    u_id,
                    med_id,
                    now,
                )
                row = dict(row)
                row.update({
                    "name": medication["med_name"],
                    "dosage": medication["dosage"],
                    "pills_remaining": medication["pills_remaining"],
                    "warning": medication["warning"],
                })
            else:
                row = dict(row)
            return row


@router.patch("/intake/{intk_id}")
async def update_intake_status(
    intk_id: int,
    payload: dict,
    user: dict = Depends(get_consented_user),
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

    # transition_intake refuses doses awaiting caregiver confirmation under the row
    # lock (409 "awaiting_caregiver_confirmation").
    from app.services.intake_repository import transition_intake
    try:
        return await transition_intake(u_id, intk_id, new_status)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=409)


@router.get("")
async def list_medications(user: dict = Depends(get_consented_user)):
    u_id = await _get_u_id(user)
    if not u_id:
        return []
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT med_id AS id, med_name AS name, dosage, pill_prescribed AS total_pills,
                   pills_remaining, instructions, warning, pill_description, use_before,
                   is_active, schedule_time, prescription_meta, created_at,
                   dose_form, units_per_dose
            FROM medication
            WHERE u_id = $1
            ORDER BY is_active DESC, created_at DESC
            """,
            u_id,
        )
    result = []
    for r in rows:
        row = dict(r)
        row["units_per_dose"] = _units_json(row.get("units_per_dose"))
        result.append(row)
    return result


@router.post("")
async def create_medication(
    payload: MedicationPayload,
    user: dict = Depends(get_consented_user),
):
    u_id = await _get_u_id(user)
    if not u_id:
        return JSONResponse(
            {"detail": "Local user not found. Please link your account first."},
            status_code=404,
        )
    remaining = payload.pills_remaining if payload.pills_remaining is not None else payload.total_pills
    dose_form = payload.dose_form or "solid_oral"
    units_per_dose = payload.units_per_dose if payload.units_per_dose is not None else Decimal("1")
    pool = get_pool()
    async with pool.acquire() as conn:
        try:
            med_id = await conn.fetchval(
                """
                INSERT INTO medication
                    (u_id, med_name, dosage, pill_prescribed, pills_remaining,
                     instructions, warning, pill_description, use_before,
                     is_active, schedule_time, prescription_meta, dose_form, units_per_dose)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
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
                json.dumps(payload.prescription_meta) if payload.prescription_meta else None,
                dose_form,
                units_per_dose,
            )
            # Auto-generate intake schedule rows
            await _generate_intake_schedule(
                conn, med_id, u_id, payload.schedule_time, payload.use_before
            )
        except Exception as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
    return {"id": med_id, "name": payload.name.strip(), "dose_form": dose_form,
            "units_per_dose": _units_json(units_per_dose)}


@router.patch("/{med_id}")
async def update_medication(
    med_id: int,
    payload: MedicationPayload,
    user: dict = Depends(get_consented_user),
):
    u_id = await _get_u_id(user)
    if not u_id:
        return JSONResponse({"detail": "User not found"}, status_code=404)
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            previous = await conn.fetchrow(
                """
                SELECT schedule_time, use_before
                FROM medication
                WHERE med_id=$1 AND u_id=$2
                FOR UPDATE
                """,
                med_id,
                u_id,
            )
            if not previous:
                updated = None
            else:
                old_signature = (
                    _normalized_schedule(previous["schedule_time"]),
                    (previous["use_before"] or "").strip(),
                )
                new_signature = (
                    _normalized_schedule(payload.schedule_time),
                    (payload.use_before or "").strip(),
                )
                updated = await conn.fetchval(
                    """
                    UPDATE medication
                    SET med_name=$1, dosage=$2, pill_prescribed=$3, pills_remaining=$4,
                        instructions=$5, warning=$6, pill_description=$7, use_before=$8,
                        is_active=$9, schedule_time=$10, prescription_meta=$11,
                        dose_form=COALESCE($14, dose_form), units_per_dose=COALESCE($15, units_per_dose)
                    WHERE med_id=$12 AND u_id=$13
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
                    json.dumps(payload.prescription_meta) if payload.prescription_meta else None,
                    med_id,
                    u_id,
                    payload.dose_form,
                    payload.units_per_dose,
                )
                if old_signature != new_signature:
                    # Completed and historical rows remain part of the audit
                    # history. Only future rows that have not been confirmed
                    # are replaced by the new schedule.
                    await conn.execute(
                        """
                        DELETE FROM intake
                        WHERE med_id=$1 AND u_id=$2
                          AND intake_stats IN ('pending', 'missed')
                          AND intake_time_stamp > NOW()
                        """,
                        med_id,
                        u_id,
                    )
                    await _generate_intake_schedule(
                        conn,
                        med_id,
                        u_id,
                        payload.schedule_time,
                        payload.use_before,
                    )
    if not updated:
        return JSONResponse({"detail": "Medication not found"}, status_code=404)
    return {"id": updated}


@router.delete("/{med_id}")
async def delete_medication(med_id: int, user: dict = Depends(get_consented_user)):
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
