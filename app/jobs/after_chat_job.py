"""Every 5 minutes: close check-ins the robot never closed, and finish post-chat work (summary, the summary's risk
backstop, memory facts) that was missed or failed: background tasks die with every container rebuild, and an
outage leaves a chat pending. after_chat.process does the work, the same as after /end. A chat still unfinished
after_chat.RETRY_DAYS after it ended is ended without a model call (after_chat.end_stale), never silently."""

import logging

from app.database import get_pool
from app.services import after_chat

log = logging.getLogger(__name__)

# No turn for this long and still open: the robot was switched off or lost the network. The robot ends a check-in
# after 5 minutes (reachy_app CHECKIN_MAX); counted from the last turn, never from the start, so a live chat is
# never closed under the patient.
ABANDON_MINUTES = 15
BATCH = 20


async def run_after_chat_sweep():
    async with get_pool().acquire() as conn:
        closed = await conn.fetch(
            "UPDATE conversation c SET ended_at = NOW(), end_reason = 'abandoned', after_chat_state = 'pending' "
            "WHERE c.ended_at IS NULL AND GREATEST(c.started_at, "
            "  COALESCE((SELECT MAX(t.created_at) FROM conversation_turn t "
            "            WHERE t.conversation_id = c.conversation_id), c.started_at)) "
            "  < NOW() - make_interval(mins => $1) "
            "RETURNING c.conversation_id", ABANDON_MINUTES)
        try:
            stale = await after_chat.end_stale(conn)
        except Exception:
            log.exception("ending stale post-chat work failed")
            stale = 0
        # Abandoned chats at once; others a minute after /end, whose own background task normally does them.
        # 'pending' at any attempt count: one whose last attempt never finished is ended by process(), never silently.
        rows = await conn.fetch(
            "SELECT conversation_id, u_id FROM conversation "
            "WHERE (after_chat_state = 'pending' OR (after_chat_state = 'failed' AND after_chat_attempts < $1)) "
            "AND (end_reason = 'abandoned' OR ended_at < NOW() - INTERVAL '1 minute') "
            "AND ended_at > NOW() - make_interval(days => $3) "
            "ORDER BY ended_at LIMIT $2", after_chat.MAX_ATTEMPTS, BATCH, after_chat.RETRY_DAYS)
    if closed:
        log.info("closed %d abandoned conversation(s)", len(closed))
    if stale:
        log.warning("ended %d conversation(s) whose post-chat work never finished", stale)
    for row in rows:
        try:   # one bad chat never stops the others
            await after_chat.process(str(row["conversation_id"]), row["u_id"])
        except Exception:
            log.exception("post-chat work for conversation %s failed", row["conversation_id"])
