"""Post-chat work for a check-in: summary, mood, the summary's risk backstop (layer 3 of services/conversation.py)
and memory facts. This module is its one owner.

Runs as a background task after /end, and again from the sweep job (jobs/after_chat_job.py) for abandoned chats,
chats the task missed (background tasks die with every container rebuild) and failed attempts.
- A risk chat never reaches a model here: its words were never sent, and still aren't. A late risk check still
  running for the chat is waited for first (bounded), so its flag counts too.
- Withdrawn check-in analysis consent: no model call, and so no backstop (layers 1 and 2 ran under consent).
- With memory consent and enough to go on, one call writes the summary, the RISK line and the facts
  (memory.after_chat_call); otherwise conversation.summarize. Either way a RISK line no earlier layer acted on
  alerts family, in the transaction that saves the summary. The alert quotes the summary only when Reachy had no
  memory notes in the chat (memory notice §3: family never see the notes).
- An attempt that gets no RISK judgement (no model answered, or none wrote a RISK line) saves nothing and is tried
  again. When the last attempt gets none, never finishes (a restart, an error) or cannot save its results, an
  unflagged chat gets a safety_check_incomplete family notification in the app: the gap is never silent. So does a
  chat still unfinished RETRY_DAYS after it ended (end_stale)."""

import asyncio
import logging

from app.database import get_pool
from app.services import consent_service, conversation, memory, outbox

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
RETRY_DAYS = 2   # the sweep retries a chat this long after its end; end_stale then ends it
MIN_PATIENT_TURNS, MIN_PATIENT_CHARS = 2, 15
ANALYSIS_SCOPES = ("core", "cloud_voice", "conversation_analysis")   # needed for any post-chat model call
# A rate limit is not an attempt for this long after the end (the 5-minute sweep retries); later it counts, so under
# a lasting rate limit (a used-up daily quota also stops the risk check) family hear within about half an hour that
# the check did not run.
RATE_LIMIT_REFUND_MINUTES = 10
INCOMPLETE_TEXT = ("Reachy could not finish the end-of-chat safety check of a check-in conversation: the AI "
                   "service did not answer. Please read the conversation in the app.")
_running: set[str] = set()   # one process serves both ports and the scheduler (app.serve)
_risk_checks: dict[str, set] = {}   # conversation id -> its late risk checks still running (api_device)


def track_risk_check(conversation_id: str, task: asyncio.Task) -> None:
    """A late risk check of this conversation is running: post-chat work waits for it before claiming the chat."""
    tasks = _risk_checks.setdefault(conversation_id, set())
    tasks.add(task)

    def finished(done: asyncio.Task) -> None:
        tasks.discard(done)
        if not tasks and _risk_checks.get(conversation_id) is tasks:
            del _risk_checks[conversation_id]

    task.add_done_callback(finished)


def risk_check_wait() -> float:
    """The longest post-chat work waits for late risk checks: their rate-limit pause and deadline, plus a margin."""
    return conversation.LATE_RISK_RATE_LIMIT_WAIT + conversation.LATE_RISK_DEADLINE_SECONDS + 5


async def _wait_for_risk_checks(conversation_id: str) -> None:
    loop = asyncio.get_running_loop()
    tasks = [task for task in _risk_checks.get(conversation_id, ())
             if not task.done() and task.get_loop() is loop]
    if not tasks:
        return
    _, pending = await asyncio.wait(tasks, timeout=risk_check_wait())
    if pending:
        log.warning("conversation %s: a late risk check is still running; post-chat work goes ahead",
                    conversation_id)


async def process(conversation_id: str, u_id: int) -> str | None:
    """Returns the new after_chat_state, or None when the chat was not claimable (or is already being processed).
    A chat whose last attempt never finished is ended here too (_close_unfinished)."""
    if conversation_id in _running:
        return None
    _running.add(conversation_id)   # before the wait, so the sweep can't start it twice meanwhile
    try:
        await _wait_for_risk_checks(conversation_id)
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


# Nothing saved (mood NULL) means no RISK judgement was saved: mood is written only with a judged summary, by the
# last attempt together with the notification, or for a flagged chat.
_END_UNFINISHED = ("after_chat_state = CASE WHEN risk_flag THEN 'skipped' WHEN mood IS NOT NULL THEN 'done' "
                   "ELSE 'failed' END, mood = COALESCE(mood, 'unknown')")
_NOTIFY_INCOMPLETE = "INSERT INTO notification (u_id, category, type, message) VALUES ($1, 'family', $2, $3)"


