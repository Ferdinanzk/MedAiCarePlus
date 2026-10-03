"""Robot device API, served only on the private port (main.py enforces the port rule).

Every route authenticates a device token (device_auth.get_device) and acts only
for that device's own patient.
"""

import asyncio
import io
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, Field, ValidationError, field_validator

from app import config
from app.database import get_pool
from app.routers.api_monitor import EndPayload, LandmarkPayload, get_session
from app.services import after_chat, consent_service, conversation, outbox, reachy_tasks
from app.services.device_auth import get_device, get_device_for_heartbeat
from app.services.face_recognition_service import FaceRecognitionService
from app.services.intake_repository import commit_monitored
from app.services.landmark_service import LandmarkService
from app.services.monitor_service import LABELS, BusyOtherClient, registry

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/device", tags=["device"])

CLIENT_TYPE = "reachy"
MAX_WAIT_SECONDS = 25
MAX_FRAME_BYTES = 1_000_000
FRAME_VISION_INTERVAL = 0.5   # identity + emotion on a streamed frame, as often as the server re-checks identity


class EmotionReport(BaseModel):
    """Emotion the robot scored on its own camera for one face of this landmark packet."""
    face_index: int = Field(ge=0, le=3)
    probabilities: dict[str, float]

    @field_validator("probabilities")
    @classmethod
    def _seven_probabilities(cls, value: dict[str, float]) -> dict[str, float]:
        if set(value) != set(LABELS) or any(not 0.0 <= p <= 1.0 for p in value.values()):
            raise ValueError(f"probabilities must give {', '.join(LABELS)}, each between 0 and 1")
        if abs(sum(value.values()) - 1.0) > 0.01:
            raise ValueError("probabilities must sum to 1")
        return value


class DeviceLandmarkPayload(LandmarkPayload):
    emotion: EmotionReport | None = None


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


# ── Check-in conversations ───────────────────────────────────────────────────

class ConversationStartPayload(BaseModel):
    task_id: str
    language: Literal["zh-TW", "en"] = "zh-TW"


class ConversationTurnPayload(BaseModel):
    text: str = Field(min_length=1, max_length=conversation.MAX_TEXT)


class ConversationEndPayload(BaseModel):
    reason: Literal["finished", "goodbye", "silence", "risk", "patient_left", "stopped", "error"] = "finished"


_background: set = set()


async def _checkin_consent(device: dict) -> None:
    if not reachy_tasks.checkin_allowed(await consent_service.get_state(device["u_id"])):
        raise HTTPException(403, "checkin_consent_required")


async def _own_conversation(conn, device: dict, conversation_id: str):
    try:
        conversation_id = str(uuid.UUID(conversation_id))
    except ValueError as exc:
        raise HTTPException(404, "Conversation not found") from exc
    row = await conn.fetchrow(
        "SELECT conversation_id, language, ended_at, risk_flag FROM conversation "
        "WHERE conversation_id = $1::uuid AND u_id = $2 FOR UPDATE", conversation_id, device["u_id"])
    if not row:
        raise HTTPException(404, "Conversation not found")
    return row


async def _history(conn, conversation_id) -> list[dict]:
    rows = await conn.fetch(
        "SELECT role, text FROM conversation_turn WHERE conversation_id = $1::uuid ORDER BY turn_id",
        str(conversation_id))
    return [dict(row) for row in rows]


async def _add_turn(conn, conversation_id, u_id: int, role: str, text: str, flagged: bool = False) -> int:
    return await conn.fetchval(
        "INSERT INTO conversation_turn (conversation_id, u_id, role, text, flagged) "
        "VALUES ($1::uuid, $2, $3, $4, $5) RETURNING turn_id",
        str(conversation_id), u_id, role, text, flagged)


def _spoken(reply: str, language: str, end: bool, risk: bool = False, conversation_id=None) -> dict:
    body = {"reply": reply, "speech_text": conversation.speech_text(reply, language), "end": end, "risk": risk}
    if conversation_id is not None:
        body["conversation_id"] = str(conversation_id)
    return body


@router.post("/conversations")
async def conversation_start(payload: ConversationStartPayload, device: dict = Depends(get_device)):
    """Open a check-in conversation for a task leased by this robot; returns the opening line."""
    await _checkin_consent(device)
    language = conversation.language_of(payload.language)
    conversation_id = str(uuid.uuid4())
    opening = conversation.OPENING[language]
    async with get_pool().acquire() as conn, conn.transaction():
        task = await _leased_task(conn, device, payload.task_id)
        await conn.execute(
            "INSERT INTO conversation (conversation_id, u_id, task_id, language, model) "
            "VALUES ($1::uuid, $2, $3::uuid, $4, $5)",
            conversation_id, device["u_id"], str(task["task_id"]), language, config.LLM_MODEL or "openrouter/free")
        await _add_turn(conn, conversation_id, device["u_id"], "reachy", opening)
    return _spoken(opening, language, end=False, conversation_id=conversation_id)


