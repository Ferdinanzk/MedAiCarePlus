"""Leased robot tasks: one open task per patient and 5-minute slot.

queued -> leased -> searching -> in_progress -> completed | not_found | aborted
(leased -> aborted, searching -> not_found | aborted). The minute job returns
expired leases to queued, or marks tasks expired once past expires_at.
"""

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app import config
from app.config import MEDCARE_TIMEZONE
from app.database import get_pool
from app.services import ai_settings, consent_service, dose_safety, ocr_parsing, outbox, schedule

log = logging.getLogger(__name__)

LEASE_SECONDS = 60
OFFLINE_AFTER_SECONDS = 60
OFFLINE_NOTICE_WINDOW_SECONDS = 6 * 3600
OPEN_STATUSES = ("queued", "leased", "searching", "in_progress")
TERMINAL_STATUSES = ("completed", "not_found", "aborted")
TRANSITIONS = {
    "leased": {"searching", "aborted"},
    "searching": {"in_progress", "not_found", "aborted"},
    "in_progress": {"completed", "not_found", "aborted"},
}
_OPEN_SQL = "('queued','leased','searching','in_progress')"
_MEAL_WAITING_SQL = "('queued','leased','searching')"   # a meal retry that has not reached the patient yet
_LOCAL_TZ = ZoneInfo(MEDCARE_TIMEZONE)


class TaskNotFound(LookupError):
    pass


def is_supported(dose_form: str | None, units_per_dose) -> bool:
    """Automatic recording applies only to one solid oral unit (tablet/capsule)."""
    try:
        return dose_form == "solid_oral" and float(units_per_dose) == 1.0
    except (TypeError, ValueError):
        return False


CHECKIN_SCOPES = ("robot_microphone", "cloud_voice", "conversation_analysis", "safety_alerts")


def checkin_allowed(consent_state: dict) -> bool:
    return all(consent_service.is_current(consent_state, scope) for scope in CHECKIN_SCOPES)


def slot_label(slot_time: datetime) -> str:
    if slot_time.tzinfo is None:
        slot_time = slot_time.replace(tzinfo=timezone.utc)
    return slot_time.astimezone(_LOCAL_TZ).strftime("%H:%M")


def _iso(value):
    return value.isoformat() if value is not None else None


# Quick fixes 2 of 19 Oct 2026: before an after-meal dose at lunch or dinner Reachy asks 「您有吃飯了嗎？」 (the
# meal_check line). The meal times are local (MEDCARE_TIMEZONE), "HH:MM", both ends included; breakfast is not one.
# Quick fixes 3: a medicine taken before a meal or on an empty stomach never gets the question, whatever else its
# instructions say (「空腹（飯前1小時或飯後2小時）服用」 also names 飯後). A warning against an empty stomach
# (「飯後服用，避免空腹」, 「勿空腹服用」: ocr_parsing.NOT_EMPTY_STOMACH) says the opposite and keeps the question.
MEAL_WINDOWS = (("10:30", "14:30"), ("16:30", "21:30"))
AFTER_MEAL_WORDS = ("飯後", "餐後")
BEFORE_MEAL_WORDS = ("空腹", "飯前", "餐前")


# Quick fix 4 of 20 Oct 2026: when the patient has not eaten yet (the robot reports a task completed with the outcome
# meal_not_eaten for a dose), Reachy comes back MEAL_RETRY_MINUTES later: one task (a meal retry) that the lease hands
# out only from its stored not_before on, at most MEAL_RETRY_LIMIT per dose. Only meal retries have a not_before.
MEAL_RETRY_MINUTES = 20
MEAL_RETRY_LIMIT = 3
MANUAL_TASK_MIN_MINUTES = 15   # a manual task ('Use Reachy', 'Talk to Reachy now') stays open at least this long


