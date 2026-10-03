"""Leased robot tasks: one open task per patient and 5-minute slot.

queued -> leased -> searching -> in_progress -> completed | not_found | aborted
(leased -> aborted, searching -> not_found | aborted). The minute job returns
expired leases to queued, or marks tasks expired once past expires_at.
"""

import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.config import MEDCARE_TIMEZONE
from app.database import get_pool
from app.services import consent_service, dose_safety, outbox

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


async def task_payload(conn, task) -> dict:
    """Task as the bridge sees it. Doses are re-read on every call (restart recovery)."""
    doses = await conn.fetch(
        "SELECT i.intk_id, m.med_id, m.med_name, m.pill_description, m.dose_form, m.units_per_dose, i.intake_stats "
        "FROM intake i JOIN medication m ON m.med_id = i.med_id "
        "WHERE i.u_id = $1 AND i.intk_id = ANY($2::int[]) ORDER BY m.med_name, m.med_id, i.intk_id",
        task["u_id"], list(task["intk_ids"]))
    head = await conn.fetchrow(
        'SELECT u.name, d.auto_record FROM "user" u '
        "LEFT JOIN reachy_device d ON d.u_id = u.u_id AND d.revoked_at IS NULL WHERE u.u_id = $1",
        task["u_id"])
    consent = await consent_service.fetch_state(conn, task["u_id"])
    return {
        "task_id": str(task["task_id"]), "slot_time": _iso(task["slot_time"]), "reason": task["reason"],
        "attempt": task["attempt"], "status": task["status"], "expires_at": _iso(task["expires_at"]),
        "patient_name": head["name"] if head else None,
        "auto_record": bool(head and head["auto_record"]),
        # The robot may listen (speech-to-text on the robot) only while this consent is current.
        "microphone": consent_service.is_current(consent, "robot_microphone"),
        # A check-in conversation needs every check-in scope (notice: check-ins only with safety alerts on).
        "checkin": checkin_allowed(consent),
        "doses": [{
            "intk_id": d["intk_id"], "med_id": d["med_id"], "med_name": d["med_name"],
            "pill_description": d["pill_description"], "dose_form": d["dose_form"],
            "units_per_dose": float(d["units_per_dose"]) if d["units_per_dose"] is not None else None,
            "intake_stats": d["intake_stats"], "supported": is_supported(d["dose_form"], d["units_per_dose"]),
        } for d in doses],
    }


async def enqueue_reachy_task(conn, u_id: int, slot_time, intk_ids: list[int], reason: str, expires_at,
                              at=None) -> str | None:
    """Open (or re-arm a queued) task for this slot inside the caller's transaction.

    Returns the open task id, or None when the patient has no active device or
    no current robot camera consent. A task already leased/searching/in_progress
    is never re-armed or reset. Raises a schedule.DoseRefused when overdose
    protection refuses a listed dose (dose_safety.check, judged at `at`, default
    now): the robot must never start a dose hours early, a second dose, or a
    missed one. Callers that picked the doses at a moment of their own pass it.
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
        "INSERT INTO reachy_task (task_id, u_id, slot_time, intk_ids, reason, expires_at) "
        "VALUES ($1::uuid, $2, $3, $4::int[], $5, $6) ON CONFLICT DO NOTHING RETURNING task_id",
        str(uuid.uuid4()), u_id, slot_time, list(intk_ids), reason, expires_at)
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
    return str(task_id) if task_id is not None else None


async def _lease_once(u_id: int, device_id: str) -> dict | None:
    # A task holding an open dose that is not due yet stays queued until it is, and one holding an open dose that has
    # expired is not handed out (enqueue refuses such tasks; this also covers any queued before the rules, or that
    # lapsed while queued). Doses already taken, skipped or waiting for family don't count: expiry is a matter of
    # time alone, and a taken dose past its halfway point must not keep the slot's other doses from the robot.
    # Overdose protection's switch decides, as in dose_safety.startable_sql; monitor/start checks the rest.
    async with get_pool().acquire() as conn, conn.transaction():
        task = await conn.fetchrow(
            "UPDATE reachy_task SET status = 'leased', lease_owner = $2::uuid, "
            f"lease_until = NOW() + INTERVAL '{LEASE_SECONDS} seconds' "
            "WHERE task_id = (SELECT t.task_id FROM reachy_task t WHERE t.u_id = $1 AND t.status = 'queued' "
            "AND t.expires_at > NOW() AND NOT EXISTS (SELECT 1 FROM intake i WHERE i.intk_id = ANY(t.intk_ids) "
            f"AND i.intake_stats IN ('pending','missed') AND NOT ({dose_safety.startable_sql('i', 'NOW()')})) "
            "ORDER BY t.slot_time, t.created_at LIMIT 1 FOR UPDATE SKIP LOCKED) "
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
            task = await conn.fetchrow(
                "UPDATE reachy_task SET status = $2, detail = COALESCE($3::jsonb, detail), "
                "finished_at = NOW(), lease_until = NULL WHERE task_id = $1::uuid RETURNING *",
                task_id, status, detail_json)
        else:
            task = await conn.fetchrow(
                "UPDATE reachy_task SET status = $2, detail = COALESCE($3::jsonb, detail), "
                f"lease_until = NOW() + INTERVAL '{LEASE_SECONDS} seconds' WHERE task_id = $1::uuid RETURNING *",
                task_id, status, detail_json)
        if status == "aborted" and (detail or {}).get("reason") == "robot_offline":
            await notify_robot_offline(conn, u_id, task["slot_time"], datetime.now(timezone.utc))
        return await task_payload(conn, task)


async def extend_leases(conn, device_id: str) -> None:
    await conn.execute(
        f"UPDATE reachy_task SET lease_until = NOW() + INTERVAL '{LEASE_SECONDS} seconds' "
        "WHERE lease_owner = $1::uuid AND status IN ('leased','searching','in_progress')",
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
    """Expire tasks past expires_at, requeue expired leases, and send robot-offline notices."""
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