@router.post("/conversations/{conversation_id}/turn")
async def conversation_turn(conversation_id: str, payload: ConversationTurnPayload, device: dict = Depends(get_device)):
    """The patient's words (already text, from the robot) in, Reachy's reply out."""
    await _checkin_consent(device)
    text = payload.text.strip()
    if not text:
        raise HTTPException(422, "Empty turn")
    risk = conversation.screen(text)
    async with get_pool().acquire() as conn, conn.transaction():
        row = await _own_conversation(conn, device, conversation_id)
        if row["ended_at"] is not None:
            raise HTTPException(409, "Conversation has ended")
        language = row["language"]
        turn_id = await _add_turn(conn, row["conversation_id"], device["u_id"], "patient", text, bool(risk))
        history = await _history(conn, row["conversation_id"])
        if risk:
            # Fixed help-line reply; the words never go to the model. Every verified contact is told,
            # whatever their other alert settings (robot notice §6).
            await conn.execute("UPDATE conversation SET risk_flag = TRUE WHERE conversation_id = $1::uuid",
                               str(row["conversation_id"]))
            notified = await outbox.enqueue_to_contacts(
                conn, device["u_id"], kind="safety_alert", priority=0,
                messages=[{"type": "text", "text": conversation.safety_alert_text(device["name"], text, risk)}],
                dedupe_prefix=f"safety_alert:{row['conversation_id']}:{turn_id}", contact_flag=None)
            if not notified:
                # Nobody to tell (no verified *family* contact; the patient's own LINE is not one): never silent.
                log.warning("safety alert for user %s reached no family contact", device["u_id"])
                await conn.execute(
                    "INSERT INTO notification (u_id, category, type, message) VALUES ($1, 'family', $2, $3)",
                    device["u_id"], "safety_alert_undelivered",
                    "A check-in safety alert could not be sent: no verified family contact on LINE.")
    patient_turns = sum(1 for turn in history if turn["role"] == "patient")
    if risk:
        reply, end = conversation.HELPLINE[language], True
    elif conversation.wants_to_end(text) or patient_turns >= conversation.MAX_PATIENT_TURNS:
        reply, end = conversation.CLOSING[language], True
    else:
        # No connection is held while the (possibly slow, free-tier) model answers.
        reply, end = await conversation.reply(history, language), False
    async with get_pool().acquire() as conn:
        await _add_turn(conn, row["conversation_id"], device["u_id"], "reachy", reply)
    return _spoken(reply, language, end=end, risk=bool(risk))


@router.post("/conversations/{conversation_id}/end")
async def conversation_end(conversation_id: str, payload: ConversationEndPayload, device: dict = Depends(get_device)):
    """Close the conversation (idempotent); the summary and mood are written in the background."""
    async with get_pool().acquire() as conn, conn.transaction():
        row = await _own_conversation(conn, device, conversation_id)
        if row["ended_at"] is not None:
            return {"ended": True}
        await conn.execute(
            "UPDATE conversation SET ended_at = NOW(), end_reason = $2, after_chat_state = 'pending' "
            "WHERE conversation_id = $1::uuid", str(row["conversation_id"]), payload.reason)
    task = asyncio.create_task(after_chat.process(str(row["conversation_id"]), device["u_id"]))
    _background.add(task)
    task.add_done_callback(_background.discard)
    return {"ended": True}


@router.post("/heartbeat")
async def heartbeat(payload: HeartbeatPayload, device: dict = Depends(get_device_for_heartbeat)):
    server_time = datetime.now(timezone.utc).isoformat()
    if device["revoked"] or not device["consent_current"]:
        return {"stop_all": True, "microphone": False, "server_time": server_time}
    detail = {"vision_fps": payload.vision_fps, "bridge_version": payload.bridge_version,
              "missing_clips": payload.missing_clips}
    async with get_pool().acquire() as conn, conn.transaction():
        await conn.execute(
            "UPDATE reachy_device SET last_seen_at = NOW(), robot_reachable = $2, landmark_fps = $3, "
            "status_detail = $4::jsonb WHERE device_id = $1::uuid",
            device["device_id"], payload.robot_reachable, payload.landmark_fps, json.dumps(detail))
        await reachy_tasks.extend_leases(conn, device["device_id"])
    microphone = consent_service.is_current(await consent_service.get_state(device["u_id"]), "robot_microphone")
    return {"stop_all": False, "microphone": microphone, "server_time": server_time}


