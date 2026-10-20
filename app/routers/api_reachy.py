"""Robot pairing, status and settings for the signed-in patient (public port, user auth)."""

import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, StrictBool

from app import config
from app.database import get_pool
from app.dependencies import get_consented_user
from app.jobs.missed_dose_job import _slot_key
from app.services import consent_service, dose_confirmation, dose_safety, memory, reachy_tasks, schedule, voice_lines
from app.services.device_auth import hash_token, issue_token

router = APIRouter(prefix="/api/reachy", tags=["reachy"])

ONLINE_SECONDS = 60

# Quick fix 6 (20 Oct 2026): the Reachy card's "Demo dose: Acetal 500mg" button, for testing the intake flow. The demo
# medicine is the patient's medication whose prescription_meta says {"demo": "acetal"}, made on the first press: taken
# after meals, with no dose time, so no dose is ever generated for it (each press makes its one dose).
DEMO_MEDICINE = "acetal"
DEMO_NAME = "Acetal 500mg"
DEMO_INSTRUCTIONS = "示範藥物（飯後服用），用來測試服藥流程"   # 飯後, and none of 空腹, 飯前, 餐前 (reachy_tasks.after_meal)
DEMO_SCHEDULE = {"morning": False, "noon": False, "night": False, "bedtime": False,
                 "after_meals": True, "before_meals": False, "custom_times": []}
DEMO_STOCK = Decimal(30)       # its stock when made, and when topped up
DEMO_STOCK_LOW = Decimal(10)   # topped up below this: an as-needed medicine's refill alert comes at 7 doses or fewer


class SettingsPayload(BaseModel):
    """The Reachy card's settings. A field left out (or null) keeps its value."""
    auto_record: bool | None = None
    call_name: str | None = Field(default=None, max_length=memory.CALL_NAME_MAX)   # "" = the registered name
    live_voice: bool | None = None
    # Quick fix 5: Reachy comes back 20 minutes after "not eaten yet" (reachy_tasks._queue_meal_retry). Only true or
    # false: "true", 1 and the like are a 422, so a client's slip never switches it.
    meal_retry: StrictBool | None = None


class TaskPayload(BaseModel):
    intk_id: int = Field(gt=0)


def _iso(value):
    return value.isoformat() if value is not None else None


def _camera(status_detail) -> dict | None:
    """The robot's camera as its last heartbeat reported it, or None (robots older than 0.7.0 send none).
    asyncpg hands JSONB back as text."""
    if status_detail is None:
        return None
    detail = json.loads(status_detail) if isinstance(status_detail, str) else status_detail
    camera = detail.get("camera") if isinstance(detail, dict) else None
    return camera if isinstance(camera, dict) else None


async def _status(conn, u_id: int) -> dict:
    device = await conn.fetchrow(
        "SELECT device_id, label, auto_record, live_voice, last_seen_at, robot_reachable, landmark_fps, status_detail "
        "FROM reachy_device WHERE u_id = $1 AND revoked_at IS NULL", u_id)
    if not device:
        return {"paired": False}
    task = await conn.fetchrow(
        "SELECT task_id, status, slot_time, finished_at FROM reachy_task WHERE u_id = $1 "
        "ORDER BY created_at DESC LIMIT 1", u_id)
    names = await conn.fetchrow('SELECT reachy_call_name, name, reachy_meal_retry FROM "user" WHERE u_id = $1', u_id)
    stored = names["reachy_call_name"] if names else None
    last_seen = device["last_seen_at"]
    online = last_seen is not None and datetime.now(timezone.utc) - last_seen <= timedelta(seconds=ONLINE_SECONDS)
    # Family alerts (safety, confirmations, missed doses) go only to verified non-patient contacts on LINE.
    alert_contacts = await conn.fetchval(
        "SELECT COUNT(*) FROM family_contacts WHERE u_id = $1 AND verified = TRUE AND line_id IS NOT NULL "
        "AND relationship IS DISTINCT FROM 'user'", u_id)
    return {
        "alert_contacts": int(alert_contacts or 0),
        "paired": True, "device_id": str(device["device_id"]), "label": device["label"],
        "auto_record": bool(device["auto_record"]), "last_seen_at": _iso(last_seen), "online": online,
        # What Reachy calls the patient without a memory 'name' entry; custom: saved on this card.
        "call_name": memory.call_name_of(stored, names["name"] if names else None),
        "call_name_custom": stored is not None,
        "live_voice": bool(device["live_voice"]), "live_voice_test": config.LIVE_VOICE_TEST,
        # Reachy comes back 20 minutes after "not eaten yet" (quick fix 5; off by default).
        "meal_retry": bool(names and names["reachy_meal_retry"]),
        "robot_reachable": device["robot_reachable"],
        "landmark_fps": float(device["landmark_fps"]) if device["landmark_fps"] is not None else None,
        "camera": _camera(device["status_detail"]),
        "last_task": {"task_id": str(task["task_id"]), "status": task["status"],
                      "slot_time": _iso(task["slot_time"]), "finished_at": _iso(task["finished_at"])}
        if task else None,
    }