async def _notify_sad_mood(conn, u_id: int, conversation_id: str, name: str, language: str) -> int:
    """Tell opted-in, verified caregivers when a completed check-in is classified as sad."""
    enabled = await conn.fetchval(
        "SELECT notify_family_on_bad_mood FROM notification_settings WHERE u_id = $1", u_id)
    if enabled is False:
        return 0
    if language == "zh-TW":
        text = (f"情緒關懷提醒：{name} 聊到下雨、想出去玩時感到難過，請找時間關心一下。\n"
                f"Mood check-in: {name} felt sad about the rain stopping them from going out. Please check in when you can.")
    else:
        text = (f"Mood check-in: {name} felt sad about the rain stopping them from going out. "
                "Please check in when you can.")
    queued = await outbox.enqueue_to_contacts(
        conn, u_id, kind="emotion_alert", priority=2, messages=[{"type": "text", "text": text}],
        dedupe_prefix=f"conversation_sad:{conversation_id}", contact_flag="notify_emotion")
    if queued:
        await conn.execute(
            "INSERT INTO notification (u_id, category, type, message) VALUES ($1, 'family', 'emotion_alert', $2)",
            u_id, f"Sad check-in mood alert queued for {queued} caregiver(s)")
    return queued


async def _close_unfinished(conn, conversation_id: str, u_id: int) -> str | None:
    """End a chat still 'pending' after its last attempt was claimed. That attempt never finished (a restart, or an
    error before its state was written) or could not save its results: process() runs one attempt per chat at a
    time, and this runs inside it. It is not tried again. With nothing saved (mood NULL), an unflagged chat gets the
    safety_check_incomplete notification, as after any last failed attempt, so the gap is never silent. Returns the
    new state, or None."""
    async with conn.transaction():
        state = await conn.fetchval(
            f"UPDATE conversation SET {_END_UNFINISHED} "
            "WHERE conversation_id = $1::uuid AND u_id = $2 AND after_chat_state = 'pending' "
            "AND after_chat_attempts >= $3 RETURNING after_chat_state", conversation_id, u_id, MAX_ATTEMPTS)
        if state == "failed":
            log.error("conversation %s: its last post-chat attempt never finished, so the end-of-chat safety check "
                      "did not run", conversation_id)
            await conn.execute(_NOTIFY_INCOMPLETE, u_id, "safety_check_incomplete", INCOMPLETE_TEXT)
    return state


async def end_stale(conn) -> int:
    """End, without a model call, chats whose post-chat work is still unfinished RETRY_DAYS after they ended (the
    app was down, or every attempt was lost), as _close_unfinished does: an unflagged chat with nothing saved gets
    the safety_check_incomplete notification. A chat whose last attempt failed was told already. Returns how many
    were ended. Run by the sweep (jobs/after_chat_job.py)."""
    async with conn.transaction():
        rows = await conn.fetch(
            f"UPDATE conversation SET {_END_UNFINISHED}, after_chat_attempts = GREATEST(after_chat_attempts, $1) "
            "WHERE (after_chat_state = 'pending' OR (after_chat_state = 'failed' AND after_chat_attempts < $1)) "
            "AND ended_at <= NOW() - make_interval(days => $2) AND NOT (conversation_id::text = ANY($3::text[])) "
            "RETURNING conversation_id, u_id, after_chat_state", MAX_ATTEMPTS, RETRY_DAYS, sorted(_running))
        for row in rows:
            if row["after_chat_state"] == "failed":
                log.error("conversation %s: post-chat work never finished within %d days, so the end-of-chat safety "
                          "check did not run", row["conversation_id"], RETRY_DAYS)
                await conn.execute(_NOTIFY_INCOMPLETE, row["u_id"], "safety_check_incomplete", INCOMPLETE_TEXT)
    return len(rows)


def _memory_was_on(state: dict, started_at) -> bool:
    """Whether Reachy could have had the memory notes in this chat (the reply's block, the named opening, the
    follow-up): memory is on now, or its consent changed after the chat started (it was on for part of it)."""
    if memory.consent_current(state):
        return True
    changed = (state.get("conversation_memory") or {}).get("created_at")
    return changed is not None and started_at is not None and changed > started_at


async def _alert_alone(conversation_id: str, u_id: int, quote: str, risk: str) -> None:
    """The summary found a risk, but its results could not be saved: the alert matters more, so it goes on its own."""
    log.error("conversation %s: the summary found %s, and saving it failed; alerting family on its own",
              conversation_id, risk)
    try:
        async with get_pool().acquire() as conn, conn.transaction():
            name = await conn.fetchval('SELECT name FROM "user" WHERE u_id = $1', u_id)
            await conversation.summary_backstop(conn, u_id, name or "", conversation_id, quote, risk)
    except Exception:
        log.exception("conversation %s: the summary's risk alert failed too", conversation_id)