# ── Monitor (same payloads as /api/intake/monitor/*) ─────────────────────────

async def _reachy_commit(state, candidate: dict, method: str) -> dict:
    # Prompting attributes the event to this medication; it does not identify the pill.
    return await commit_monitored(state, candidate, "reachy_prompted")


@router.post("/monitor/start")
async def monitor_start(payload: MonitorStartPayload, device: dict = Depends(get_device)):
    # Robots score emotion themselves; the server only needs its identity model.
    if not FaceRecognitionService._available:
        raise HTTPException(503, "Identity model is not ready")
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
                "AND m.pills_remaining>=m.units_per_dose AND m.is_active=TRUE",
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
async def monitor_landmarks(payload: DeviceLandmarkPayload, device: dict = Depends(get_device)):
    state = get_session(device, payload.session_id, payload.generation, CLIENT_TYPE)
    packet = payload.model_dump()
    if packet["emotion"] is not None and packet["emotion"]["face_index"] >= len(packet["faces"]):
        raise HTTPException(422, "emotion.face_index does not name a face in this packet")
    try:
        return await registry.landmarks(state, packet)
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


def _decode_rgb(data: bytes):
    from PIL import Image, UnidentifiedImageError

    try:
        image = Image.open(io.BytesIO(data))
        if image.width > 1920 or image.height > 1080:
            raise ValueError("Camera frame is too large")
        return image.convert("RGB")
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("Invalid camera frame") from exc


async def _frame_vision(state, frame_seq: int, jpeg: bytes) -> None:
    try:
        await registry.vision(state, frame_seq, jpeg, _reachy_commit)
    except Exception:   # a failed identity check only delays verification; the next frame retries
        log.exception("identity check on streamed frame %s failed", frame_seq)


def _maybe_start_vision(state, frame_seq: int, jpeg: bytes) -> None:
    """Identity/emotion every FRAME_VISION_INTERVAL, and at once while a candidate waits for a fresh face match."""
    if state.vision_task is not None and not state.vision_task.done():
        return
    candidate = state.candidate
    waiting = candidate is not None and not candidate.get("ready", False)
    now = time.monotonic()
    if not waiting and now - state.last_vision_started < FRAME_VISION_INTERVAL:
        return
    state.last_vision_started = now
    state.vision_task = asyncio.create_task(_frame_vision(state, frame_seq, jpeg))


@router.post("/monitor/frame")
async def monitor_frame(session_id: str = Form(...), generation: str = Form(...),
                        frame_seq: int = Form(..., gt=0), timestamp: float = Form(...),
                        file: UploadFile = File(...), device: dict = Depends(get_device)):
    """One camera frame (JPEG, `timestamp` = capture time in seconds on the robot's clock).

    The server computes the landmarks the robot used to compute itself, then feeds the same monitor
    session as /monitor/landmarks and /monitor/vision do, so recording policy is unchanged.
    """
    if not LandmarkService._available:
        raise HTTPException(503, "Landmark models are not ready")
    state = get_session(device, session_id, generation, CLIENT_TYPE)
    data = await file.read(MAX_FRAME_BYTES + 1)
    if len(data) > MAX_FRAME_BYTES:
        raise HTTPException(413, "Camera frame is too large")
    loop = asyncio.get_running_loop()
    async with state.frame_lock:
        if state.ended:
            raise HTTPException(409, "Session expired or belongs to another account")
        if frame_seq <= state.last_frame_seq:
            return state.public()
        try:
            image = await loop.run_in_executor(None, _decode_rgb, data)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if state.vision_engine is None:
            state.vision_engine = LandmarkService.get_instance().new_engine()
        packet = await loop.run_in_executor(None, state.vision_engine.process, image, frame_seq, timestamp * 1000.0)
        try:
            payload = LandmarkPayload(session_id=session_id, generation=generation, **packet)
            response = await registry.landmarks(state, payload.model_dump())
        except (ValidationError, ValueError, TypeError, IndexError) as exc:
            raise HTTPException(422, str(exc)) from exc
    _maybe_start_vision(state, frame_seq, data)
    return response


@router.post("/monitor/end")
async def monitor_end(payload: EndPayload, device: dict = Depends(get_device)):
    state = get_session(device, payload.session_id, payload.generation, CLIENT_TYPE)
    await registry.end(state)
    return {"success": True}