def after_meal(schedule_time, instructions: str | None, dose_time: datetime | None) -> bool:
    """The dose is taken after a meal (its medicine's schedule_time.after_meals is true, or its instructions say 飯後
    or 餐後) and its local time is at lunch or dinner (MEAL_WINDOWS). Never when schedule_time.before_meals is true
    or the instructions say 空腹 (not as in 避免空腹), 飯前 or 餐前."""
    schedule_time, words = schedule.load(schedule_time), ocr_parsing.NOT_EMPTY_STOMACH.sub("", instructions or "")
    if schedule_time.get("before_meals") is True or any(word in words for word in BEFORE_MEAL_WORDS):
        return False
    marked = schedule_time.get("after_meals") is True or any(word in words for word in AFTER_MEAL_WORDS)
    if not marked or dose_time is None:
        return False
    if dose_time.tzinfo is None:
        dose_time = dose_time.replace(tzinfo=timezone.utc)
    local = dose_time.astimezone(_LOCAL_TZ).strftime("%H:%M")
    return any(start <= local <= end for start, end in MEAL_WINDOWS)


async def task_payload(conn, task) -> dict:
    """Task as the bridge sees it. Doses are re-read on every call (restart recovery)."""
    doses = await conn.fetch(
        "SELECT i.intk_id, m.med_id, m.med_name, m.pill_description, m.dose_form, m.units_per_dose, i.intake_stats, "
        "m.schedule_time, m.instructions, i.intake_time_stamp FROM intake i JOIN medication m ON m.med_id = i.med_id "
        "WHERE i.u_id = $1 AND i.intk_id = ANY($2::int[]) ORDER BY m.med_name, m.med_id, i.intk_id",
        task["u_id"], list(task["intk_ids"]))
    head = await conn.fetchrow(
        'SELECT u.name, d.auto_record, d.device_id IS NOT NULL AS paired FROM "user" u '
        "LEFT JOIN reachy_device d ON d.u_id = u.u_id AND d.revoked_at IS NULL WHERE u.u_id = $1",
        task["u_id"])
    consent = await consent_service.fetch_state(conn, task["u_id"])
    checkin = checkin_allowed(consent)
    from app.services import memory   # here, not at the top, as voice_lines below
    return {
        "task_id": str(task["task_id"]), "slot_time": _iso(task["slot_time"]), "reason": task["reason"],
        "attempt": task["attempt"], "status": task["status"], "expires_at": _iso(task["expires_at"]),
        "patient_name": head["name"] if head else None,
        # The name Reachy calls the patient (memory.call_name; None when there is none it would say): the robot's own
        # voice says it in the dose greeting when the named line has no copy in the live voice (quick fixes, 19 Oct).
        "call_name": await memory.call_name(conn, task["u_id"], memory.consent_current(consent)),
        "auto_record": bool(head and head["auto_record"]),
        # The robot may listen (speech-to-text on the robot) only while this consent is current.
        "microphone": consent_service.is_current(consent, "robot_microphone"),
        # A check-in conversation needs every check-in scope (notice: check-ins only with safety alerts on).
        "checkin": checkin,
        # A live voice check-in (spec 2026-10-12 E1): .env's test switch, a paired robot, check-ins allowed and a
        # usable live model on the AI card. Otherwise false, and the robot runs today's text chat. Live voice is the
        # default for every robot: the Reachy card's per-robot "Live voice (test)" switch no longer counts (13 Oct
        # 2026: a robot paired to another patient had it off, and its check-ins silently ran the text chat).
        "live": bool(config.LIVE_VOICE_TEST and head and head["paired"] and checkin
                     and ai_settings.live_target() is not None),
        "doses": [{
            "intk_id": d["intk_id"], "med_id": d["med_id"], "med_name": d["med_name"],
            "pill_description": d["pill_description"], "dose_form": d["dose_form"],
            "units_per_dose": float(d["units_per_dose"]) if d["units_per_dose"] is not None else None,
            "intake_stats": d["intake_stats"], "supported": is_supported(d["dose_form"], d["units_per_dose"]),
            # The robot asks whether the patient has eaten before such a dose's prompt (quick fixes 2, 19 Oct).
            "after_meal": after_meal(d["schedule_time"], d["instructions"], d["intake_time_stamp"]),
        } for d in doses],
    }