async def _process(conversation_id: str, u_id: int) -> str | None:
    async with get_pool().acquire() as conn:
        # 'pending' until this attempt writes its state, whatever the state was: one that never finishes is ended
        # by _close_unfinished, never left 'failed' with no attempts left (which the sweep no longer picks up).
        chat = await conn.fetchrow(
            "UPDATE conversation SET after_chat_attempts = after_chat_attempts + 1, after_chat_state = 'pending' "
            "WHERE conversation_id = $1::uuid AND u_id = $2 AND after_chat_state IN ('pending','failed') "
            "AND after_chat_attempts < $3 RETURNING language, risk_flag, followup_memory_id, after_chat_attempts, "
            "started_at, ended_at > NOW() - make_interval(mins => $4) AS refundable",
            conversation_id, u_id, MAX_ATTEMPTS, RATE_LIMIT_REFUND_MINUTES)
        if chat is None:
            return await _close_unfinished(conn, conversation_id, u_id)
        history = [dict(row) for row in await conn.fetch(
            "SELECT role, text FROM conversation_turn WHERE conversation_id = $1::uuid ORDER BY turn_id",
            conversation_id)]
    language = conversation.language_of(chat["language"])
    if chat["risk_flag"]:
        # Family were alerted by an earlier layer; the words never reach a model (summary NULL, mood unknown).
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
    raw, quote = None, False
    try:
        known = []
        if _memory_was_on(state, chat["started_at"]):
            async with get_pool().acquire() as conn:
                known = await memory.current_facts(conn, u_id)
        # Family never see the memory notes (memory notice §3). Reachy's lines in a chat it had notes for can carry
        # them into the summary, whichever prompt writes it, so a summary alert then does not quote the summary; it
        # still goes, saying it comes from the summary.
        quote = not (known or chat["followup_memory_id"])
        if (memory.consent_current(state) and len(patient) >= MIN_PATIENT_TURNS
                and sum(map(len, patient)) >= MIN_PATIENT_CHARS):
            summary, mood, risk, raw, reason = await memory.after_chat_call(history, language, known, today)
        else:
            info: dict = {}
            summary, mood, risk = await conversation.summarize(history, language, info)
            reason = info.get("reason") or ("ok" if summary else "empty")
    except Exception:
        log.exception("after-chat model call failed for %s", conversation_id)
        summary, mood, risk, raw, reason = None, "unknown", None, None, "error"
    if summary is None and risk is None and reason == "rate_limited" and chat["refundable"]:
        await _set_state(conversation_id, "pending", refund=True)   # the sweep retries; quota is not a failure
        return "pending"

    # Judged: a model's answer was accepted (both prompts need its RISK line) or an answer found a risk. Anything
    # less (an outage, no key, nothing usable, or a summary with no RISK line) saves nothing and the sweep retries, up
    # to MAX_ATTEMPTS; the last attempt keeps any summary it has and tells family the check did not run.
    judged = reason == "ok" or risk is not None
    final = chat["after_chat_attempts"] >= MAX_ATTEMPTS
    quoted = (summary or "") if quote else ""
    try:
        async with get_pool().acquire() as conn, conn.transaction():
            if judged or (final and summary):
                await conn.execute("UPDATE conversation SET summary = $2, mood = $3 WHERE conversation_id = $1::uuid",
                                   conversation_id, summary, mood)
            if not judged and final:
                log.error("conversation %s: no model judged it for risk at the end (%s), so the end-of-chat safety "
                          "check did not run", conversation_id, "no RISK line" if summary else reason)
                await conn.execute("UPDATE conversation SET mood = 'unknown' WHERE conversation_id = $1::uuid "
                                   "AND mood IS NULL", conversation_id)
                await conn.execute(_NOTIFY_INCOMPLETE, u_id, "safety_check_incomplete", INCOMPLETE_TEXT)
            if risk or (judged and mood == "sad"):
                name = await conn.fetchval('SELECT name FROM "user" WHERE u_id = $1', u_id)
            if risk:
                await conversation.summary_backstop(conn, u_id, name or "", conversation_id, quoted, risk)
            elif judged and mood == "sad":
                await _notify_sad_mood(conn, u_id, conversation_id, name or "Reachy user", language)
    except Exception:
        log.exception("saving post-chat results failed for %s", conversation_id)
        if risk:
            await _alert_alone(conversation_id, u_id, quoted, risk)
        # The sweep tries again. After the last attempt the chat stays 'pending', as the claim left it, so the next
        # sweep ends it (_close_unfinished): with the notification unless the alert went.
        state = "pending" if final else "failed"
        await _set_state(conversation_id, state)
        return state

    stored = 0
    if raw and risk is None:   # after_chat_call gives facts only with an explicit RISK none
        try:
            facts = [fact for fact in (memory.validate_fact(item, today=today, source="chat")
                                       for item in raw[:memory.MAX_FACTS])
                     if fact and memory.grounded(fact, "\n".join(patient))]
            async with get_pool().acquire() as conn, conn.transaction():
                stored = await memory.store_facts(conn, u_id, conversation_id, facts)
        except Exception:
            log.exception("storing memory facts failed for %s", conversation_id)
    try:
        async with get_pool().acquire() as conn, conn.transaction():
            await memory.finish_followup(conn, u_id, chat["followup_memory_id"],
                                         [turn["text"] for turn in history if turn["role"] == "reachy"])
    except Exception:
        log.exception("follow-up bookkeeping failed for %s", conversation_id)
    log.info("after-chat %s: reason=%s risk=%s stored=%d", conversation_id, reason, risk, stored)
    state = "done" if judged else "failed"
    await _set_state(conversation_id, state)
    return state
