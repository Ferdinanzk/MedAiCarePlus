import datetime
import json
from decimal import Decimal
from typing import Optional
from zoneinfo import ZoneInfo
from pydantic import BaseModel, Field, field_validator
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from app.dependencies import get_consented_user
from app.database import get_pool
from app.config import MEDCARE_TIMEZONE
from app.services import dose_emotion, dose_safety, schedule

router = APIRouter(prefix="/api/medications", tags=["medications-api"])
_MEDCARE_TZ = ZoneInfo(MEDCARE_TIMEZONE)


async def _generate_intake_schedule(
    conn,
    med_id: int,
    u_id: int,
    schedule_time: dict | None,
    use_before: str | None,
    start_at: datetime.datetime | None = None,
):
    """Generate future intake rows for the next 30 days (or until use_before)."""
    now = start_at or datetime.datetime.now(_MEDCARE_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_MEDCARE_TZ)
    values = [(u_id, med_id, moment)
              for moment in schedule.occurrences(schedule_time, now, until=schedule.parse_date(use_before))]
    if values:
        await conn.executemany(
            """
            INSERT INTO intake (u_id, med_id, intake_time_stamp, intake_stats, notify_stats)
            VALUES ($1, $2, $3, 'pending', 'pending')
            ON CONFLICT DO NOTHING
            """,
            values,
        )


async def _clear_future_doses(conn, med_id: int, u_id: int) -> None:
    """Completed and past rows stay as history; only future, unresolved rows are removed."""
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


DOSE_FORMS = ("solid_oral", "liquid", "inhaler", "injection", "topical", "other")


class MedicationPayload(BaseModel):
    name: str
    dosage: Optional[str] = None
    total_pills: int = 0
    pills_remaining: Optional[Decimal] = None
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
    # Overdose protection's limits for this medicine (services/dose_safety.py). null = the default from its schedule
    # (half the shortest gap between its dose times, or 4 h; as many doses a day as it has times, or no limit). On
    # update a field left out keeps the stored value, and an explicit null returns to the default.
    min_interval_minutes: Optional[int] = Field(default=None, ge=30, le=2880)
    max_daily_doses: Optional[int] = Field(default=None, ge=1, le=24)

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

    @field_validator("pills_remaining")
    @classmethod
    def _check_remaining(cls, value):
        # NUMERIC(8,2): half tablets are allowed.
        if value is not None and (not value.is_finite() or value < 0 or value >= 1_000_000
                                  or value != value.quantize(Decimal("0.01"))):
            raise ValueError("pills_remaining must be >= 0 with at most two decimals")
        return value

    @field_validator("schedule_time")
    @classmethod
    def _check_schedule(cls, value):
        return schedule.validate(value)


class SupplyPayload(BaseModel):
    quantity: Decimal = Field(gt=0, lt=10_000)
    note: Optional[str] = Field(default=None, max_length=200)

    @field_validator("quantity")
    @classmethod
    def _two_decimals(cls, value):
        if value != value.quantize(Decimal("0.01")):
            raise ValueError("quantity has at most two decimals")
        return value


def _number(value) -> float | None:
    return float(value) if value is not None else None