async def enqueue_reachy_task(conn, u_id: int, slot_time, intk_ids: list[int], reason: str, expires_at,
                              at=None, not_before=None) -> str | None:
    """_enqueue (the task), then the patient's own lines in the live voice rendered ahead for the check-in the task
    ends with (services/voice_lines.py; nothing without a live voice target)."""
    task_id = await _enqueue(conn, u_id, slot_time, intk_ids, reason, expires_at, at, not_before)
    if task_id is not None:
        from app.services import voice_lines   # here, not at the top: voice_lines uses this module's consent gate
        voice_lines.render_ahead_for_patient(u_id)
    return task_id


async def manual_expires_at(conn, u_id: int, slot_time, start) -> datetime:
    """When a manual task for the slot at slot_time, open from `start`, expires: at the end of the slot's missed window
    (remind_after_minutes x (remind_after_retries + 1) after it) or MANUAL_TASK_MIN_MINUTES after `start`, whichever
    is later."""
    settings_row = await conn.fetchrow(
        "SELECT COALESCE(remind_after_minutes, 10) AS after, COALESCE(remind_after_retries, 3) AS retries "
        "FROM notification_settings WHERE u_id = $1", u_id)
    after, retries = (settings_row["after"], settings_row["retries"]) if settings_row else (10, 3)
    return max(slot_time + timedelta(minutes=after * (retries + 1)),
               start + timedelta(minutes=MANUAL_TASK_MIN_MINUTES))


async def _enqueue(conn, u_id: int, slot_time, intk_ids: list[int], reason: str, expires_at,
                   at=None, not_before=None) -> str | None:
    """Open (or re-arm a queued) task for this slot inside the caller's transaction.

    Returns the open task id, or None when the patient has no active device or
    no current robot camera consent. A task already leased/searching/in_progress
    is never re-armed or reset; a missed_retry for a slot whose task is held
    in_progress (a companion session after a dose was left pending) marks it
    retry_due, so task_due brings the dose up and set_status queues the retry
    once that task is over. Raises a schedule.DoseRefused when overdose
    protection refuses a listed dose (dose_safety.check, judged at `at`, default
    now): the robot must never start a dose hours early, a second dose, or a
    missed one. Callers that picked the doses at a moment of their own pass it.
    A meal retry passes not_before (_queue_meal_retry): stored with the task, the lease waits for it.
    """
    if reason not in ("upcoming", "missed_retry", "manual", "checkin") or (not intk_ids and reason != "checkin"):
        raise ValueError("Invalid task request")
    device = await conn.fetchval(
        "SELECT device_id FROM reachy_device WHERE u_id = $1 AND revoked_at IS NULL", u_id)
    if device is None:
        return None
    state = await consent_service.fetch_state(conn, u_id)
    if not (consent_service.is_current(state, "core") and consent_service.is_current(state, "robot_camera")):
        return None
    if intk_ids:
        await dose_safety.check(conn, u_id, list(intk_ids), at=at)
    task_id = await conn.fetchval(
        "INSERT INTO reachy_task (task_id, u_id, slot_time, intk_ids, reason, expires_at, not_before) "
        "VALUES ($1::uuid, $2, $3, $4::int[], $5, $6, $7) ON CONFLICT DO NOTHING RETURNING task_id",
        str(uuid.uuid4()), u_id, slot_time, list(intk_ids), reason, expires_at, not_before)
    if task_id is not None:
        return str(task_id)
    task_id = await conn.fetchval(
        "UPDATE reachy_task SET attempt = attempt + 1, expires_at = GREATEST(expires_at, $3) "
        "WHERE u_id = $1 AND slot_time = $2 AND status = 'queued' RETURNING task_id",
        u_id, slot_time, expires_at)
    if task_id is None:
        task_id = await conn.fetchval(
            "SELECT task_id FROM reachy_task WHERE u_id = $1 AND slot_time = $2 "
            "AND status IN ('leased','searching','in_progress')",
            u_id, slot_time)
        if task_id is not None and reason == "missed_retry":
            await conn.execute(
                "UPDATE reachy_task SET detail = COALESCE(detail, '{}'::jsonb) || jsonb_build_object('retry_due', "
                "jsonb_build_object('intk_ids', $2::int[], 'expires_at', $3::text)) "
                "WHERE task_id = $1::uuid AND status = 'in_progress'",
                str(task_id), list(intk_ids), _iso(expires_at))
    return str(task_id) if task_id is not None else None


