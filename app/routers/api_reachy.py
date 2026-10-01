"""Robot pairing, status and settings for the signed-in patient (public port, user auth)."""

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.database import get_pool
from app.dependencies import get_consented_user
from app.jobs.missed_dose_job import _slot_key
from app.services import consent_service, reachy_tasks
from app.services.device_auth import hash_token, issue_token

router = APIRouter(prefix="/api/reachy", tags=["reachy"])

ONLINE_SECONDS = 60
MANUAL_TASK_MIN_MINUTES = 15


class SettingsPayload(BaseModel):
    auto_record: bool


class TaskPayload(BaseModel):
    intk_id: int = Field(gt=0)


def _iso(value):
    return value.isoformat() if value is not None else None


async def _status(conn, u_id: int) -> dict:
    device = await conn.fetchrow(
        "SELECT device_id, label, auto_record, last_seen_at, robot_reachable, landmark_fps "
        "FROM reachy_device WHERE u_id = $1 AND revoked_at IS NULL", u_id)
    if not device:
        return {"paired": False}
    task = await conn.fetchrow(
        "SELECT task_id, status, slot_time, finished_at FROM reachy_task WHERE u_id = $1 "
        "ORDER BY created_at DESC LIMIT 1", u_id)
    last_seen = device["last_seen_at"]
    online = last_seen is not None and datetime.now(timezone.utc) - last_seen <= timedelta(seconds=ONLINE_SECONDS)
    return {
        "paired": True, "device_id": str(device["device_id"]), "label": device["label"],
        "auto_record": bool(device["auto_record"]), "last_seen_at": _iso(last_seen), "online": online,
        "robot_reachable": device["robot_reachable"],
        "landmark_fps": float(device["landmark_fps"]) if device["landmark_fps"] is not None else None,
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
    async with get_pool().acquire() as conn:
        updated = await conn.fetchval(
            "UPDATE reachy_device SET auto_record = $2 WHERE u_id = $1 AND revoked_at IS NULL RETURNING device_id",
            user["u_id"], payload.auto_record)
        if updated is None:
            raise HTTPException(409, "robot_not_paired")
        return await _status(conn, user["u_id"])


@router.post("/tasks")
async def manual_task(payload: TaskPayload, user: dict = Depends(get_consented_user)):
    """'Use Reachy': a manual task for that dose's 5-minute slot (its pending/missed doses)."""
    u_id = user["u_id"]
    async with get_pool().acquire() as conn, conn.transaction():
        dose = await conn.fetchrow(
            "SELECT intake_time_stamp FROM intake WHERE intk_id = $1 AND u_id = $2 "
            "AND intake_stats IN ('pending','missed')", payload.intk_id, u_id)
        if not dose:
            raise HTTPException(404, "dose_not_found")
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
            "ORDER BY m.med_name, m.med_id, i.intk_id", u_id, slot_time, slot_end)
        intk_ids = [row["intk_id"] for row in rows] or [payload.intk_id]
        settings_row = await conn.fetchrow(
            "SELECT COALESCE(remind_after_minutes, 10) AS after, COALESCE(remind_after_retries, 3) AS retries "
            "FROM notification_settings WHERE u_id = $1", u_id)
        after, retries = (settings_row["after"], settings_row["retries"]) if settings_row else (10, 3)
        expires_at = max(slot_time + timedelta(minutes=after * (retries + 1)),
                         datetime.now(timezone.utc) + timedelta(minutes=MANUAL_TASK_MIN_MINUTES))
        task_id = await reachy_tasks.enqueue_reachy_task(conn, u_id, slot_time, intk_ids, "manual", expires_at)
        if task_id is None:
            raise HTTPException(403, "robot_consent_required")
        task = await conn.fetchrow(
            "SELECT task_id, status, slot_time, intk_ids FROM reachy_task WHERE task_id = $1::uuid", task_id)
    return {"task_id": str(task["task_id"]), "status": task["status"], "slot_time": _iso(task["slot_time"]),
            "intk_ids": list(task["intk_ids"])}