def _expiry_warning(use_before: str | None, today: datetime.date) -> str | None:
    expires = schedule.parse_date(use_before)
    if expires is None:
        return None
    days_left = (expires - today).days
    if days_left < 0:
        return "Expired"
    if days_left <= 7:
        return f"Expires in {days_left} days"
    return None


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
            f"""
            SELECT
                i.intk_id        AS intake_id,
                m.med_id         AS med_id,
                m.med_name       AS name,
                m.dosage,
                m.pills_remaining,
                m.units_per_dose,
                m.dose_form,
                m.pill_description,
                m.warning,
                m.use_before,
                m.prescription_meta->>'course_end' AS course_end,
                m.schedule_time,
                i.intake_time_stamp AS scheduled_time,
                {schedule.previous_sql('i')} AS previous_time,
                {dose_safety.next_sql('i')} AS next_time,
                i.actual_intake_time AS taken_at,
                i.intake_stats   AS status,
                i.intk_id        AS id,
                dose_emotion_best.dose_emotion
            FROM medication m
            JOIN intake i ON i.med_id = m.med_id
            {dose_emotion.chip_join('i.intk_id', 'i.u_id', 'i.intake_stats')}
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
        # A scanned course's last day is stored in use_before to end its reminders (Scan page, course_end in
        # prescription_meta); reaching it is not the medicine expiring.
        course_end = row.pop("course_end", None)
        row["use_before_warning"] = (None if course_end and course_end == row.get("use_before")
                                     else _expiry_warning(row.get("use_before"), today))
        ts = row.get("scheduled_time")
        row["slot_label"] = schedule.slot_label(row.get("schedule_time"), ts.astimezone(_MEDCARE_TZ)) if ts else ""
        # From when the dose may be started or recorded (DOSE_EARLY_MINUTES before its time, or halfway from the
        # same medicine's previous dose if that is later); the server enforces it.
        row["due_from"] = schedule.due_from(ts, row.pop("previous_time"))
        # Until when a missed dose may still be taken (halfway to the next one); while overdose protection is on,
        # the server refuses it afterwards (dose_expired). None: it never expires.
        row["expires_at"] = dose_safety.expires_at(ts, row.pop("next_time", None))
        row["pills_remaining"] = _number(row.get("pills_remaining"))
        row["units_per_dose"] = _number(row.get("units_per_dose"))
        if "dose_emotion" in row:
            # Facial expression while this dose was taken (a camera session's result), or None.
            row["emotion"] = dose_emotion.chip(row.pop("dose_emotion"))
        result.append(row)
    return result


@router.post("/{med_id}/intake-now")
async def intake_now(med_id: int, user: dict = Depends(get_consented_user)):
    """Resolve a medication card's Take Now action to a pending intake row.

    Today's scheduled rows a pill taken now may count for (dose_safety.open_sql:
    their time has arrived, or is at most DOSE_EARLY_MINUTES away and past
    halfway from the previous dose, and while overdose protection is on they
    have not expired; missed rows included) are reused, nearest to now first,
    the earlier one on a tie. A pill taken at 10:01 with 08:00 open and 12:00
    due counts as 12:00: crediting 08:00 would leave the 12:00 reminder to
    prompt a second pill two hours later. If the next scheduled slot is further
    away, or the medication is unscheduled, create one ad-hoc row at the current
    time: a later scheduled dose is never consumed early. Under protection, a
    missed dose that has expired is not made up that way (409 dose_expired:
    wait for the next one), and the dose returned must pass every rule (409
    dose_too_soon / daily_max_reached otherwise, ad-hoc rows included; nothing
    is created then). The medication row lock makes repeated/concurrent clicks
    return the same pending row instead of creating duplicate doses. Nothing
    is marked taken here; monitor/commit_monitored owns that transition after
    camera evidence.
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
                SELECT med_id, med_name, dosage, pills_remaining, units_per_dose, warning,
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
            if (medication["pills_remaining"] or 0) < (medication.get("units_per_dose") or 1):
                return JSONResponse({"detail": "No pills remain for this medication"}, status_code=409)

            # A due row is the medication's existing scheduled dose. Rows that
            # are not due yet are intentionally not selected: Take Now means the
            # dose starts now even when the next scheduled slot is later today.
            row = await conn.fetchrow(
                f"""
                SELECT i.intk_id AS id, i.med_id, m.med_name AS name, m.dosage,
                       i.intake_time_stamp AS scheduled_time, i.intake_stats AS status,
                       {schedule.previous_sql('i')} AS previous_time,
                       m.pills_remaining, m.warning
                FROM intake i
                JOIN medication m ON m.med_id=i.med_id
                WHERE i.intk_id IN (
                    SELECT intk_id
                    FROM intake
                    WHERE med_id=$1 AND u_id=$2
                      AND intake_stats IN ('pending', 'missed')
                      AND intake_time_stamp >= $3
                      AND {dose_safety.open_sql('intake', '$4::timestamptz')}
                    ORDER BY ABS(EXTRACT(EPOCH FROM (intake_time_stamp - $4::timestamptz))),
                             intake_time_stamp
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
                # The latest scheduled dose of today that has come is still open: it expired (or it would have been
                # picked). Under protection the pill is not taken as an ad-hoc dose instead (409 dose_expired); with
                # protection off check() lets it through and an ad-hoc dose is made as before. An earlier ad-hoc dose
                # left pending is no missed dose: it never expires, and it does not stand in for the schedule here.
                latest = await conn.fetchrow(
                    f"""
                    SELECT intk_id, intake_stats FROM intake
                    WHERE med_id=$1 AND u_id=$2 AND intake_time_stamp >= $3 AND intake_time_stamp <= $4
                      AND NOT {dose_safety.ad_hoc_sql('intake')}
                    ORDER BY intake_time_stamp DESC, intk_id DESC
                    LIMIT 1
                    """,
                    med_id,
                    u_id,
                    local_start,
                    now,
                )
                if latest is not None and latest["intake_stats"] in ("pending", "missed"):
                    await dose_safety.check(conn, u_id, [latest["intk_id"]], at=now)
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
                # An ad-hoc row keeps the moment it was made, never a whole minute: that is how it is told from a
                # scheduled one (dose_safety.is_ad_hoc).
                if (now.second, now.microsecond) == (0, 0):
                    now += datetime.timedelta(microseconds=1)
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
            # Every rule, on the dose the pill would count for. A refusal rolls back an ad-hoc row made just now.
            await dose_safety.check(conn, u_id, [row["id"]], at=now)
            row["pills_remaining"] = _number(row.get("pills_remaining"))
            # An ad-hoc row is made now, so it is due whatever came before it.
            row["due_from"] = schedule.due_from(row.get("scheduled_time"), row.pop("previous_time", None))
            return row


@router.patch("/intake/{intk_id}")
async def update_intake_status(
    intk_id: int,
    payload: dict,
    user: dict = Depends(get_consented_user),
):
    """
    Update an intake record's status (e.g., mark as 'taken').
    Marking a dose taken removes units_per_dose from stock; undoing it puts that amount back.
    """
    u_id = await _get_u_id(user)
    if not u_id:
        return JSONResponse({"detail": "User not found"}, status_code=404)

    new_status = payload.get("status")
    if new_status not in ("taken", "skipped", "missed", "pending"):
        return JSONResponse({"detail": "Invalid status"}, status_code=400)

    # transition_intake refuses doses awaiting caregiver confirmation under the row
    # lock (409 "awaiting_caregiver_confirmation"), and 'taken' for a dose overdose
    # protection refuses (schedule.DoseRefused: 409 "dose_not_due_yet",
    # "dose_too_soon", "daily_max_reached" or "dose_expired", from main.py).
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
                   is_active, archived_at, schedule_time, prescription_meta, created_at,
                   dose_form, units_per_dose, min_interval_minutes, max_daily_doses
            FROM medication
            WHERE u_id = $1
            ORDER BY is_active DESC, created_at DESC
            """,
            u_id,
        )
    today = datetime.datetime.now(_MEDCARE_TZ).date()
    result = []
    for r in rows:
        row = dict(r)
        supply = schedule.supply(row.get("pills_remaining"), row.get("units_per_dose"),
                                 row.get("schedule_time"), today) if row.get("is_active") else {}
        row["units_per_dose"] = _number(row.get("units_per_dose"))
        row["pills_remaining"] = _number(row.get("pills_remaining"))
        row["daily_units"] = supply.get("daily_units")
        row["days_left"] = supply.get("days_left")
        row["run_out_date"] = supply.get("run_out_date")
        # What null means for this medicine's overdose protection limits (from its schedule), for the edit form.
        row["default_min_interval_minutes"] = int(dose_safety.min_gap(row.get("schedule_time")).total_seconds() // 60)
        row["default_max_daily_doses"] = dose_safety.daily_max(row.get("schedule_time"))
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
    remaining = payload.pills_remaining if payload.pills_remaining is not None else Decimal(payload.total_pills)
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
                     is_active, schedule_time, prescription_meta, dose_form, units_per_dose,
                     min_interval_minutes, max_daily_doses)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
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
                payload.min_interval_minutes,
                payload.max_daily_doses,
            )
            # Auto-generate intake schedule rows
            if payload.is_active:
                await _generate_intake_schedule(
                    conn, med_id, u_id, payload.schedule_time, payload.use_before
                )
        except Exception as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
    return {"id": med_id, "name": payload.name.strip(), "dose_form": dose_form,
            "units_per_dose": _number(units_per_dose), "min_interval_minutes": payload.min_interval_minutes,
            "max_daily_doses": payload.max_daily_doses}


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
                SELECT schedule_time, use_before, is_active
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
                    schedule.signature(previous["schedule_time"]),
                    (previous["use_before"] or "").strip(),
                    bool(previous.get("is_active", True)),
                )
                new_signature = (
                    schedule.signature(payload.schedule_time),
                    (payload.use_before or "").strip(),
                    payload.is_active,
                )
                updated = await conn.fetchrow(
                    """
                    UPDATE medication
                    SET med_name=$1, dosage=$2, pill_prescribed=$3, pills_remaining=$4,
                        instructions=$5, warning=$6, pill_description=$7, use_before=$8,
                        is_active=$9, schedule_time=$10, prescription_meta=$11,
                        dose_form=COALESCE($14, dose_form), units_per_dose=COALESCE($15, units_per_dose),
                        min_interval_minutes=CASE WHEN $16 THEN $17::int ELSE min_interval_minutes END,
                        max_daily_doses=CASE WHEN $18 THEN $19::int ELSE max_daily_doses END,
                        archived_at=CASE WHEN $9 THEN NULL ELSE COALESCE(archived_at, NOW()) END
                    WHERE med_id=$12 AND u_id=$13
                    RETURNING med_id, min_interval_minutes, max_daily_doses
                    """,
                    payload.name.strip(),
                    payload.dosage,
                    payload.total_pills,
                    payload.pills_remaining if payload.pills_remaining is not None else Decimal(payload.total_pills),
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
                    # Left out: keep; given (null included): set. null returns to the schedule's default.
                    "min_interval_minutes" in payload.model_fields_set,
                    payload.min_interval_minutes,
                    "max_daily_doses" in payload.model_fields_set,
                    payload.max_daily_doses,
                )
                if old_signature != new_signature:
                    await _clear_future_doses(conn, med_id, u_id)
                    if payload.is_active:
                        await _generate_intake_schedule(
                            conn,
                            med_id,
                            u_id,
                            payload.schedule_time,
                            payload.use_before,
                        )
    if not updated:
        return JSONResponse({"detail": "Medication not found"}, status_code=404)
    return {"id": updated["med_id"], "min_interval_minutes": updated["min_interval_minutes"],
            "max_daily_doses": updated["max_daily_doses"]}