def _leasable_sql() -> str:
    """When the robot may lease a queued task t of patient $1 now (the rules in _lease_once's comment). task_due asks
    the same question, so the robot is never told of a task it could not lease. A meal retry (a not_before) only from
    that time on, and only while one of its doses is still to take: the robot never wakes for one whose doses were
    all taken, sent to family or skipped while it waited (the minute job then cancels it, maintenance)."""
    return ("t.u_id = $1 AND t.status = 'queued' AND t.expires_at > NOW() AND NOT EXISTS (SELECT 1 FROM intake i "
            "WHERE i.intk_id = ANY(t.intk_ids) "
            f"AND i.intake_stats IN ('pending','missed') AND NOT ({dose_safety.startable_sql('i', 'NOW()')})) "
            "AND (t.not_before IS NULL OR (t.not_before <= NOW() AND EXISTS (SELECT 1 FROM intake o "
            "WHERE o.intk_id = ANY(t.intk_ids) AND o.intake_stats IN ('pending','missed'))))")


def _meal_retry_sql(dose: str, statuses: str) -> str:
    """A meal retry (a not_before) in one of `statuses` holds the dose `dose` (an intake alias)."""
    return (f"EXISTS (SELECT 1 FROM reachy_task meal WHERE meal.u_id = {dose}.u_id AND meal.not_before IS NOT NULL "
            f"AND meal.status IN {statuses} AND {dose}.intk_id = ANY(meal.intk_ids))")


def meal_retry_waiting_sql(dose: str) -> str:
    """A meal retry that has not reached the patient yet (queued, leased or searching) holds the dose `dose` (an
    intake alias) while the patient's robot is online (a heartbeat within OFFLINE_AFTER_SECONDS): Reachy comes back
    for it after the meal, so the missed-dose job sends nothing of its own for it meanwhile. Review of quick fix 4:
    once Reachy is with the patient (in_progress) the job goes on as for any dose task, so a dose left pending there
    gets the job's next retry, a task of the slot's own that task_due reports, and the check-in that follows ends for
    the medicine instead of holding the dose for as long as the chat runs. A robot offline holds nothing back either:
    the patient gets the LINE reminders the robot-offline notice promises family."""
    return (f"({_meal_retry_sql(dose, _MEAL_WAITING_SQL)} AND EXISTS (SELECT 1 FROM reachy_device meal_device "
            f"WHERE meal_device.u_id = {dose}.u_id AND meal_device.revoked_at IS NULL AND meal_device.last_seen_at >= "
            f"NOW() - INTERVAL '{OFFLINE_AFTER_SECONDS} seconds'))")


async def _lease_once(u_id: int, device_id: str) -> dict | None:
    # A task holding an open dose that is not due yet stays queued until it is, and one holding an open dose that has
    # expired is not handed out (enqueue refuses such tasks; this also covers any queued before the rules, or that
    # lapsed while queued). Doses already taken, skipped or waiting for family don't count: expiry is a matter of
    # time alone, and a taken dose past its halfway point must not keep the slot's other doses from the robot.
    # Overdose protection's switch decides, as in dose_safety.startable_sql; monitor/start checks the rest.
    # Medicine comes first (companion spec 2.5): a check-in ('Talk to Reachy now', its slot_time the press) waits
    # behind every leasable dose task, then the oldest slot goes first.
    async with get_pool().acquire() as conn, conn.transaction():
        task = await conn.fetchrow(
            "UPDATE reachy_task SET status = 'leased', lease_owner = $2::uuid, "
            f"lease_until = NOW() + INTERVAL '{LEASE_SECONDS} seconds' "
            f"WHERE task_id = (SELECT t.task_id FROM reachy_task t WHERE {_leasable_sql()} "
            "ORDER BY (t.reason = 'checkin'), t.slot_time, t.created_at LIMIT 1 FOR UPDATE SKIP LOCKED) "
            "RETURNING *",
            u_id, device_id)
        return await task_payload(conn, task) if task else None


