"""The patient's own Reachy check-in conversations (robot notice §4: the patient sees everything).

Each turn may carry timings (conversation_turn.metrics, in ms): how the robot heard a patient turn, and how
Reachy's reply was made and played. /metrics/* sum them up so the slow steps of a live conversation can be found.
"""

import json
import math
import statistics
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query

from app.database import get_pool
from app.dependencies import get_consented_user
from app.jobs.conversation_retention_job import TRANSCRIPT_DAYS
from app.services import deletion_ledger, memory

router = APIRouter(prefix="/api/conversations", tags=["conversations"])

MAX_METRIC_DAYS = 90   # accepted, but windows are cut to TRANSCRIPT_DAYS (see _window)
MAX_METRIC_TURNS = 2000
MAX_STAGE_MS = 24 * 60 * 60 * 1000   # a "duration" above a day is not a check-in timing; it is left out
PATIENT_STAGES = ("vad_release_ms", "stt_ms", "handover_ms")                  # robot, on patient turns
REPLY_ROBOT_STAGES = ("round_trip_ms", "tts_first_audio_ms", "tts_total_ms")  # robot, on Reachy's turns
FIRST_SOUND = "speech_end_to_first_sound_ms"   # handover + round trip + first audio: the silence the patient hears
STAGES = ("vad_release_ms", "stt_ms", "handover_ms", "round_trip_ms", "llm_ms", "tts_first_audio_ms",
          "tts_total_ms", FIRST_SOUND)
_IN_WINDOW = "c.u_id = $1 AND t.created_at >= NOW() - make_interval(days => $2::int)"


def _conversation_id(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise HTTPException(404, "Conversation not found") from exc


def _metrics(value) -> dict | None:
    """A turn's metrics as an object (asyncpg hands JSONB back as text), or None."""
    if value is None:
        return None
    value = json.loads(value) if isinstance(value, str) else value
    return value if isinstance(value, dict) else None


def _part(metrics: dict | None, key: str) -> dict:
    part = (metrics or {}).get(key)
    return part if isinstance(part, dict) else {}


def _ms(value) -> float | None:
    """A duration in ms, or None when missing or not a duration (a flag, text, a negative number, or more than a
    day). The ceiling keeps rows stored before the device API bounded numbers from breaking the median."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= MAX_STAGE_MS:
        return None
    return value


def _window(days: int) -> int:
    """The days a window really covers. Only risk-flagged patient turns outlive the 30-day transcript purge, so
    a longer window would quietly report a biased sample; it is cut to the retention period instead."""
    return min(days, TRANSCRIPT_DAYS)


def _spread(values: list[float]) -> dict:
    ordered = sorted(values)
    return {"count": len(ordered), "median_ms": round(statistics.median(ordered)),
            "p90_ms": round(ordered[math.ceil(0.9 * len(ordered)) - 1])}   # nearest rank


# The literal /metrics/* routes come before /{conversation_id}, which would otherwise capture them.

@router.get("/metrics/summary")
async def metrics_summary(user: dict = Depends(get_consented_user),
                          days: int = Query(7, ge=1, le=MAX_METRIC_DAYS)):
    """Median and 90th-percentile time of each check-in stage over the last `days` days.

    days in the answer is the window really used: at most the 30-day transcript retention. turns counts the
    turns in the window that carry timings; stages without timings are left out. fallback_rate is the share of
    model replies that ended in the fixed line (None when no reply asked the model); models counts every model
    call (failed ones too) by the model that answered it, else the model asked.
    """
    days = _window(days)
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT t.conversation_id, t.turn_id, t.role, t.metrics "
            "FROM conversation_turn t JOIN conversation c ON c.conversation_id = t.conversation_id "
            f"WHERE {_IN_WINDOW} ORDER BY t.conversation_id, t.turn_id",
            user["u_id"], days)
    samples: dict[str, list[float]] = {stage: [] for stage in STAGES}
    models: dict[str, list[float]] = {}
    measured = model_replies = fallbacks = 0
    unanswered: dict = {}   # conversation -> handover_ms of its latest patient turn that Reachy has not answered

    def add(stage: str, value) -> None:
        if _ms(value) is not None:
            samples[stage].append(value)

    for row in rows:
        metrics = _metrics(row["metrics"])
        measured += bool(metrics)
        robot, server = _part(metrics, "robot"), _part(metrics, "server")
        if row["role"] == "patient":
            for stage in PATIENT_STAGES:
                add(stage, robot.get(stage))
            unanswered[row["conversation_id"]] = _ms(robot.get("handover_ms"))
            continue
        for stage in REPLY_ROBOT_STAGES:
            add(stage, robot.get(stage))
        parts = (unanswered.pop(row["conversation_id"], None), _ms(robot.get("round_trip_ms")),
                 _ms(robot.get("tts_first_audio_ms")))
        if None not in parts:
            samples[FIRST_SOUND].append(sum(parts))
        attempts = [attempt for attempt in server.get("attempts") or [] if isinstance(attempt, dict)]
        if attempts or server.get("fallback_used") is True:   # the model was asked (fixed lines never ask it)
            model_replies += 1
            fallbacks += server.get("fallback_used") is True
            add("llm_ms", server.get("llm_ms"))
        for attempt in attempts:
            model = attempt.get("model_served") or attempt.get("model_requested")
            if isinstance(model, str) and _ms(attempt.get("ms")) is not None:
                models.setdefault(model, []).append(attempt["ms"])
    return {
        "days": days,
        "turns": measured,
        "stages": {stage: _spread(values) for stage, values in samples.items() if values},
        "fallback_rate": round(fallbacks / model_replies, 3) if model_replies else None,
        "models": sorted(({"model": model, "count": len(values), "median_ms": round(statistics.median(values))}
                          for model, values in models.items()), key=lambda m: (-m["count"], m["model"])),
    }


@router.get("/metrics/turns")
async def metric_turns(user: dict = Depends(get_consented_user),
                       days: int = Query(7, ge=1, le=MAX_METRIC_DAYS)):
    """Every turn of the last `days` days (at most the 30-day transcript retention) with its timings, newest first
    (at most 2000), for a CSV download.

    The words are left out; only their length is given.
    """
    days = _window(days)
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT t.conversation_id, t.turn_id, t.role, t.created_at, char_length(t.text) AS text_chars, "
            "t.metrics FROM conversation_turn t JOIN conversation c ON c.conversation_id = t.conversation_id "
            f"WHERE {_IN_WINDOW} ORDER BY t.created_at DESC, t.turn_id DESC LIMIT $3",
            user["u_id"], days, MAX_METRIC_TURNS)
    return {"items": [{"conversation_id": str(row["conversation_id"]), "turn_id": row["turn_id"],
                       "role": row["role"], "created_at": row["created_at"], "text_chars": row["text_chars"],
                       "metrics": _metrics(row["metrics"])} for row in rows]}


@router.get("")
async def list_conversations(user: dict = Depends(get_consented_user),
                             limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.conversation_id AS id, c.started_at, c.ended_at, c.end_reason, c.summary, c.mood,
                   c.risk_flag, c.language,
                   COUNT(t.turn_id) FILTER (WHERE t.role = 'patient') AS patient_turns,
                   (SELECT text FROM conversation_turn p WHERE p.conversation_id = c.conversation_id
                      AND p.role = 'patient' ORDER BY p.turn_id LIMIT 1) AS first_words,
                   COUNT(*) OVER () AS total
            FROM conversation c
            LEFT JOIN conversation_turn t ON t.conversation_id = c.conversation_id
            WHERE c.u_id = $1
            GROUP BY c.conversation_id
            ORDER BY c.started_at DESC
            LIMIT $2 OFFSET $3
            """,
            user["u_id"], limit, offset)
    items = [{**{key: value for key, value in dict(row).items() if key != "total"},
              "id": str(row["id"]), "patient_turns": int(row["patient_turns"])} for row in rows]
    total = int(rows[0]["total"]) if rows else 0
    return {"items": items, "total": total, "has_more": offset + len(items) < total}