@router.post("/{med_id}/archive")
async def archive_medication(med_id: int, user: dict = Depends(get_consented_user)):
    """Stop a course: no more doses or reminders, history kept."""
    u_id = await _get_u_id(user)
    if not u_id:
        return JSONResponse({"detail": "User not found"}, status_code=404)
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            archived = await conn.fetchval(
                "UPDATE medication SET is_active=FALSE, archived_at=COALESCE(archived_at, NOW()) "
                "WHERE med_id=$1 AND u_id=$2 RETURNING med_id",
                med_id, u_id)
            if archived:
                await _clear_future_doses(conn, med_id, u_id)
    if not archived:
        return JSONResponse({"detail": "Medication not found"}, status_code=404)
    return {"id": archived, "is_active": False}


@router.post("/{med_id}/reactivate")
async def reactivate_medication(med_id: int, user: dict = Depends(get_consented_user)):
    """Resume an archived course from now on, with its saved schedule."""
    u_id = await _get_u_id(user)
    if not u_id:
        return JSONResponse({"detail": "User not found"}, status_code=404)
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "UPDATE medication SET is_active=TRUE, archived_at=NULL "
                "WHERE med_id=$1 AND u_id=$2 RETURNING med_id, schedule_time, use_before",
                med_id, u_id)
            if row:
                await _generate_intake_schedule(conn, med_id, u_id, schedule.load(row["schedule_time"]),
                                                row["use_before"])
    if not row:
        return JSONResponse({"detail": "Medication not found"}, status_code=404)
    return {"id": row["med_id"], "is_active": True}