async def lease_next(u_id: int, device_id: str, wait: float) -> dict | None:
    """Lease the oldest queued task, long-polling up to `wait` seconds (checked every second)."""
    deadline = time.monotonic() + max(0.0, float(wait))
    while True:
        task = await _lease_once(u_id, device_id)
        remaining = deadline - time.monotonic()
        if task is not None or remaining <= 0:
            return task
        await asyncio.sleep(min(1.0, remaining))


def _retry_due_sql() -> str:
    """A held task of patient $1 that a missed-dose retry marked (enqueue_reachy_task: detail.retry_due) while a
    companion session kept it in progress, with a marked dose still open and startable and the retry not expired:
    set_status queues that retry when the task is over, so it is a medicine still to take."""
    return ("SELECT 1 FROM reachy_task held WHERE held.u_id = $1 AND held.status = 'in_progress' "
            "AND held.detail ? 'retry_due' AND (held.detail->'retry_due'->>'expires_at')::timestamptz > NOW() "
            "AND EXISTS (SELECT 1 FROM intake i WHERE i.u_id = held.u_id AND i.intk_id IN "
            "(SELECT jsonb_array_elements_text(held.detail->'retry_due'->'intk_ids')::int) "
            f"AND i.intake_stats IN ('pending','missed') AND {dose_safety.startable_sql('i', 'NOW()')})")


async def task_due(conn, u_id: int) -> bool:
    """Medicine first (companion spec 3.4), for the heartbeat: would the robot lease a task of this patient now (the
    lease's own rule, _leasable_sql) that holds a dose still to take? The task the robot is busy with is never
    queued, so it never counts, except when a missed-dose retry for its own slot came meanwhile (_retry_due_sql: a
    dose the patient left pending during a dose task, still owed while the companion session holds that task). A
    queued check-in ('Talk to Reachy now' pressed during a chat), or a task whose doses were all taken, skipped or
    sent to family meanwhile, is no medicine: it never interrupts a chat, and the robot leases it afterwards as
    today."""
    return bool(await conn.fetchval(
        f"SELECT EXISTS (SELECT 1 FROM reachy_task t WHERE {_leasable_sql()} AND EXISTS (SELECT 1 FROM intake "
        "open_dose WHERE open_dose.intk_id = ANY(t.intk_ids) AND open_dose.intake_stats IN ('pending','missed'))) "
        f"OR EXISTS ({_retry_due_sql()})",
        u_id))


async def current_task(u_id: int, device_id: str) -> dict | None:
    async with get_pool().acquire() as conn:
        task = await conn.fetchrow(
            "SELECT * FROM reachy_task WHERE u_id = $1 AND lease_owner = $2::uuid "
            "AND status IN ('leased','searching','in_progress') ORDER BY slot_time LIMIT 1",
            u_id, device_id)
        return await task_payload(conn, task) if task else None


async def set_status(u_id: int, device_id: str, task_id: str, status: str, detail: dict | None) -> dict:
    """Validated device-driven transition. TaskNotFound (404) / ValueError on an illegal one (409)."""
    try:
        uuid.UUID(str(task_id))
    except ValueError as exc:
        raise TaskNotFound("Task not found") from exc
    async with get_pool().acquire() as conn, conn.transaction():
        task = await conn.fetchrow(
            "SELECT * FROM reachy_task WHERE task_id = $1::uuid AND u_id = $2 FOR UPDATE", task_id, u_id)
        if not task:
            raise TaskNotFound("Task not found")
        if task["lease_owner"] is None or str(task["lease_owner"]) != str(device_id):
            raise ValueError("Task is not leased by this device")
        if task["status"] == status:
            # A retried request after a lost response: no transition, no error.
            return await task_payload(conn, task)
        if status not in TRANSITIONS.get(task["status"], set()):
            raise ValueError(f"Illegal transition {task['status']} -> {status}")
        detail_json = json.dumps(detail) if detail is not None else None
        if status in TERMINAL_STATUSES:
            held = task   # its detail before the robot's own replaces it: a retry_due mark (enqueue_reachy_task)
            task = await conn.fetchrow(
                "UPDATE reachy_task SET status = $2, detail = COALESCE($3::jsonb, detail), "
                "finished_at = NOW(), lease_until = NULL WHERE task_id = $1::uuid RETURNING *",
                task_id, status, detail_json)
            await _queue_retry_due(conn, held, detail)
            if status == "completed":
                await _queue_meal_retry(conn, task, detail)
        else:
            task = await conn.fetchrow(
                "UPDATE reachy_task SET status = $2, detail = COALESCE($3::jsonb, detail), "
                f"lease_until = NOW() + INTERVAL '{LEASE_SECONDS} seconds' WHERE task_id = $1::uuid RETURNING *",
                task_id, status, detail_json)
        if status == "aborted" and (detail or {}).get("reason") == "robot_offline":
            await notify_robot_offline(conn, u_id, task["slot_time"], datetime.now(timezone.utc))
        return await task_payload(conn, task)