@router.post("/pairing")
async def pair(user: dict = Depends(get_consented_user)):
    """Create a device for this patient (replacing any active one); the token is shown once."""
    state = await consent_service.get_state(user["u_id"])
    if not consent_service.is_current(state, "robot_camera"):
        raise HTTPException(403, "robot_consent_required")
    device_id = str(uuid.uuid4())
    token = issue_token(device_id, user["u_id"])
    async with get_pool().acquire() as conn, conn.transaction():
        await conn.execute(
            "UPDATE reachy_device SET revoked_at = NOW() WHERE u_id = $1 AND revoked_at IS NULL", user["u_id"])
        await conn.execute(
            "INSERT INTO reachy_device (device_id, u_id, token_hash) VALUES ($1::uuid, $2, $3)",
            device_id, user["u_id"], hash_token(token))
    return {"device_id": device_id, "token": token}


@router.delete("/pairing")
async def unpair(user: dict = Depends(get_consented_user)):
    async with get_pool().acquire() as conn, conn.transaction():
        await reachy_tasks.revoke_devices(conn, user["u_id"], "unpaired")
    return {"paired": False}


@router.get("/status")
async def status(user: dict = Depends(get_consented_user)):
    async with get_pool().acquire() as conn:
        return await _status(conn, user["u_id"])


@router.patch("/settings")
async def settings(payload: SettingsPayload, user: dict = Depends(get_consented_user)):
    """A call name follows the rules of the patient's memory 'name' entry (422 invalid_call_name); an empty one goes
    back to the registered name (NULL). A changed name renders the opening ahead in the live voice (voice_lines).
    Switching meal_retry off cancels the meal retry still waiting, in the same transaction (quick fix 5)."""
    call_name = None
    if payload.call_name is not None and payload.call_name.strip():
        call_name = memory.clean_call_name(payload.call_name)
        if call_name is None:
            raise HTTPException(422, "invalid_call_name")
    async with get_pool().acquire() as conn, conn.transaction():
        updated = await conn.fetchval(
            "UPDATE reachy_device SET auto_record = COALESCE($2, auto_record), live_voice = COALESCE($3, live_voice) "
            "WHERE u_id = $1 AND revoked_at IS NULL RETURNING device_id",
            user["u_id"], payload.auto_record, payload.live_voice)
        if updated is None:
            raise HTTPException(409, "robot_not_paired")
        if payload.call_name is not None:
            await conn.execute('UPDATE "user" SET reachy_call_name = $2 WHERE u_id = $1', user["u_id"], call_name)
        if payload.meal_retry is not None:
            await conn.execute('UPDATE "user" SET reachy_meal_retry = $2 WHERE u_id = $1', user["u_id"],
                               payload.meal_retry)
            if not payload.meal_retry:
                await reachy_tasks.cancel_meal_retries(conn, user["u_id"])
        status = await _status(conn, user["u_id"])
    if payload.call_name is not None:   # after the commit: the render reads the new name on its own connection
        voice_lines.render_ahead_for_patient(user["u_id"])
    return status


@router.post("/checkin")
async def checkin_task(user: dict = Depends(get_consented_user)):
    """'Talk to Reachy now': a conversation-only task; the robot finds the patient, then chats."""
    u_id = user["u_id"]
    if not reachy_tasks.checkin_allowed(await consent_service.get_state(u_id)):
        raise HTTPException(403, "checkin_consent_required")
    now = datetime.now(timezone.utc)
    async with get_pool().acquire() as conn, conn.transaction():
        device = await conn.fetchval(
            "SELECT device_id FROM reachy_device WHERE u_id = $1 AND revoked_at IS NULL", u_id)
        if device is None:
            raise HTTPException(409, "robot_not_paired")
        task_id = await reachy_tasks.enqueue_reachy_task(
            conn, u_id, now, [], "checkin", now + timedelta(minutes=reachy_tasks.MANUAL_TASK_MIN_MINUTES))
        if task_id is None:
            raise HTTPException(403, "robot_consent_required")
    return {"task_id": task_id, "status": "queued", "slot_time": _iso(now)}