@router.post("/{med_id}/supply")
async def add_supply(med_id: int, payload: SupplyPayload, user: dict = Depends(get_consented_user)):
    """Record a refill: the quantity is added to stock and kept as a supply record."""
    u_id = await _get_u_id(user)
    if not u_id:
        return JSONResponse({"detail": "User not found"}, status_code=404)
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "UPDATE medication SET pills_remaining=pills_remaining+$3 WHERE med_id=$1 AND u_id=$2 "
                "RETURNING pills_remaining, units_per_dose, schedule_time, is_active",
                med_id, u_id, payload.quantity)
            if row:
                await conn.execute(
                    "INSERT INTO medication_supply (u_id, med_id, quantity, note) VALUES ($1,$2,$3,$4)",
                    u_id, med_id, payload.quantity, (payload.note or "").strip() or None)
    if not row:
        return JSONResponse({"detail": "Medication not found"}, status_code=404)
    today = datetime.datetime.now(_MEDCARE_TZ).date()
    supply = schedule.supply(row["pills_remaining"], row["units_per_dose"], row["schedule_time"], today) \
        if row["is_active"] else {}
    return {"id": med_id, "pills_remaining": _number(row["pills_remaining"]),
            "days_left": supply.get("days_left"), "run_out_date": supply.get("run_out_date")}


