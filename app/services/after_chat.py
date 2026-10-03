"""Post-chat work for a check-in: summary, mood and memory facts.

Runs as a background task after /end and again from the sweep job (jobs/after_chat_job.py) for chats the
task missed. A risk chat never reaches a model here: its words were never sent, and still aren't."""

import logging

from app.database import get_pool
from app.services import consent_service, conversation, memory

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
MIN_PATIENT_TURNS, MIN_PATIENT_CHARS = 2, 15
ANALYSIS_SCOPES = ("core", "cloud_voice", "conversation_analysis")   # needed for any post-chat model call
_running: set[str] = set()   # one process serves both ports and the scheduler (app.serve)


async def process(conversation_id: str, u_id: int) -> str | None:
    """Returns the new after_chat_state, or None when the chat was not claimable."""
    if conversation_id in _running:
        return None
    _running.add(conversation_id)
    try:
        return await _process(conversation_id, u_id)
    finally:
        _running.discard(conversation_id)


async def _set_state(conversation_id: str, state: str, *, refund: bool = False) -> None:
    async with get_pool().acquire() as conn:
        await conn.execute(
            "UPDATE conversation SET after_chat_state = $2, after_chat_attempts = after_chat_attempts - $3 "
            "WHERE conversation_id = $1::uuid", conversation_id, state, 1 if refund else 0)


async def _save_summary(conversation_id: str, summary: str | None, mood: str) -> None:
    async with get_pool().acquire() as conn:
        await conn.execute("UPDATE conversation SET summary = $2, mood = $3 WHERE conversation_id = $1::uuid",
                           conversation_id, summary, mood)


async def _process(conversation_id: str, u_id: int) -> str | None:
    async with get_pool().acquire() as conn:
        chat = await conn.fetchrow(
            "UPDATE conversation SET after_chat_attempts = after_chat_attempts + 1 "
            "WHERE conversation_id = $1::uuid AND u_id = $2 AND after_chat_state IN ('pending','failed') "
            "AND after_chat_attempts < $3 RETURNING language, risk_flag, followup_memory_id",
            conversation_id, u_id, MAX_ATTEMPTS)
        if chat is None:
            return None
        history = [dict(row) for row in await conn.fetch(
            "SELECT role, text FROM conversation_turn WHERE conversation_id = $1::uuid ORDER BY turn_id",
            conversation_id)]
    language = conversation.language_of(chat["language"])
    if chat["risk_flag"]:
        await _save_summary(conversation_id, None, "unknown")
        await _set_state(conversation_id, "skipped")
        return "skipped"
    patient = [turn["text"] for turn in history if turn["role"] == "patient"]
    if not patient:
        await _set_state(conversation_id, "skipped")
        return "skipped"

    state = await consent_service.get_state(u_id)
    if not all(consent_service.is_current(state, scope) for scope in ANALYSIS_SCOPES):
        # Consent withdrawn after the chat (the sweep can reach an abandoned chat much later):
        # its text no longer goes to a model.
        await _set_state(conversation_id, "skipped")
        return "skipped"

    today = memory.local_today()
    raw = None
    try:
        memory_on = memory.consent_current(state)
        if memory_on and len(patient) >= MIN_PATIENT_TURNS and sum(map(len, patient)) >= MIN_PATIENT_CHARS:
            async with get_pool().acquire() as conn:
                known = await memory.current_facts(conn, u_id)
            summary, mood, raw, reason = await memory.after_chat_call(history, language, known, today)
        else:
            summary, mood = await conversation.summarize(history, language)
            reason = "ok" if summary else "empty"
    except Exception:
        log.exception("after-chat model call failed for %s", conversation_id)
        await _set_state(conversation_id, "failed")
        return "failed"
    if reason == "rate_limited":
        await _set_state(conversation_id, "pending", refund=True)   # the sweep retries; quota is not a failure
        return "pending"

    stored = 0
    try:
        await _save_summary(conversation_id, summary, mood)
        if raw:
            facts = [fact for fact in (memory.validate_fact(item, today=today, source="chat")
                                       for item in raw[:memory.MAX_FACTS])
                     if fact and memory.grounded(fact, "\n".join(patient))]
            async with get_pool().acquire() as conn, conn.transaction():
                stored = await memory.store_facts(conn, u_id, conversation_id, facts)
    except Exception:
        log.exception("storing post-chat results failed for %s", conversation_id)
    try:
        async with get_pool().acquire() as conn, conn.transaction():
            await memory.finish_followup(conn, u_id, chat["followup_memory_id"],
                                         [turn["text"] for turn in history if turn["role"] == "reachy"])
    except Exception:
        log.exception("follow-up bookkeeping failed for %s", conversation_id)
    log.info("after-chat %s: reason=%s stored=%d", conversation_id, reason, stored)
    # Nothing usable came back (openrouter/free sometimes routes to a model that answers nothing):
    # the sweep retries, up to MAX_ATTEMPTS.
    state = "failed" if summary is None and reason in ("unavailable", "empty") else "done"
    await _set_state(conversation_id, state)
    return state
