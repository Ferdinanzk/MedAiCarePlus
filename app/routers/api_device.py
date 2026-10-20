"""Robot device API, served only on the private port (main.py enforces the port rule).

Every route authenticates a device token (device_auth.get_device; the live chat's
WebSocket: device_auth.get_device_ws) and acts only for that device's own patient.
"""

import asyncio
import io
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import (APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile,
                     WebSocket)
from pydantic import BaseModel, Field, ValidationError, field_validator

from app.database import get_pool
from app.routers.api_monitor import EndPayload, LandmarkPayload, get_session
from app.services import (after_chat, ai_settings, consent_service, conversation, dose_safety, dose_status, dose_video,
                          live_session, memory, outbox, reachy_tasks, reminders, schedule)
from app.services.device_auth import get_device, get_device_for_heartbeat, get_device_ws
from app.services.face_recognition_service import FaceRecognitionService
from app.services.intake_detection import SessionEnded
from app.services.intake_repository import commit_monitored
from app.services.landmark_service import LandmarkService
from app.services.live_session import _in_background
from app.services.monitor_service import LABELS, BusyOtherClient, registry

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/device", tags=["device"])

CLIENT_TYPE = "reachy"
MAX_WAIT_SECONDS = 25
MAX_FRAME_BYTES = 1_000_000
FRAME_VISION_INTERVAL = 0.5   # identity + emotion on a streamed frame, as often as the server re-checks identity
SLOW_FRAME_SECONDS = 0.25     # a frame held up longer is logged (a 0.25 s gap makes the stream degraded)


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


class DoseEventPayload(BaseModel):
    """A watched dose that ended without a result (bridge/session.py WATCHING): family get the clip and the AI's
    reading (services/dose_event.py)."""
    intk_id: int = Field(gt=0)
    outcome: Literal["not_seen", "patient_left"]
    evidence: dict | None = None


class ExtraEventPayload(BaseModel):
    event_id: str
    decision: Literal["confirmed", "uncertain"]
    confidence: float = Field(ge=0, le=1)


class CameraReport(BaseModel):
    """The robot's camera as its watchdog reports it (robot app 0.7.0+). idle: no session, so 0 fps is normal."""
    state: Literal["idle", "ok", "stalled", "recovering", "unavailable"]
    fps: float | None = Field(default=None, ge=0, le=240, allow_inf_nan=False)
    since: float | None = Field(default=None, allow_inf_nan=False)   # unix seconds (robot clock) of the last state change
    media: dict | None = None    # the robot daemon's GET /api/media/status answer, e.g. {"available", "released"}


class HeartbeatPayload(BaseModel):
    robot_reachable: bool | None = None
    landmark_fps: float | None = Field(default=None, ge=0, le=240)
    vision_fps: float | None = Field(default=None, ge=0, le=240)
    bridge_version: str | None = Field(default=None, max_length=50)
    missing_clips: int | list[str] | None = None
    camera: CameraReport | None = None   # robots older than 0.7.0 send none
    companion: bool = False   # robot app 0.9.0: a companion session runs; keep its task from expiring


class MonitorStartPayload(BaseModel):
    mode: Literal["dose", "observe"]
    intk_id: int | None = Field(default=None, gt=0)
    task_id: str | None = None
    companion: bool = False   # robot app 0.9.0: an observe session of a companion session (spec 3.2)