@router.get("/{med_id}/supply")
async def list_supply(med_id: int, user: dict = Depends(get_consented_user)):
    u_id = await _get_u_id(user)
    if not u_id:
        return JSONResponse({"detail": "User not found"}, status_code=404)
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT supply_id AS id, quantity, note, created_at FROM medication_supply "
            "WHERE med_id=$1 AND u_id=$2 ORDER BY created_at DESC LIMIT 50",
            med_id, u_id)
    return [{**dict(r), "quantity": _number(r["quantity"])} for r in rows]


@router.delete("/{med_id}")
async def delete_medication(med_id: int, user: dict = Depends(get_consented_user)):
    """Only a medication without any history can be deleted; otherwise archive it."""
    u_id = await _get_u_id(user)
    if not u_id:
        return JSONResponse({"detail": "User not found"}, status_code=404)
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            has_history = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM intake WHERE med_id=$1 AND u_id=$2 "
                "AND (intake_stats NOT IN ('pending') OR intake_time_stamp <= NOW()))",
                med_id, u_id)
            if has_history:
                return JSONResponse({"detail": "has_history"}, status_code=409)
            deleted = await conn.fetchval(
                "DELETE FROM medication WHERE med_id=$1 AND u_id=$2 RETURNING med_id",
                med_id, u_id,
            )
    if not deleted:
        return JSONResponse({"detail": "Medication not found"}, status_code=404)
    return {"deleted": deleted}