async def _queue_retry_due(conn, held, ended: dict | None) -> None:
    """A task that a missed-dose retry marked while it was held (detail.retry_due) has just ended: queue that retry
    now, inside the caller's transaction and a savepoint, for the marked doses still open (pending or missed), until
    the retry's own expiry. Nothing when none is open or the retry has expired; a dose overdose protection refuses
    now is logged and skipped, as missed_dose_job.send_robot does. The robot then leases it before any check-in.
    A dose the robot's ending detail gives the outcome meal_not_eaten is left out (review of quick fixes 3): Reachy
    has just said it comes back once the patient has eaten, and the missed-dose job's next retry still comes."""
    detail = held["detail"]
    if isinstance(detail, str):
        detail = json.loads(detail)
    retry = detail.get("retry_due") if isinstance(detail, dict) else None
    if not retry:
        return
    expires_at = datetime.fromisoformat(retry["expires_at"])
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= schedule.current_time():
        return
    outcomes = (ended or {}).get("outcomes")
    not_eaten = {str(intk_id) for intk_id, outcome in outcomes.items()
                 if outcome == "meal_not_eaten"} if isinstance(outcomes, dict) else set()
    still_open = [row["intk_id"] for row in await conn.fetch(
        "SELECT intk_id FROM intake WHERE u_id = $1 AND intk_id = ANY($2::int[]) "
        "AND intake_stats IN ('pending','missed') ORDER BY intk_id", held["u_id"], list(retry["intk_ids"]))
        if str(row["intk_id"]) not in not_eaten]
    if not still_open:
        return
    try:
        async with conn.transaction():
            await enqueue_reachy_task(conn, held["u_id"], held["slot_time"], still_open, "missed_retry", expires_at)
    except schedule.DoseRefused as refused:
        log.info("no robot retry for the %s slot of patient %s after its task ended: dose %s refused (%s)",
                 slot_label(held["slot_time"]), held["u_id"], refused.intk_id, refused.detail)