@router.post("/tasks")
async def manual_task(payload: TaskPayload, user: dict = Depends(get_consented_user)):
    """'Use Reachy' (and the Reachy card's test alert): a manual task for that dose's 5-minute slot (its
    pending/missed doses that overdose protection lets start now). A dose it refuses gets 409 with the reason
    (dose_not_due_yet with its time, dose_too_soon, daily_max_reached, dose_expired) and the patient's sentence."""
    u_id = user["u_id"]
    async with get_pool().acquire() as conn, conn.transaction():
        dose = await conn.fetchrow(
            "SELECT intake_time_stamp FROM intake "
            "WHERE intk_id = $1 AND u_id = $2 AND intake_stats IN ('pending','missed')", payload.intk_id, u_id)
        if not dose:
            raise HTTPException(404, "dose_not_found")
        now = schedule.current_time()
        await dose_safety.check(conn, u_id, [payload.intk_id], at=now)
        device = await conn.fetchval(
            "SELECT device_id FROM reachy_device WHERE u_id = $1 AND revoked_at IS NULL", u_id)
        if device is None:
            raise HTTPException(409, "robot_not_paired")
        slot_time = _slot_key(dose["intake_time_stamp"])
        slot_end = slot_time + timedelta(minutes=5)
        rows = await conn.fetch(
            "SELECT i.intk_id FROM intake i JOIN medication m ON m.med_id = i.med_id "
            "WHERE i.u_id = $1 AND i.intake_stats IN ('pending','missed') "
            "AND i.intake_time_stamp >= $2 AND i.intake_time_stamp < $3 "
            f"AND {dose_safety.startable_sql('i', '$4::timestamptz')} "
            "ORDER BY m.med_name, m.med_id, i.intk_id", u_id, slot_time, slot_end, now)
        # The slot's other doses come along only when nothing refuses them (a gap or a daily maximum of their own).
        allowed = set(await dose_safety.allowed(conn, u_id, [row["intk_id"] for row in rows], at=now))
        intk_ids = [row["intk_id"] for row in rows if row["intk_id"] in allowed or row["intk_id"] == payload.intk_id]
        intk_ids = intk_ids or [payload.intk_id]
        expires_at = await reachy_tasks.manual_expires_at(conn, u_id, slot_time, datetime.now(timezone.utc))
        task_id = await reachy_tasks.enqueue_reachy_task(conn, u_id, slot_time, intk_ids, "manual", expires_at,
                                                         at=now)
        if task_id is None:
            raise HTTPException(403, "robot_consent_required")
        task = await conn.fetchrow(
            "SELECT task_id, status, slot_time, intk_ids FROM reachy_task WHERE task_id = $1::uuid", task_id)
    return {"task_id": str(task["task_id"]), "status": task["status"], "slot_time": _iso(task["slot_time"]),
            "intk_ids": list(task["intk_ids"])}


async def _clear_demo_doses(conn, u_id: int, intk_ids: list[int]) -> None:
    """The demo medicine's earlier doses go before a press makes a fresh one, so a press always works (a demo dose
    taken or waiting for family would refuse the next one as too soon under overdose protection): their open robot
    tasks are aborted (reason demo_reset), their open family requests expired as one expires on its own (another dose
    in such a request goes back to its previous status), then their rows are deleted, and their monitor_event and
    dose_emotion rows with them (ON DELETE CASCADE). A family button pressed later on such a request is answered that
    it has expired."""
    if not intk_ids:
        return
    await conn.execute(
        "UPDATE reachy_task SET status = 'aborted', finished_at = NOW(), lease_until = NULL, "
        "detail = COALESCE(detail, '{}'::jsonb) || jsonb_build_object('reason', 'demo_reset') "
        "WHERE u_id = $1 AND status IN ('queued','leased','searching','in_progress') AND intk_ids && $2::int[]",
        u_id, intk_ids)
    now = datetime.now(timezone.utc)
    for row in await conn.fetch(
            "SELECT confirmation_id FROM dose_confirmation WHERE u_id = $1 AND resolution IS NULL "
            "AND intk_ids && $2::int[] ORDER BY created_at", u_id, intk_ids):
        await dose_confirmation._expire(conn, str(row["confirmation_id"]), now)
    await conn.execute("DELETE FROM intake WHERE u_id = $1 AND intk_id = ANY($2::int[])", u_id, intk_ids)