def _uuid(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise HTTPException(404, "Task not found") from exc


async def _leased_task(conn, device: dict, task_id: str):
    task = await conn.fetchrow(live_session.LEASED_TASK_SQL, _uuid(task_id), device["u_id"], device["device_id"])
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


# The robot waits for family's answer (robot app 0.17.0: up to 120 s), and after a "not taken" searches and runs the
# intake again: a confirmation request keeps its task open at least this long from now (quick fixes of 19 Oct 2026).
CONFIRM_WAIT_EXTEND_SECONDS = 600
EXTEND_FOR_CONFIRM_SQL = (
    "UPDATE reachy_task SET expires_at = GREATEST(expires_at, "
    f"NOW() + INTERVAL '{CONFIRM_WAIT_EXTEND_SECONDS} seconds') "
    "WHERE task_id = $1::uuid AND status IN ('leased','searching','in_progress')")
# Family's answer to a task's request: the one `id` names, else the task's newest (index dose_confirmation_task_idx).
FAMILY_ANSWER_SQL = (
    "SELECT confirmation_id, intk_ids, resolution, created_at, resolved_at FROM dose_confirmation "
    "WHERE u_id = $1 AND task_id = $2::uuid AND ($3::uuid IS NULL OR confirmation_id = $3::uuid) "
    "ORDER BY created_at DESC LIMIT 1")


@router.get("/tasks/{task_id}/confirmation")
async def family_answer(task_id: str, request_id: uuid.UUID | None = Query(None, alias="id"),
                        device: dict = Depends(get_device)):
    """What family answered on LINE to the task's request: the request `id` names (the id its POST answered: a slot
    of two medicines has two), else the task's newest. 200 {"status": "none"} when there is no such request, never
    a 404 (the robot reads a 404 as a lost session); else its id, status (open, confirmed, denied or expired), doses
    and times. The robot polls it while it waits; read only."""
    async with get_pool().acquire() as conn:
        task = await _leased_task(conn, device, task_id)
        row = await conn.fetchrow(FAMILY_ANSWER_SQL, device["u_id"], str(task["task_id"]),
                                  str(request_id) if request_id is not None else None)
    if row is None:
        return {"status": "none"}
    return {"confirmation_id": str(row["confirmation_id"]), "status": row["resolution"] or "open",
            "intk_ids": list(row["intk_ids"]), "created_at": row["created_at"].isoformat(),
            "resolved_at": row["resolved_at"].isoformat() if row["resolved_at"] is not None else None}


@router.post("/tasks/{task_id}/confirmation")
async def confirmation(task_id: str, payload: ConfirmationPayload, device: dict = Depends(get_device)):
    """NEEDS_CONFIRM: the dose goes to caregiver confirmation (no stock change). A dose overdose protection refuses
    gets 409 with its reason (dose_not_due_yet, dose_too_soon, daily_max_reached, dose_expired; from
    dose_confirmation.create), whatever the patient said. The robot files this after it saw a hand-to-mouth event
    or heard 「我吃完了」, so a second dose (too soon, or over the daily maximum) also alerts family. In the same
    transaction the task stays open at least CONFIRM_WAIT_EXTEND_SECONDS more, for the robot's wait for family's
    answer and a second intake after a "not taken"."""
    from app.services import dose_confirmation

    # The robot files this while its camera session for the dose is still open: due and not expired are judged as
    # of when that session started (it was allowed then), like a camera commit.
    session = registry.sessions.get(registry.by_user.get(device["u_id"]) or "")
    started_at = (getattr(session, "started_at", None) if session is not None and session.client_type == CLIENT_TYPE
                  and session.intk_id == payload.intk_id else None)
    # What the request says about its video is known before it is made (dose_video.clip_outlook: the dose-video
    # consent and enough fresh frames); the clip of a detected moment starts before its approach.
    started = session.event_started_at if session is not None and session.client_type == CLIENT_TYPE else None
    video, frames = await dose_video.clip_outlook(
        device["u_id"], event_started_at=started if payload.source == "uncertain_detection" else None)
    evidence = {**(payload.evidence or {}), "video": video}
    try:
        async with get_pool().acquire() as conn, conn.transaction():
            task = await _leased_task(conn, device, task_id)
            if payload.intk_id not in list(task["intk_ids"]):
                raise HTTPException(409, "Dose does not belong to this task")
            try:
                confirmation_id = await dose_confirmation.create(
                    conn, u_id=device["u_id"], task_id=str(task["task_id"]), intk_ids=[payload.intk_id],
                    source=payload.source, evidence=evidence, started_at=started_at)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            await conn.execute(EXTEND_FOR_CONFIRM_SQL, str(task["task_id"]))
    except schedule.DoseRefused as refused:
        await dose_safety.alert_after(refused)
        raise
    if frames:   # capture() re-checks consent before writing
        dose_video.capture(device["u_id"], [payload.intk_id], frames=frames)
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


@router.post("/tasks/{task_id}/event")
async def dose_event_report(task_id: str, payload: DoseEventPayload, device: dict = Depends(get_device)):
    """The robot watched a dose and nothing settled it: no hand-to-mouth moment in the watch (not_seen) or the
    patient left (patient_left). Family get the camera's clip of those moments and the AI's reading in one LINE
    message, to judge for themselves (services/dose_event.py); the dose stays as it is. Idempotent per dose and
    outcome. Answers at once: the clip is made in the background."""
    from app.services import dose_event

    async with get_pool().acquire() as conn:
        task = await _leased_task(conn, device, task_id)
        if payload.intk_id not in list(task["intk_ids"]):
            raise HTTPException(409, "Dose does not belong to this task")
        row = await conn.fetchrow(
            "SELECT m.med_name FROM intake i JOIN medication m ON m.med_id = i.med_id WHERE i.intk_id = $1 "
            "AND i.u_id = $2", payload.intk_id, device["u_id"])
    evidence = payload.evidence if isinstance(payload.evidence, dict) else {}
    emotion = evidence.get("emotion") if isinstance(evidence.get("emotion"), dict) else None
    notes = []
    if evidence.get("said_done"):
        notes.append(("本人有說「吃完了」", "The patient said they had finished"))
    dedupe = f"robot:{task['task_id']}:{payload.intk_id}:{payload.outcome}"
    dose_event.schedule(device["u_id"], kind="watch", dedupe=dedupe, intk_ids=[payload.intk_id],
                        medication_name=row["med_name"] if row else None, verification=None, intake=payload.outcome,
                        confidence=None, event_time=schedule.current_time(), needs_review=True, emotion=emotion,
                        notes=notes)
    return {"event": dedupe, "queued": True}


def _unchanged(request: Request, key: str) -> bool:
    """If-None-Match names this key: the ETag as it was sent (quoted), a bare key or a weak W/ tag, one or several
    separated by commas (RFC 9110). Then the robot's copy is current and only the headers go back."""
    header = request.headers.get("if-none-match", "")
    tags = [tag.strip().removeprefix("W/").strip('"') for tag in header.split(",")]
    return key in tags


def _wav_response(data: bytes | None, key: str) -> Response:
    """A clip with its key in ETag (Cache-Control private: it is one patient's); 304 with the same headers and no
    body when `data` is None."""
    headers = {"ETag": f'"{key}"', "Cache-Control": "private"}
    if data is None:
        return Response(status_code=304, headers=headers)
    return Response(content=data, media_type="audio/wav", headers=headers)


@router.get("/clips/med/{med_id}")
async def medication_clip(med_id: int, request: Request, position: int = Query(1, ge=1, le=8),
                          language: str = Query("zh-TW"), device: dict = Depends(get_device)):
    """The spoken prompt for this patient's medicine in `position` of its slot (services/med_clips.py), as a WAV
    with its key in ETag; 304 for a matching If-None-Match (the kept clip is read, never rendered again, when the
    robot's copy is current); 404 when there is none (the robot then says its generic prompt)."""
    from app.services import med_clips

    found = await med_clips.clip(device["u_id"], med_id, position, language)
    if found is None:
        raise HTTPException(404, "No prompt clip for this medicine")
    data, key = found
    return _wav_response(None if _unchanged(request, key) else data, key)


@router.get("/clips/line/{line_id}")
async def line_clip(line_id: str, request: Request, language: str = Query("zh-TW"),
                    device: dict = Depends(get_device)):
    """One of Reachy's own lines around a live chat, in the live voice (services/voice_lines.py): a PCM16 mono 16 kHz
    WAV with its key in ETag; 304 for a matching If-None-Match, checked before any render; 404 when there is none
    (no live voice target, an unknown line, a per-patient line without its consent: check-in consent for opening
    and reminder_lead, the robot's consent for the dose lines; a render that failed, or a line that could not be
    kept or read): the robot then drops its copy, never the current one (a matching If-None-Match
    is a 304 first), and says the line with its own voice. A failing lookup is a 500 on purpose: the robot keeps
    its copy through a database error."""
    from app.services import voice_lines

    found = await voice_lines.locate(device["u_id"], line_id, language)
    if found is None:
        raise HTTPException(404, "No clip for this line")
    if _unchanged(request, found.key):
        return _wav_response(None, found.key)
    kept = await voice_lines.fetch(found)
    if kept is None:
        raise HTTPException(404, "No clip for this line")
    data, key = kept
    return _wav_response(data, key)


@router.get("/clips/speech/{key}")
async def speech_clip(key: str, language: str = Query("zh-TW"), device: dict = Depends(get_device)):
    """A sentence the server sent this robot as text (the text chat's opening or reply, a refusal's sentence) in the
    live voice (services/speech_clips.py): the PCM16 mono 16 kHz WAV of the key its `clip` named, once its render is
    done (waiting at most speech_clips.WAIT_SECONDS); 404 for a key never issued to this device's patient, expired,
    failed or not ready in time: the robot then says the words with its own voice. The key is a random token per
    issue, so the path the access log writes names no words. `language` is accepted as on the line route; the key
    already names the clip."""
    from app.services import speech_clips

    data = await speech_clips.get(device["u_id"], key)
    if data is None:
        raise HTTPException(404, "No clip for this text")
    return _wav_response(data, key)


# ── Speech-to-text for the 「我吃完了」 listener ──────────────────────────────────

async def _audio_at_most(request: Request, limit: int) -> bytes | None:
    """The body, read only while it is at most `limit` bytes: None once it is longer, so an oversized upload is
    never held whole."""
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            return None
    return bytes(body)


@router.post("/stt")
async def speech_to_text(request: Request, language: Literal["zh-TW", "en"] = Query("zh-TW"),
                         device: dict = Depends(get_device)):
    """One segment of the done-mode listener (raw PCM16 mono 16 kHz, application/octet-stream) as text, through
    OpenAI's transcription endpoint (services/speech_to_text.py; quick fixes 2 of 19 Oct 2026): 200 {"text",
    "model", "ms"}. 404 no_cloud_stt without a live voice target with a key or without the robot's microphone
    consent; 413 over 15 s of audio, 422 under 0.2 s or an odd number of bytes; 502 stt_failed for any failure of
    the call. The robot then uses its own Whisper. Nothing is stored, and no log carries the words or the audio."""
    from app.services import speech_to_text as stt

    api_key = stt.cloud_key()
    if api_key is None or not consent_service.is_current(await consent_service.get_state(device["u_id"]),
                                                         "robot_microphone"):
        raise HTTPException(404, "no_cloud_stt")
    audio = await _audio_at_most(request, stt.MAX_BYTES)
    if audio is None:
        raise HTTPException(413, "audio_too_long")
    if len(audio) < stt.MIN_BYTES:
        raise HTTPException(422, "audio_too_short")
    if len(audio) % 2:   # PCM16 is 2 bytes a sample; an odd data chunk would make a malformed WAV (no pad byte)
        raise HTTPException(422, "audio_not_pcm16")
    try:
        return await stt.transcribe(audio, language, api_key)
    except stt.SttFailed:
        raise HTTPException(502, "stt_failed") from None


# ── Check-in conversations ───────────────────────────────────────────────────

class ConversationStartPayload(BaseModel):
    task_id: str
    language: Literal["zh-TW", "en"] = "zh-TW"
    companion: bool = False   # robot app 0.9.0's companion session: no turn limit (companion spec 3.3)


MAX_METRIC_KEYS = 30
MAX_METRIC_KEY_LENGTH = 40
MAX_METRIC_TEXT = 80
MAX_METRIC_NUMBER = 10 ** 9   # ~11.6 days in ms; larger numbers are not timings or counts from a check-in
MAX_TURN_ID = 2_147_483_647   # conversation_turn.turn_id is a SERIAL (int4)


def _timing_metrics(value: dict | None) -> dict | None:
    """Robot timings (durations in ms, never clock readings): a flat object of at most 30 short keys, each value
    a number (at most 10**9 either way), true/false, short text or null. Anything else (nested objects, lists,
    long text, huge numbers) is refused."""
    if value is None:
        return None
    if len(value) > MAX_METRIC_KEYS:
        raise ValueError(f"metrics may have at most {MAX_METRIC_KEYS} keys")
    for key, item in value.items():
        if not 0 < len(key) <= MAX_METRIC_KEY_LENGTH:
            raise ValueError(f"metric names must be 1 to {MAX_METRIC_KEY_LENGTH} characters")
        if item is None or isinstance(item, bool):
            continue
        # abs() first: math.isfinite(10**400) would raise OverflowError (a 500, not a 422). The bound also
        # refuses NaN and infinity, which Python's JSON parser accepts.
        if isinstance(item, (int, float)) and abs(item) <= MAX_METRIC_NUMBER:
            continue
        if isinstance(item, str) and len(item) <= MAX_METRIC_TEXT:
            continue
        raise ValueError(f"metric {key!r} must be a number up to {MAX_METRIC_NUMBER}, true/false, text up to "
                         f"{MAX_METRIC_TEXT} characters, or null")
    return value


class ConversationTurnPayload(BaseModel):
    text: str = Field(min_length=1, max_length=conversation.MAX_TEXT)
    metrics: dict[str, Any] | None = None   # how the robot heard this utterance; kept on the patient turn

    @field_validator("metrics")
    @classmethod
    def _flat_metrics(cls, value: dict | None) -> dict | None:
        return _timing_metrics(value)


class TurnMetricsPayload(BaseModel):
    metrics: dict[str, Any]   # how the robot played one of Reachy's lines

    @field_validator("metrics")
    @classmethod
    def _flat_metrics(cls, value: dict) -> dict:
        return _timing_metrics(value)


class ConversationEndPayload(BaseModel):
    # Robot app 0.9.0 (companion spec 3.5): patient_left after its rest rule, medicine when a medicine task fell due.
    reason: Literal["finished", "goodbye", "silence", "risk", "patient_left", "stopped", "error",
                    "medicine"] = "finished"


def _ms_since(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


# ── Today's medicine for the check-in replies (get_today_dose_status, services/dose_status.py) ──
# The conversation's opening runs the forced tool call in the background (conversation.open_dose_status) and keeps
# its record on Reachy's opening turn, server.dose_tool. A patient's turn that comes while it still runs waits for
# it up to DOSE_OPEN_WAIT; each reply gets that record as a tool exchange, and the tool itself for a fresh look.
DOSE_OPEN_WAIT = 3.0
DOSE_LOOKUP_TIMEOUT = 2.0
MERGE_SERVER_METRICS_SQL = (
    "UPDATE conversation_turn SET metrics = COALESCE(metrics, '{}'::jsonb) || jsonb_build_object("
    "'server', COALESCE(metrics->'server', '{}'::jsonb) || $2::jsonb) WHERE turn_id = $1")
OPENING_DOSE_TOOL_SQL = ("SELECT metrics->'server'->'dose_tool' AS dose_tool FROM conversation_turn "
                         "WHERE conversation_id = $1::uuid AND role = 'reachy' ORDER BY turn_id LIMIT 1")
_dose_openings: dict = {}   # conversation_id -> its open_dose_status task, while it runs


def _dose_lookup(u_id: int, conversation_id: str):
    """The tool's answer for this conversation, on a connection of its own and within DOSE_LOOKUP_TIMEOUT."""
    async def lookup() -> dict:
        async def run():
            async with get_pool().acquire() as conn:
                return await dose_status.for_conversation(conn, u_id, conversation_id)
        return await asyncio.wait_for(run(), DOSE_LOOKUP_TIMEOUT)
    return lookup


async def _open_dose_status(u_id: int, conversation_id: str, turn_id: int, language: str, opening: str) -> None:
    """Background, from the conversation's start: the forced tool call, kept on the opening turn. Never raises: a
    failure leaves the replies without an opening record (they can still call the tool)."""
    try:
        record = await conversation.open_dose_status(language, opening, _dose_lookup(u_id, conversation_id))
        async with get_pool().acquire() as conn:
            await conn.execute(MERGE_SERVER_METRICS_SQL, turn_id, json.dumps({"dose_tool": record}))
        log.info("conversation %s: today's dose status %s (tool call by the %s)", conversation_id,
                 record["result"].get("status"), record["forced_by"])
    except Exception:
        log.exception("conversation %s: the opening dose status failed; replies call the tool themselves",
                      conversation_id)
    finally:
        _dose_openings.pop(conversation_id, None)


async def _opening_dose_record(conn, conversation_id: str) -> dict | None:
    stored = await conn.fetchval(OPENING_DOSE_TOOL_SQL, str(conversation_id))
    if isinstance(stored, str):   # JSONB comes back as text
        stored = json.loads(stored)
    return stored if isinstance(stored, dict) else None


async def _checkin_consent(device: dict) -> None:
    if not reachy_tasks.checkin_allowed(await consent_service.get_state(device["u_id"])):
        raise HTTPException(403, "checkin_consent_required")


async def _own_conversation(conn, device: dict, conversation_id: str):
    try:
        conversation_id = str(uuid.UUID(conversation_id))
    except ValueError as exc:
        raise HTTPException(404, "Conversation not found") from exc
    row = await conn.fetchrow(
        "SELECT conversation_id, language, ended_at, risk_flag, followup_memory_id, live, companion FROM conversation "
        "WHERE conversation_id = $1::uuid AND u_id = $2 FOR UPDATE", conversation_id, device["u_id"])
    if not row:
        raise HTTPException(404, "Conversation not found")
    return row


async def _history(conn, conversation_id) -> list[dict]:
    rows = await conn.fetch(
        "SELECT role, text FROM conversation_turn WHERE conversation_id = $1::uuid ORDER BY turn_id",
        str(conversation_id))
    return [dict(row) for row in rows]


def _live_patient_turns(row) -> int:
    """Patient turns a live chat stored before it fell back to this text chat (live_session, spec C3): the text
    chat's turn limit counts from the fallback, so they are left out of it."""
    live = row.get("live")
    live = json.loads(live) if isinstance(live, str) else live   # asyncpg hands JSONB back as text
    return int((live or {}).get("live_patient_turns") or 0)


async def _add_turn(conn, conversation_id, u_id: int, role: str, text: str, flagged: bool = False,
                    metrics: dict | None = None) -> int:
    return await conn.fetchval(
        live_session.INSERT_TURN_SQL,
        str(conversation_id), u_id, role, text, flagged, json.dumps(metrics) if metrics is not None else None)


def _spoken(reply: str, language: str, end: bool, reply_turn_id: int, u_id: int, risk: bool = False,
            conversation_id=None) -> dict:
    # reply_turn_id: the robot posts how it played this line to /turns/{reply_turn_id}/metrics.
    from app.services import speech_clips

    body = {"reply": reply, "speech_text": conversation.speech_text(reply, language), "end": end, "risk": risk,
            "reply_turn_id": reply_turn_id}
    if conversation_id is not None:
        body["conversation_id"] = str(conversation_id)
    clip = speech_clips.issue(u_id, reply, language)   # the words in the live voice, rendered from now on
    if clip is not None:
        body["clip"] = clip
    return body


@router.post("/conversations")
async def conversation_start(payload: ConversationStartPayload, device: dict = Depends(get_device)):
    """Open a check-in conversation for a task leased by this robot; returns the opening line. A companion session
    (robot app 0.9.0 sends companion: true) is stored as such: its chat has no turn limit (companion spec 3.3)."""
    await _checkin_consent(device)
    language = conversation.language_of(payload.language)
    conversation_id = str(uuid.uuid4())
    memory_on = memory.consent_current(await consent_service.get_state(device["u_id"]))
    async with get_pool().acquire() as conn, conn.transaction():
        task = await _leased_task(conn, device, payload.task_id)
        # The name: the memory 'name' entry (memory on), else the Reachy card's call name, else the registered name.
        name = await memory.call_name(conn, device["u_id"], memory_on)
        followup = await memory.pick_followup(conn, device["u_id"], memory.local_today()) if memory_on else None
        reminder = live_session._reminder_of(task)
        reminder_row = await reminders.take_for_robot(str(reminder["reminder_id"])) if reminder else None
        if reminder_row is not None:   # a reminder's check-in without the live voice: the robot's own voice says it
            opening = reminder_row["text"]
            await reminders.said_by_robot(str(reminder["reminder_id"]), "robot_text")
        elif task["reason"] == "checkin":
            opening = {
                "zh-TW": "我來提醒您查看用藥時間表，並依照處方在排定時間服藥。若現在還沒到時間，請不要提前服用。需要我陪您確認嗎？",
                "en": "I'm here to remind you to check your medication schedule and follow your prescription. If it isn't due yet, please don't take it early. Would you like me to help you check?",
            }[language]
        else:
            opening = memory.opening_line(language, name)
        await conn.execute(
            "INSERT INTO conversation (conversation_id, u_id, task_id, language, model, followup_memory_id, "
            "companion) VALUES ($1::uuid, $2, $3::uuid, $4, $5, $6::uuid, $7)",
            conversation_id, device["u_id"], str(task["task_id"]), language,
            ai_settings.main_model_id("reply"), str(followup["memory_id"]) if followup else None, payload.companion)
        turn_id = await _add_turn(conn, conversation_id, device["u_id"], "reachy", opening)
    # The forced get_today_dose_status call runs while the robot says the opening.
    _dose_openings[conversation_id] = _in_background(
        _open_dose_status(device["u_id"], conversation_id, turn_id, language, opening))
    return _spoken(opening, language, end=False, reply_turn_id=turn_id, u_id=device["u_id"],
                   conversation_id=conversation_id)


@router.post("/conversations/{conversation_id}/turn")
async def conversation_turn(conversation_id: str, payload: ConversationTurnPayload, device: dict = Depends(get_device)):
    """The patient's words (already text, from the robot) in, Reachy's reply out.

    Safety (the layers are described in services/conversation.py):
    - A keyword match never reaches the reply or risk-check model.
    - Every other turn is also judged by conversation.classify_risk, running at the same time as the reply, so
      the turn waits for the slower of the two. Goodbye and last turns, which get a fixed line, are judged too.
    - A risk either way means the help-line reply, the end of the conversation, and a family alert.
    - A judgement that failed is retried in the background (_late_risk_check). Once a conversation is flagged,
      any further turn gets the help line straight away.

    With memory consent, the reply (and nothing else) gets the memory block (memory.build_block), built only for a
    turn the model will answer.

    The robot's timings for the utterance are kept on the patient turn ({"robot": ...}); how long each server
    stage took goes on Reachy's turn ({"server": ...}): risk_source says which layer found a risk (keyword,
    model, earlier, late_model) or none. server_ms (from the handler's start, after device auth, to the reply
    being stored) lets the robot tell the server's share of its wait from the network's.
    """
    received = time.monotonic()
    await _checkin_consent(device)
    consent_ms = _ms_since(received)
    text = payload.text.strip()
    if not text:
        raise HTTPException(422, "Empty turn")
    started = time.monotonic()
    keyword = conversation.screen(text)
    screen_ms = _ms_since(started)
    opening = _dose_openings.get(conversation_id)
    if opening is not None:   # the opening's tool call is still running: the first reply should have its answer
        try:
            await asyncio.wait_for(asyncio.shield(opening), DOSE_OPEN_WAIT)
        except (asyncio.TimeoutError, Exception):
            pass
    started = time.monotonic()
    async with get_pool().acquire() as conn, conn.transaction():
        row = await _own_conversation(conn, device, conversation_id)
        if row["ended_at"] is not None:
            raise HTTPException(409, "Conversation has ended")
        cid, language = str(row["conversation_id"]), row["language"]
        turn_id = await _add_turn(conn, cid, device["u_id"], "patient", text, bool(keyword),
                                  metrics=None if payload.metrics is None else {"robot": payload.metrics})
        history = await _history(conn, cid)
        patient_turns = sum(1 for turn in history if turn["role"] == "patient") - _live_patient_turns(row)
        # A companion chat (robot app 0.9.0, companion spec 3.3) has no turn limit: a goodbye or a risk ends it, or
        # the robot does (rest, medicine, a stop).
        closing = conversation.wants_to_end(text) or (
            not row["companion"] and patient_turns >= conversation.MAX_PATIENT_TURNS)
        block = ""
        # The memory block (facts only) goes to the reply alone, so it is built only when the model will write
        # one: never for a risk turn, a flagged conversation or a fixed closing line.
        if not (keyword or row["risk_flag"] or closing):
            try:
                # A savepoint: a memory read that fails costs the reply its notes, never the patient's stored
                # words or the turn's risk check.
                async with conn.transaction():
                    if memory.consent_current(await consent_service.get_state(device["u_id"])):
                        block = await memory.build_block(conn, device["u_id"], language,
                                                         row["followup_memory_id"], memory.local_today())
            except Exception:
                log.exception("conversation %s: the memory block failed; replying without it", cid)
                block = ""
        dose = None
        if not (keyword or row["risk_flag"] or closing):
            record = None
            try:
                async with conn.transaction():   # a savepoint, as for the memory block
                    record = await _opening_dose_record(conn, cid)
            except Exception:
                log.exception("conversation %s: the opening dose status could not be read", cid)
            dose = conversation.DoseTool(exchange=conversation.dose_exchange(record),
                                         lookup=_dose_lookup(device["u_id"], cid))
        if keyword:   # the words never go to the model
            await conversation.alert_family(conn, device["u_id"], cid,
                                            conversation.safety_alert_text(device["name"], text, keyword),
                                            str(turn_id))
    db_ms = _ms_since(started)
    llm = {"llm_ms": 0, "fallback_used": False, "attempts": []}   # fixed lines never call the model
    check = {"risk_ms": 0, "risk_result": None, "risk_attempts": []}
    if keyword or row["risk_flag"]:
        reply, end, risk = conversation.HELPLINE[language], True, True
        source = "keyword" if keyword else "earlier"   # earlier: a late check flagged a turn already answered
    else:
        # No connection is held while the (possibly slow, free-tier) model answers. The risk check never gets
        # the memory block.
        classifying = asyncio.create_task(conversation.classify_risk(history, language))
        replying = (None if closing
                    else asyncio.create_task(conversation.reply_with_metrics(history, language, block, dose=dose)))
        kind, check = await classifying
        risk, end, source = kind is not None, True, "model" if kind else "none"
        if kind:
            if replying is not None:
                replying.cancel()   # the help line replaces it
            reply = conversation.HELPLINE[language]
            async with get_pool().acquire() as conn, conn.transaction():
                await conn.execute("UPDATE conversation_turn SET flagged = TRUE WHERE turn_id = $1", turn_id)
                await conversation.alert_family(conn, device["u_id"], cid,
                                                conversation.safety_alert_text(device["name"], text, kind),
                                                str(turn_id))
        elif replying is None:
            reply = conversation.CLOSING[language]
        else:
            (reply, llm), end = await replying, False
    server = {"received_to_reply_ms": _ms_since(received), "consent_ms": consent_ms, "screen_ms": screen_ms,
              "db_ms": db_ms, "llm_ms": llm["llm_ms"], "fallback_used": llm["fallback_used"], "risk": risk,
              "attempts": llm["attempts"], "risk_source": source, **check}
    if llm.get("dose_tool"):
        server["dose_tool"] = llm["dose_tool"]   # the tool calls this reply made, with what they answered
    async with get_pool().acquire() as conn:
        reply_turn_id = await _add_turn(conn, cid, device["u_id"], "reachy", reply, metrics={"server": server})
    if check["risk_result"] == "unknown":
        rate_limited = any(attempt.get("status") == 429 for attempt in check["risk_attempts"])
        # Post-chat work waits for it, so a late flag also means no post-chat model call (after_chat).
        after_chat.track_risk_check(cid, _in_background(_late_risk_check(
            device["u_id"], device["name"], cid, turn_id, reply_turn_id, text, history, language, rate_limited)))
    return {**_spoken(reply, language, end=end, reply_turn_id=reply_turn_id, u_id=device["u_id"], risk=risk),
            "server_ms": _ms_since(received)}


async def _late_risk_check(u_id: int, name: str, conversation_id: str, turn_id: int, reply_turn_id: int,
                           text: str, history: list[dict], language: str, rate_limited: bool = False) -> None:
    """The turn's risk judgement failed or ran out of time, and Reachy answered without it: ask again with longer
    while nobody waits (after a pause when OpenRouter rate-limited the first one). A risk flags the turn and alerts
    family (still once per conversation), and any next turn of the conversation gets the help line. The result
    goes on Reachy's turn as late_risk_* server metrics.

    It keeps running even when consent is withdrawn meanwhile: it judges words spoken under consent, and alerts
    continue after withdrawal (robot notice §6). Post-chat work for the conversation waits for it (bounded), so a
    risk it finds means no post-chat model call. Held only in memory: a restart drops it, and then the end-of-chat
    summary (retried by the after-chat sweep) is the check that remains. So is it when this one fails too."""
    try:
        if rate_limited:
            await asyncio.sleep(conversation.LATE_RISK_RATE_LIMIT_WAIT)
        kind, check = await conversation.classify_risk(history, language, late=True)
        if check["risk_result"] == "unknown":
            log.error("conversation %s turn %s: the model never judged it for safety risks; only the keyword "
                      "list and the end-of-chat summary check it", conversation_id, turn_id)
        late = {f"late_{key}": value for key, value in check.items()}
        if kind:
            late["risk_source"] = "late_model"
        async with get_pool().acquire() as conn, conn.transaction():
            await conn.execute(
                "UPDATE conversation_turn SET metrics = COALESCE(metrics, '{}'::jsonb) || jsonb_build_object("
                "'server', COALESCE(metrics->'server', '{}'::jsonb) || $2::jsonb) WHERE turn_id = $1",
                reply_turn_id, json.dumps(late))
            if kind:
                await conn.execute("UPDATE conversation_turn SET flagged = TRUE WHERE turn_id = $1", turn_id)
                await conversation.alert_family(conn, u_id, conversation_id,
                                                conversation.safety_alert_text(name, text, kind), str(turn_id))
    except Exception:
        log.exception("late risk check failed")


@router.post("/conversations/{conversation_id}/turns/{turn_id}/metrics")
async def conversation_turn_metrics(conversation_id: str, turn_id: str, payload: TurnMetricsPayload,
                                    device: dict = Depends(get_device)):
    """How the robot played one of Reachy's lines, merged into that turn's metrics under "robot".

    Timings only, so check-in consent is not needed, and they may arrive after the conversation has ended.
    Repeated posts merge (a later value for the same key wins), and the merged object keeps the 30-key cap
    (422 otherwise), so posting new keys again and again cannot grow a turn without limit.
    """
    try:
        turn = int(turn_id)
    except ValueError as exc:
        raise HTTPException(404, "Turn not found") from exc
    if not 0 < turn <= MAX_TURN_ID:
        raise HTTPException(404, "Turn not found")
    async with get_pool().acquire() as conn, conn.transaction():
        row = await _own_conversation(conn, device, conversation_id)
        stored = await conn.fetchrow(
            "SELECT metrics->'robot' AS robot FROM conversation_turn "
            "WHERE turn_id = $1 AND conversation_id = $2::uuid AND role = 'reachy' FOR UPDATE",
            turn, str(row["conversation_id"]))
        if stored is None:
            raise HTTPException(404, "Turn not found")
        robot = stored["robot"]
        robot = json.loads(robot) if isinstance(robot, str) else robot   # asyncpg hands JSONB back as text
        merged = {**(robot if isinstance(robot, dict) else {}), **payload.metrics}
        if len(merged) > MAX_METRIC_KEYS:
            raise HTTPException(422, f"a turn may keep at most {MAX_METRIC_KEYS} robot metrics")
        await conn.execute(
            "UPDATE conversation_turn SET metrics = COALESCE(metrics, '{}'::jsonb) || "
            "jsonb_build_object('robot', $2::jsonb) WHERE turn_id = $1",
            turn, json.dumps(merged))
    return {"ok": True}


@router.post("/conversations/{conversation_id}/end")
async def conversation_end(conversation_id: str, payload: ConversationEndPayload, device: dict = Depends(get_device)):
    """Close the conversation (idempotent). Post-chat work (summary, mood, the summary's risk backstop, memory
    facts) runs in the background through after_chat.process; the after-chat sweep retries what it misses."""
    async with get_pool().acquire() as conn, conn.transaction():
        row = await _own_conversation(conn, device, conversation_id)
        if row["ended_at"] is not None:
            return {"ended": True}
        await conn.execute(
            "UPDATE conversation SET ended_at = NOW(), end_reason = $2, after_chat_state = 'pending' "
            "WHERE conversation_id = $1::uuid", str(row["conversation_id"]), payload.reason)
    _in_background(after_chat.process(str(row["conversation_id"]), device["u_id"]))
    return {"ended": True}


# ── Live voice check-ins (spec 2026-10-12-live-voice-2a-design.md A2) ───────

LIVE_CHAT_OPEN = 4409   # WebSocket close code: this patient already has a live chat running
_live_chats: dict[int, live_session.LiveRelay] = {}   # u_id -> the patient's running live chat (this process)
reminders.find_relay = _live_chats.get   # a reminder that fires during a live chat is said through its relay


@router.websocket("/conversations/live")
async def conversation_live(websocket: WebSocket):
    """A live voice check-in: live_session.LiveRelay runs the start checks (a fallback to today's text chat at the
    first failure), GPT-Live and the chat. Device port only (main.py's DevicePortIsolation also covers WebSockets).
    get_device_ws closes the socket itself on a bad key (4401) or without robot consent (4403). One live chat per
    patient: a second connection while one runs is accepted and closed with 4409. The slot is freed once the chat is
    over and GPT-Live closed (LiveRelay.on_over), before the relay waits for a record or a note to family still
    running, so the patient's next live chat is not refused meanwhile and a reminder never looks for the old one."""
    device = await get_device_ws(websocket)
    if device is None:
        return
    u_id = device["u_id"]
    if u_id in _live_chats:
        log.warning("patient %s: a second live chat was refused while one runs", u_id)
        await websocket.accept()
        await websocket.close(code=LIVE_CHAT_OPEN, reason="live_chat_open")
        return
    relay = live_session.LiveRelay(live_session.WebSocketRobot(websocket), device)

    def free() -> None:
        if _live_chats.get(u_id) is relay:   # a newer chat may hold the slot already
            del _live_chats[u_id]

    relay.on_over = free
    _live_chats[u_id] = relay   # before the accept, with no await since the check: no second chat slips in
    try:
        await websocket.accept()
        await relay.run()
    finally:
        free()


@router.post("/heartbeat")
async def heartbeat(payload: HeartbeatPayload, device: dict = Depends(get_device_for_heartbeat)):
    server_time = datetime.now(timezone.utc).isoformat()
    if device["revoked"] or not device["consent_current"]:
        return {"stop_all": True, "microphone": False, "server_time": server_time}
    detail = {"vision_fps": payload.vision_fps, "bridge_version": payload.bridge_version,
              "missing_clips": payload.missing_clips}
    if payload.camera is not None:
        detail["camera"] = payload.camera.model_dump()   # the Reachy card reads it through /api/reachy/status
    if payload.companion:
        detail["companion"] = True   # the missed-dose job reads it (reachy_tasks.meal_retry_waiting_sql)
    async with get_pool().acquire() as conn, conn.transaction():
        await conn.execute(
            "UPDATE reachy_device SET last_seen_at = NOW(), robot_reachable = $2, landmark_fps = $3, "
            "status_detail = $4::jsonb WHERE device_id = $1::uuid",
            device["device_id"], payload.robot_reachable, payload.landmark_fps, json.dumps(detail))
        await reachy_tasks.extend_leases(conn, device["device_id"])
        if payload.companion:
            await reachy_tasks.extend_for_companion(conn, device["device_id"])
    # Medicine first (companion spec 3.4): a robot in a companion session ends it when a medicine task waits. Read after
    # the writes commit: a failing read answers false and never rolls back last_seen_at or the lease extension.
    try:
        async with get_pool().acquire() as conn:
            task_due = await reachy_tasks.task_due(conn, device["u_id"])
    except Exception:
        log.warning("heartbeat: the medicine-task check failed; answering task_due false", exc_info=True)
        task_due = False
    microphone = consent_service.is_current(await consent_service.get_state(device["u_id"]), "robot_microphone")
    return {"stop_all": False, "microphone": microphone, "server_time": server_time, "task_due": task_due}


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
    checkin_search = False
    intk_id = payload.intk_id if payload.mode == "dose" else None
    async with get_pool().acquire() as conn:
        if payload.task_id is not None:
            task = await _leased_task(conn, device, payload.task_id)
            if task["status"] not in ("leased", "searching", "in_progress"):
                raise HTTPException(409, "Task is no longer active")
            if intk_id is not None and intk_id not in list(task["intk_ids"]):
                raise HTTPException(409, "Dose does not belong to this task")
            # Only a check-in task's search verifies with the OpenVINO match alone (monitor_service.openvino_only).
            # A companion session's observe session too (robot app 0.9.0): it never records, and no dose prompt
            # follows it, so the patient can be recognised from across the room (spec 3.2).
            checkin_search = task["reason"] == "checkin" or (payload.companion and payload.mode == "observe")
        if intk_id is not None:
            row = await conn.fetchrow(
                "SELECT m.dose_form, m.units_per_dose, i.intake_time_stamp FROM intake i "
                "JOIN medication m ON m.med_id=i.med_id "
                "WHERE i.intk_id=$1 AND i.u_id=$2 AND i.intake_stats IN ('pending','missed') "
                "AND m.pills_remaining>=m.units_per_dose AND m.is_active=TRUE",
                intk_id, device["u_id"])
            if not row:
                raise HTTPException(409, "Dose is unavailable or does not belong to this account")
            # Overdose protection (409 with the reason and a sentence the robot can say); starting is no evidence.
            await dose_safety.check(conn, device["u_id"], [intk_id])
            # Server policy (D2): the bridge's view is advisory.
            auto_commit = device["auto_record"] and reachy_tasks.is_supported(row["dose_form"], row["units_per_dose"])
    try:
        state = await registry.replace(device["u_id"], intk_id, device["face_label"], device["name"],
                                       mode=payload.mode, client_type=CLIENT_TYPE, auto_commit=auto_commit,
                                       checkin_search=checkin_search)
    except BusyOtherClient as exc:
        raise HTTPException(409, "busy_other_client") from exc
    state.clip_enabled = await dose_video.enabled(device["u_id"])
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
    if state.clip_enabled:
        dose_video.buffer_frame(state.u_id, data, "reachy")
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
    except schedule.DoseRefused as refused:
        # Overdose protection refused the auto-commit (monitor/start had allowed the dose; something changed during
        # the session). Nothing was recorded, a second dose alerted family, and the robot hears the reason when it
        # files its confirmation request.
        log.info("dose %s of session %s not recorded: %s", refused.intk_id, state.session_id, refused.detail)
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
    arrived = time.monotonic()
    async with state.frame_lock:
        locked = time.monotonic()
        if state.ended:
            raise HTTPException(409, "Session expired or belongs to another account")
        if frame_seq <= state.last_frame_seq:
            return state.public()
        try:
            image = await loop.run_in_executor(None, _decode_rgb, data)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if state.clip_enabled:
            dose_video.buffer_frame(state.u_id, data, "reachy")
        if state.vision_engine is None:
            state.vision_engine = LandmarkService.get_instance().new_engine()
        packet = await loop.run_in_executor(None, state.vision_engine.process, image, frame_seq, timestamp * 1000.0)
        try:
            payload = LandmarkPayload(session_id=session_id, generation=generation, **packet)
            response = await registry.landmarks(state, payload.model_dump())
        except SessionEnded as exc:
            # The detector ended the session after the state.ended check above (its completion barrier): same answer.
            raise HTTPException(409, "Session expired or belongs to another account") from exc
        except (ValidationError, ValueError, TypeError, IndexError) as exc:
            raise HTTPException(422, str(exc)) from exc
    done = time.monotonic()
    if done - arrived > SLOW_FRAME_SECONDS:
        # The robot logs its own stream gaps; this says whether the server held a frame up, and where.
        log.warning("monitor frame %s of session %s took %.2f s once received: %.2f s waiting for the session's "
                    "frame lock, %.2f s decoding it and computing landmarks", frame_seq, session_id, done - arrived,
                    locked - arrived, done - locked)
    # A verified patient's dose session is scored 4 times a second until its emotion result is written
    # (monitor_service.vision_interval, for the uncovered faces just before and after the pill; identity stays at
    # most 2 Hz); _maybe_start_vision's own gate is FRAME_VISION_INTERVAL.
    from app.services.monitor_service import vision_interval

    interval = vision_interval(state)
    if interval < FRAME_VISION_INTERVAL and time.monotonic() - state.last_vision_started >= interval:
        state.last_vision_started = float("-inf")     # due now; _maybe_start_vision still skips a running task
    _maybe_start_vision(state, frame_seq, data)
    return response


@router.post("/monitor/end")
async def monitor_end(payload: EndPayload, device: dict = Depends(get_device)):
    state = get_session(device, payload.session_id, payload.generation, CLIENT_TYPE)
    await registry.end(state)
    return {"success": True}
