"""Authenticated concurrent camera monitoring for one scheduled medication dose."""

import uuid
from types import SimpleNamespace

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from app.database import get_pool
from app.dependencies import get_consented_user
from app.services import reachy_tasks
from app.services.emotion_service import EmotionService
from app.services.face_recognition_service import FaceRecognitionService
from app.services.intake_repository import commit_monitored, undo_monitored
from app.services.monitor_service import BusyOtherClient, registry

router = APIRouter(prefix="/api/intake/monitor", tags=["intake-monitor"])


async def current_account(token_user: dict = Depends(get_consented_user)) -> dict:
    pool = get_pool()
    async with pool.acquire() as conn:
        if "u_id" in token_user:
            row = await conn.fetchrow(
                'SELECT u_id, name, face_label FROM "user" WHERE u_id=$1 AND user_active=TRUE',
                token_user["u_id"])
        else:
            row = await conn.fetchrow(
                'SELECT u_id, name, face_label FROM "user" WHERE supabase_id=$1 AND user_active=TRUE',
                token_user.get("sub"))
    if not row:
        raise HTTPException(401, "Account is no longer active")
    return dict(row)


class StartPayload(BaseModel):
    intk_id: int = Field(gt=0)


class LandmarkPayload(BaseModel):
    session_id: str
    generation: str
    frame_seq: int = Field(gt=0)
    timestamp: float
    width: int = Field(ge=1, le=1920)
    height: int = Field(ge=1, le=1080)
    faces: list[dict] = Field(default_factory=list, max_length=4)
    poses: list[dict] = Field(default_factory=list, max_length=4)
    hands: list[list] = Field(default_factory=list, max_length=8)


class OutcomePayload(BaseModel):
    session_id: str
    generation: str
    event_id: str
    outcome: str


class EndPayload(BaseModel):
    session_id: str
    generation: str


class UndoPayload(BaseModel):
    event_id: str


def get_session(account: dict, session_id: str, generation: str, client_type: str = "browser"):
    try:
        uuid.UUID(session_id)
        uuid.UUID(generation)
        return registry.get(account["u_id"], session_id, generation, client_type)
    except (ValueError, AttributeError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/start")
async def start(payload: StartPayload, account: dict = Depends(current_account)):
    if not FaceRecognitionService._available or not EmotionService._available:
        raise HTTPException(503, "Identity or seed 43 emotion model is not ready")
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT i.intk_id, m.dose_form, m.units_per_dose FROM intake i JOIN medication m ON m.med_id=i.med_id "
            "WHERE i.intk_id=$1 AND i.u_id=$2 AND i.intake_stats IN ('pending','missed') "
            "AND m.pills_remaining>=m.units_per_dose AND m.is_active=TRUE",
            payload.intk_id, account["u_id"])
    if not row:
        raise HTTPException(409, "Dose is unavailable or does not belong to this account")
    # Same rule as the robot: a hand-to-mouth gesture can stand for one solid tablet, nothing else,
    # so other doses always ask the person to confirm.
    auto_commit = reachy_tasks.is_supported(row.get("dose_form", "solid_oral"), row.get("units_per_dose", 1))
    try:
        state = await registry.replace(account["u_id"], payload.intk_id, account["face_label"], account["name"],
                                       mode="dose", client_type="browser", auto_commit=auto_commit)
    except BusyOtherClient as exc:
        raise HTTPException(409, "busy_other_client") from exc
    return state.public()


@router.post("/landmarks")
async def landmarks(payload: LandmarkPayload, account: dict = Depends(current_account)):
    state = get_session(account, payload.session_id, payload.generation)
    try:
        return await registry.landmarks(state, payload.model_dump())
    except (ValueError, TypeError, IndexError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/vision")
async def vision(session_id: str = Form(...), generation: str = Form(...),
                 frame_seq: int = Form(...), file: UploadFile = File(...),
                 account: dict = Depends(current_account)):
    state = get_session(account, session_id, generation)
    data = await file.read(1_000_001)
    if len(data) > 1_000_000:
        raise HTTPException(413, "Camera frame is too large")
    try:
        return await registry.vision(state, frame_seq, data, commit_monitored)
    except (ValueError, TypeError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/outcome")
async def outcome(payload: OutcomePayload, account: dict = Depends(current_account)):
    state = get_session(account, payload.session_id, payload.generation)
    async with state.lock:
        candidate = state.candidate
        if not candidate or candidate["event_id"] != payload.event_id:
            raise HTTPException(409, "Event does not belong to the current session")
        try:
            if payload.outcome == "undo":
                state.recorded = await undo_monitored(state, payload.event_id)
                return state.public()
            if not candidate["ready"] or state.recorded or state.mode != "dose":
                raise HTTPException(409, "Event is not ready for confirmation")
            if payload.outcome == "taken_confirmed":
                state.recorded = await commit_monitored(state, candidate, "confirmed_by_user")
                state.mode = "observe"
            elif payload.outcome == "not_taken":
                state.candidate = None
            else:
                raise HTTPException(422, "Invalid outcome")
            return state.public()
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc


@router.post("/end")
async def end(payload: EndPayload, account: dict = Depends(current_account)):
    state = get_session(account, payload.session_id, payload.generation)
    await registry.end(state)
    return {"success": True}


@router.get("/recent")
async def recent(account: dict = Depends(current_account)):
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT e.event_id, e.intk_id, e.recorded_at FROM monitor_event e "
            "JOIN intake i ON i.intk_id=e.intk_id "
            "WHERE e.u_id=$1 AND e.outcome='taken' AND i.intake_stats='taken' "
            "ORDER BY e.recorded_at DESC LIMIT 10", account["u_id"])
    return [dict(row) for row in rows]


@router.post("/undo")
async def undo(payload: UndoPayload, account: dict = Depends(current_account)):
    try:
        uuid.UUID(payload.event_id)
        result = await undo_monitored(SimpleNamespace(u_id=account["u_id"], session_id=None), payload.event_id)
        session_id = registry.by_user.get(account["u_id"])
        state = registry.sessions.get(session_id) if session_id else None
        if state and state.recorded and state.recorded.get("event_id") == payload.event_id:
            state.recorded = result
        return result
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