@router.post("/demo-dose")
async def demo_dose(user: dict = Depends(get_consented_user)):
    """Quick fix 6, 'Demo dose: Acetal 500mg': one press, and Reachy comes at once for a dose of the demo medicine.

    One transaction, all or nothing. The robot must be paired (409 robot_not_paired). The demo medicine is made, or
    made active again and topped up to DEMO_STOCK when below DEMO_STOCK_LOW; a medicine of the same name without the
    demo flag is never touched. Its earlier doses are cleared (_clear_demo_doses). A fresh dose is made as Take Now
    makes an ad-hoc one (never on a whole minute, so it never expires: dose_safety.is_ad_hoc), with reminder_sent set
    (Reachy is sent now; no "upcoming" reminder), and overdose protection judges it as for 'Use Reachy'. Reachy is
    sent as POST /api/reachy/tasks sends it: a manual task holding only that dose, for its 5-minute slot (403
    robot_consent_required without the robot's consent; 409 slot_busy when another task already holds that slot,
    which would not bring Reachy for this dose). From there on it is the normal flow of a dose."""
    u_id = user["u_id"]
    async with get_pool().acquire() as conn, conn.transaction():
        device = await conn.fetchval(
            "SELECT device_id FROM reachy_device WHERE u_id = $1 AND revoked_at IS NULL", u_id)
        if device is None:
            raise HTTPException(409, "robot_not_paired")
        # Two presses at once (two tabs) wait for each other, so they never make two demo medicines.
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext('reachy_demo_dose'), $1)", u_id)
        med_id = await conn.fetchval(
            "SELECT med_id FROM medication WHERE u_id = $1 AND prescription_meta->>'demo' = $2 "
            "ORDER BY med_id LIMIT 1", u_id, DEMO_MEDICINE)
        if med_id is None:
            med_id = await conn.fetchval(
                "INSERT INTO medication (u_id, med_name, schedule_time, instructions, pills_remaining, is_active, "
                "prescription_meta, dose_form, units_per_dose) "
                "VALUES ($1, $2, $3::jsonb, $4, $5, TRUE, $6::jsonb, 'solid_oral', 1) RETURNING med_id",
                u_id, DEMO_NAME, json.dumps(DEMO_SCHEDULE), DEMO_INSTRUCTIONS, DEMO_STOCK,
                json.dumps({"demo": DEMO_MEDICINE}))
        else:
            await _clear_demo_doses(conn, u_id, [row["intk_id"] for row in await conn.fetch(
                "SELECT intk_id FROM intake WHERE u_id = $1 AND med_id = $2 ORDER BY intk_id", u_id, med_id)])
            await conn.execute(
                "UPDATE medication SET is_active = TRUE, archived_at = NULL, "
                "pills_remaining = CASE WHEN pills_remaining < $2 THEN $3 ELSE pills_remaining END WHERE med_id = $1",
                med_id, DEMO_STOCK_LOW, DEMO_STOCK)
        now = schedule.current_time()
        if (now.second, now.microsecond) == (0, 0):
            now += timedelta(microseconds=1)
        intk_id = await conn.fetchval(
            "INSERT INTO intake (u_id, med_id, intake_time_stamp, intake_stats, notify_stats, reminder_sent) "
            "VALUES ($1, $2, $3, 'pending', 'pending', TRUE) RETURNING intk_id", u_id, med_id, now)
        await dose_safety.check(conn, u_id, [intk_id], at=now)
        slot_time = _slot_key(now)
        expires_at = await reachy_tasks.manual_expires_at(conn, u_id, slot_time, now)
        task_id = await reachy_tasks.enqueue_reachy_task(conn, u_id, slot_time, [intk_id], "manual", expires_at,
                                                         at=now)
        if task_id is None:
            raise HTTPException(403, "robot_consent_required")
        task = await conn.fetchrow(
            "SELECT task_id, status, slot_time, intk_ids FROM reachy_task WHERE task_id = $1::uuid", task_id)
        if intk_id not in list(task["intk_ids"]):
            raise HTTPException(409, "slot_busy")
    return {"task_id": str(task["task_id"]), "status": task["status"], "intk_id": intk_id, "med_id": med_id,
            "med_name": DEMO_NAME}
