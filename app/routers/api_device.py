"""Robot device API, served only on the private port (main.py enforces the port rule).

Every route authenticates a device token (device_auth.get_device) and acts only
for that device's own patient.
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, Field

from app.database import get_pool
from app.routers.api_monitor import EndPayload, LandmarkPayload, get_session
from app.services import outbox, reachy_tasks
from app.services.device_auth import get_device, get_device_for_heartbeat
from app.services.emotion_service import EmotionService
from app.services.face_recognition_service import FaceRecognitionService
from app.services.intake_repository import commit_monitored
from app.services.monitor_service import BusyOtherClient, registry

router = APIRouter(prefix="/api/device", tags=["device"])

CLIENT_TYPE = "reachy"
MAX_WAIT_SECONDS = 25


class StatusPayload(BaseModel):
    status: Literal["searching", "in_progress", "completed", "not_found", "aborted"]
    detail: dict | None = None


class ConfirmationPayload(BaseModel):
    intk_id: int = Field(gt=0)
    source: Literal["uncertain_detection", "unsupported_dose", "degraded", "auto_record_off", "patient_claim"]
    evidence: dict | None = None


class ExtraEventPayload(BaseModel):
    event_id: str
    decision: Literal["confirmed", "uncertain"]
    confidence: float = Field(ge=0, le=1)


class HeartbeatPayload(BaseModel):
    robot_reachable: bool | None = None
    landmark_fps: float | None = Field(default=None, ge=0, le=240)
    vision_fps: float | None = Field(default=None, ge=0, le=240)
    bridge_version: str | None = Field(default=None, max_length=50)
    missing_clips: int | list[str] | None = None


class MonitorStartPayload(BaseModel):
    mode: Literal["dose", "observe"]
    intk_id: int | None = Field(default=None, gt=0)
    task_id: str | None = None


def _uuid(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise HTTPException(404, "Task not found") from exc


async def _leased_task(conn, device: dict, task_id: str):
    task = await conn.fetchrow(
        "SELECT task_id, u_id, slot_time, intk_ids, status FROM reachy_task "
        "WHERE task_id = $1::uuid AND u_id = $2 AND lease_owner = $3::uuid",
        _uuid(task_id), device["u_id"], device["device_id"])
    if not task:
        raise HTTPException(404, "Task not found")
    return task


# ── Tasks ────────────────────────────────────────────────────────────────────

@router.get("/tasks/next")
async def next_task(wait: float = Query(0, ge=0, le=MAX_WAIT_SECONDS), device: dict = Depends(get_device)):
    task = await reachy_tasks.lease_next(device["u_id"], device["device_id"], wait)
    return task if task is not None else Response(status_code=204)


@router.get("/tasks/current")
async def current(device: dict = Depends(get_device)):
    task = await reachy_tasks.current_task(device["u_id"], device["device_id"])
    return task if task is not None else Response(status_code=204)


@router.post("/tasks/{task_id}/status")
async def task_status(task_id: str, payload: StatusPayload, device: dict = Depends(get_device)):
    try:
        return await reachy_tasks.set_status(device["u_id"], device["device_id"], task_id,
                                             payload.status, payload.detail)
    except reachy_tasks.TaskNotFound as exc:
        raise HTTPException(404, "Task not found") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/tasks/{task_id}/confirmation")
async def confirmation(task_id: str, payload: ConfirmationPayload, device: dict = Depends(get_device)):
    """NEEDS_CONFIRM: the dose goes to caregiver confirmation (no stock change)."""
    from app.services import dose_confirmation

    async with get_pool().acquire() as conn, conn.transaction():
        task = await _leased_task(conn, device, task_id)
        if payload.intk_id not in list(task["intk_ids"]):
            raise HTTPException(409, "Dose does not belong to this task")
        try:
            confirmation_id = await dose_confirmation.create(
                conn, u_id=device["u_id"], task_id=str(task["task_id"]), intk_ids=[payload.intk_id],
                source=payload.source, evidence=payload.evidence)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    return {"confirmation_id": str(confirmation_id)}


def _extra_event_text(name: str, slot: str) -> str:
    return (f"{name}：Reachy 在 {slot} 的藥物記錄完成後，又觀察到一次手部靠近嘴巴的動作，"
            f"這不一定是藥物，請確認藥盒。\n"
            f"{name}: Reachy observed another hand-to-mouth movement after the {slot} medicines were recorded. "
            f"This may not be a pill. Please check the pill box.")


@router.post("/tasks/{task_id}/extra-event")
async def extra_event(task_id: str, payload: ExtraEventPayload, device: dict = Depends(get_device)):
    """Uncertain evidence only: never committed, never changes stock. Idempotent per event."""
    try:
        event_id = str(uuid.UUID(payload.event_id))
    except ValueError as exc:
        raise HTTPException(422, "Invalid event id") from exc
    extra_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"extra_event:{device['u_id']}:{event_id}"))
    async with get_pool().acquire() as conn, conn.transaction():
        task = await _leased_task(conn, device, task_id)
        inserted = await conn.fetchval(
            "INSERT INTO monitor_extra_event (extra_id, u_id, task_id, detector_band, detector_score) "
            "VALUES ($1::uuid, $2, $3::uuid, $4, $5) ON CONFLICT (extra_id) DO NOTHING RETURNING extra_id",
            extra_id, device["u_id"], str(task["task_id"]), payload.decision, payload.confidence)
        notified = 0
        if inserted is not None:
            text = _extra_event_text(device["name"], reachy_tasks.slot_label(task["slot_time"]))
            notified = await outbox.enqueue_to_contacts(
                conn, device["u_id"], kind="extra_event", priority=1,
                messages=[{"type": "text", "text": text}],
                dedupe_prefix=f"extra_event:{extra_id}", contact_flag="notify_missed")
    return {"extra_id": extra_id, "recorded": inserted is not None, "notified": notified}


@router.post("/heartbeat")
async def heartbeat(payload: HeartbeatPayload, device: dict = Depends(get_device_for_heartbeat)):
    server_time = datetime.now(timezone.utc).isoformat()
    if device["revoked"] or not device["consent_current"]:
        return {"stop_all": True, "server_time": server_time}
    detail = {"vision_fps": payload.vision_fps, "bridge_version": payload.bridge_version,
              "missing_clips": payload.missing_clips}
    async with get_pool().acquire() as conn, conn.transaction():
        await conn.execute(
            "UPDATE reachy_device SET last_seen_at = NOW(), robot_reachable = $2, landmark_fps = $3, "
            "status_detail = $4::jsonb WHERE device_id = $1::uuid",
            device["device_id"], payload.robot_reachable, payload.landmark_fps, json.dumps(detail))
        await reachy_tasks.extend_leases(conn, device["device_id"])
    return {"stop_all": False, "server_time": server_time}


# ── Monitor (same payloads as /api/intake/monitor/*) ─────────────────────────

async def _reachy_commit(state, candidate: dict, method: str) -> dict:
    # Prompting attributes the event to this medication; it does not identify the pill.
    return await commit_monitored(state, candidate, "reachy_prompted")


@router.post("/monitor/start")
async def monitor_start(payload: MonitorStartPayload, device: dict = Depends(get_device)):
    if not FaceRecognitionService._available or not EmotionService._available:
        raise HTTPException(503, "Identity or seed 43 emotion model is not ready")
    if payload.mode == "dose" and (payload.intk_id is None or payload.task_id is None):
        raise HTTPException(422, "A dose session needs intk_id and task_id")
    auto_commit = False
    intk_id = payload.intk_id if payload.mode == "dose" else None
    async with get_pool().acquire() as conn:
        if payload.task_id is not None:
            task = await _leased_task(conn, device, payload.task_id)
            if task["status"] not in ("leased", "searching", "in_progress"):
                raise HTTPException(409, "Task is no longer active")
            if intk_id is not None and intk_id not in list(task["intk_ids"]):
                raise HTTPException(409, "Dose does not belong to this task")
        if intk_id is not None:
            row = await conn.fetchrow(
                "SELECT m.dose_form, m.units_per_dose FROM intake i JOIN medication m ON m.med_id=i.med_id "
                "WHERE i.intk_id=$1 AND i.u_id=$2 AND i.intake_stats IN ('pending','missed') "
                "AND m.pills_remaining>0 AND m.is_active=TRUE",
                intk_id, device["u_id"])
            if not row:
                raise HTTPException(409, "Dose is unavailable or does not belong to this account")
            # Server policy (D2): the bridge's view is advisory.
            auto_commit = device["auto_record"] and reachy_tasks.is_supported(row["dose_form"], row["units_per_dose"])
    try:
        state = await registry.replace(device["u_id"], intk_id, device["face_label"], device["name"],
                                       mode=payload.mode, client_type=CLIENT_TYPE, auto_commit=auto_commit)
    except BusyOtherClient as exc:
        raise HTTPException(409, "busy_other_client") from exc
    return state.public()


@router.post("/monitor/landmarks")
async def monitor_landmarks(payload: LandmarkPayload, device: dict = Depends(get_device)):
    state = get_session(device, payload.session_id, payload.generation, CLIENT_TYPE)
    try:
        return await registry.landmarks(state, payload.model_dump())
    except (ValueError, TypeError, IndexError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/monitor/vision")
async def monitor_vision(session_id: str = Form(...), generation: str = Form(...),
                         frame_seq: int = Form(...), file: UploadFile = File(...),
                         device: dict = Depends(get_device)):
    state = get_session(device, session_id, generation, CLIENT_TYPE)
    data = await file.read(1_000_001)
    if len(data) > 1_000_000:
        raise HTTPException(413, "Camera frame is too large")
    try:
        return await registry.vision(state, frame_seq, data, _reachy_commit)
    except (ValueError, TypeError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/monitor/end")
async def monitor_end(payload: EndPayload, device: dict = Depends(get_device)):
    state = get_session(device, payload.session_id, payload.generation, CLIENT_TYPE)
    await registry.end(state)
    return {"success": True}