@router.get("/{conversation_id}")
async def get_conversation(conversation_id: str, user: dict = Depends(get_consented_user)):
    conversation_id = _conversation_id(conversation_id)
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT conversation_id AS id, started_at, ended_at, end_reason, summary, mood, risk_flag, language, model "
            "FROM conversation WHERE conversation_id = $1::uuid AND u_id = $2", conversation_id, user["u_id"])
        if not row:
            raise HTTPException(404, "Conversation not found")
        turns = await conn.fetch(
            "SELECT turn_id, role, text, flagged, created_at, metrics FROM conversation_turn "
            "WHERE conversation_id = $1::uuid ORDER BY turn_id", conversation_id)
    return {**dict(row), "id": str(row["id"]),
            "turns": [{**dict(turn), "metrics": _metrics(turn["metrics"])} for turn in turns]}


@router.delete("/{conversation_id}")
async def delete_conversation(conversation_id: str, user: dict = Depends(get_consented_user)):
    conversation_id = _conversation_id(conversation_id)
    async with get_pool().acquire() as conn, conn.transaction():
        await memory.lock_user(conn, user["u_id"])     # facts learned in this chat cascade with it
        deleted = await conn.fetchval(
            "DELETE FROM conversation WHERE conversation_id = $1::uuid AND u_id = $2 RETURNING conversation_id",
            conversation_id, user["u_id"])
        if deleted:
            await deletion_ledger.record(conn, "conversation", user["u_id"], str(deleted))
    if not deleted:
        raise HTTPException(404, "Conversation not found")
    deletion_ledger.append_host_file("conversation", user["u_id"], str(deleted))
    return {"deleted": str(deleted)}