async def _queue_meal_retry(conn, task, ended: dict | None) -> None:
    """Quick fix 4: the robot reported the task completed and its outcomes give doses meal_not_eaten (Reachy said
    「等您吃過飯，我再來陪您吃藥。」 and rests). One meal retry is queued for those doses still open, inside the caller's
    transaction and a savepoint: reason missed_retry (lease and payload treat every dose reason alike; the robot's
    own fallback opening is then the reminder clip), its slot_time and its stored not_before MEAL_RETRY_MINUTES from
    now, and it expires like a manual task from that time (manual_expires_at). Its own slot_time keeps it apart from
    the slot's task, so neither the missed-dose job's retries nor _queue_retry_due for the slot's other doses ever
    re-arm it. Left out: a dose another open meal retry already waits for, a dose with MEAL_RETRY_LIMIT meal retries
    already (counted from the stored tasks), and one overdose protection would refuse at that time (one that will
    have expired: the missed-dose job handles it at once)."""
    outcomes = (ended or {}).get("outcomes")
    if not isinstance(outcomes, dict):
        return
    not_eaten = [intk_id for intk_id in task["intk_ids"] if outcomes.get(str(intk_id)) == "meal_not_eaten"]
    if not not_eaten:
        return
    rows = [row for row in await conn.fetch(
        "SELECT i.intk_id, i.intake_time_stamp, (SELECT COUNT(*) FROM reachy_task meal WHERE meal.u_id = i.u_id "
        "AND meal.not_before IS NOT NULL AND i.intk_id = ANY(meal.intk_ids)) AS meal_retries, "
        f"{_meal_retry_sql('i', _OPEN_SQL)} AS waiting FROM intake i WHERE i.u_id = $1 AND i.intk_id = ANY($2::int[]) "
        "AND i.intake_stats IN ('pending','missed') ORDER BY i.intk_id", task["u_id"], not_eaten)
        if row["meal_retries"] < MEAL_RETRY_LIMIT and not row["waiting"]]
    not_before = schedule.current_time() + timedelta(minutes=MEAL_RETRY_MINUTES)
    allowed = set(await dose_safety.allowed(conn, task["u_id"], [row["intk_id"] for row in rows], at=not_before)
                  if rows else ())
    rows = [row for row in rows if row["intk_id"] in allowed]
    if not rows:
        log.info("no meal retry for patient %s after task %s: each dose not eaten for is resolved, already waiting, "
                 "at its %d meal retries or refused by then", task["u_id"], task["task_id"], MEAL_RETRY_LIMIT)
        return
    from app.jobs.missed_dose_job import _slot_key   # here, not at the top: that job imports this module
    slot = _slot_key(min(row["intake_time_stamp"] for row in rows))
    expires_at = await manual_expires_at(conn, task["u_id"], slot, not_before)
    try:
        async with conn.transaction():
            await enqueue_reachy_task(conn, task["u_id"], not_before, [row["intk_id"] for row in rows],
                                      "missed_retry", expires_at, at=not_before, not_before=not_before)
    except schedule.DoseRefused as refused:
        log.info("no meal retry for the %s slot of patient %s: dose %s refused (%s)", slot_label(slot), task["u_id"],
                 refused.intk_id, refused.detail)


async def extend_leases(conn, device_id: str) -> None:
    await conn.execute(
        f"UPDATE reachy_task SET lease_until = NOW() + INTERVAL '{LEASE_SECONDS} seconds' "
        "WHERE lease_owner = $1::uuid AND status IN ('leased','searching','in_progress')",
        device_id)


COMPANION_TASK_EXTEND_SECONDS = 600   # a companion session's task stays open this long past each heartbeat saying so


async def extend_for_companion(conn, device_id: str) -> None:
    """A companion session can outlast its task's expiry (spec 2: it stays until goodbye). While the robot's heartbeat
    says one runs, the in-progress task it holds stays open at least COMPANION_TASK_EXTEND_SECONDS longer."""
    await conn.execute(
        "UPDATE reachy_task SET expires_at = GREATEST(expires_at, "
        f"NOW() + INTERVAL '{COMPANION_TASK_EXTEND_SECONDS} seconds') "
        "WHERE lease_owner = $1::uuid AND status = 'in_progress'",
        device_id)


async def notify_robot_offline(conn, u_id: int, slot_time, now: datetime) -> int:
    """Caregiver notice through the outbox, at most once per patient per 6 hours."""
    recent = await conn.fetchval(
        "SELECT 1 FROM notification_outbox WHERE u_id = $1 AND kind = 'robot_offline' AND created_at > $2 LIMIT 1",
        u_id, now - timedelta(seconds=OFFLINE_NOTICE_WINDOW_SECONDS))
    if recent:
        return 0
    name = await conn.fetchval('SELECT name FROM "user" WHERE u_id = $1', u_id) or ""
    slot = slot_label(slot_time)
    text = (f"Reachy 目前離線，無法在 {slot} 協助 {name} 服藥。LINE 用藥提醒仍會照常發送，請確認機器人的電源與網路。\n"
            f"Reachy is offline and could not help {name} with the {slot} medicines. "
            f"The LINE reminder was still sent. Please check the robot's power and network.")
    window = int(now.timestamp() // OFFLINE_NOTICE_WINDOW_SECONDS)
    return await outbox.enqueue_to_contacts(
        conn, u_id, kind="robot_offline", priority=1, messages=[{"type": "text", "text": text}],
        dedupe_prefix=f"robot_offline:{u_id}:{window}", contact_flag="notify_missed")


async def maintenance(now=None) -> None:
    """Expire tasks past expires_at, requeue expired leases, cancel meal retries with nothing left to prompt or with a
    dose that expired meanwhile, and send robot-offline notices."""
    now = now or datetime.now(timezone.utc)
    async with get_pool().acquire() as conn, conn.transaction():
        await conn.execute(
            "UPDATE reachy_task SET status = 'expired', finished_at = $1, lease_until = NULL "
            f"WHERE status IN {_OPEN_SQL} AND expires_at <= $1",
            now)
        await conn.execute(
            "UPDATE reachy_task SET status = 'queued', lease_owner = NULL, lease_until = NULL "
            "WHERE status IN ('leased','searching','in_progress') AND lease_until < $1",
            now)
        # A meal retry whose doses were all taken, sent to family or skipped while it waited is cancelled (quick fix
        # 4): nothing is left to prompt, and the lease already skips it (_leasable_sql).
        await conn.execute(
            "UPDATE reachy_task t SET status = 'aborted', finished_at = $1, "
            "detail = COALESCE(t.detail, '{}'::jsonb) || jsonb_build_object('reason', 'nothing_to_prompt') "
            "WHERE t.status = 'queued' AND t.not_before IS NOT NULL AND NOT EXISTS (SELECT 1 FROM intake i "
            "WHERE i.intk_id = ANY(t.intk_ids) AND i.intake_stats IN ('pending','missed'))",
            now)
        # So is one with an open dose that expired before the robot leased it (review of quick fix 4: the robot was
        # busy or offline at not_before, when overdose protection last judged it). The lease would never hand it out
        # (_leasable_sql), and the missed-dose job takes the dose at once instead of waiting for its expiry.
        await conn.execute(
            "UPDATE reachy_task t SET status = 'aborted', finished_at = $1, "
            "detail = COALESCE(t.detail, '{}'::jsonb) || jsonb_build_object('reason', 'dose_expired') "
            "WHERE t.status = 'queued' AND t.not_before IS NOT NULL AND EXISTS (SELECT 1 FROM intake i "
            "WHERE i.intk_id = ANY(t.intk_ids) AND i.intake_stats IN ('pending','missed') "
            f"AND {dose_safety.protected_sql('i')} AND {dose_safety.expired_sql('i', '$1::timestamptz')})",
            now)
        # A slot has come while its task is still queued and the robot has not
        # heartbeated for a minute: tell the caregivers (once per 6 h).
        rows = await conn.fetch(
            "SELECT DISTINCT ON (t.u_id) t.u_id, t.slot_time FROM reachy_task t "
            "JOIN reachy_device d ON d.u_id = t.u_id AND d.revoked_at IS NULL "
            "WHERE t.status = 'queued' AND t.slot_time <= $1 AND t.expires_at > $1 "
            "AND (d.last_seen_at IS NULL OR d.last_seen_at < $2) "
            "ORDER BY t.u_id, t.slot_time",
            now, now - timedelta(seconds=OFFLINE_AFTER_SECONDS))
        for row in rows:
            await notify_robot_offline(conn, row["u_id"], row["slot_time"], now)


async def abort_open_tasks(conn, u_id: int, reason: str) -> None:
    await conn.execute(
        "UPDATE reachy_task SET status = 'aborted', finished_at = NOW(), lease_until = NULL, "
        "detail = COALESCE(detail, '{}'::jsonb) || jsonb_build_object('reason', $2::text) "
        "WHERE u_id = $1 AND status IN ('queued','leased','searching','in_progress')", u_id, reason)


async def revoke_devices(conn, u_id: int, reason: str) -> None:
    """Unpairing and withdrawing robot consent must stop the robot the same way."""
    await conn.execute(
        "UPDATE reachy_device SET revoked_at = NOW() WHERE u_id = $1 AND revoked_at IS NULL", u_id)
    await abort_open_tasks(conn, u_id, reason)
