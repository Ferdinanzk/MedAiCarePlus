"""Every 5 minutes: close check-ins the robot never closed, and finish post-chat work (summary, the summary's risk
backstop, memory facts) that was missed or failed: background tasks die with every container rebuild, and an
outage leaves a chat pending. after_chat.process does the work, the same as after /end. A chat still unfinished
after_chat.RETRY_DAYS after it ended is ended without a model call (after_chat.end_stale), never silently."""

import logging

from app.database import get_pool
from app.services import after_chat, monitor_service

log = logging.getLogger(__name__)

# No turn for this long and still open: the robot was switched off or lost the network. Counted from the last turn,
# never from the start, so a live chat is never closed under the patient. A companion session (robot app 0.9.0)
# can go much longer without a turn (the patient in view and quiet; a live one's GPT-Live paused): it stays open
# while the patient's robot holds its camera session here (_robots_present), which a robot that went away loses
# after monitor_service.IDLE_SESSION_SECONDS without a frame.
ABANDON_MINUTES = 15
# The companion exception covers only the patient's newest open conversation, and only for this long without a turn:
# an orphaned one (a worker restart mid-chat, no /end) must not stay open through the robot's later sessions.
COMPANION_ABANDON_HOURS = 2
BATCH = 20


def _robots_present() -> list[int]:
    """Patients whose robot holds an open camera session in this process (monitor_service.registry). A companion
    session keeps its observe session open until it ends (companion spec 4.2), so its conversation is not abandoned."""
    return sorted({session.u_id for session in list(monitor_service.registry.sessions.values())
                   if session.client_type == "reachy" and not session.ended})


async def run_after_chat_sweep():
    async with get_pool().acquire() as conn:
        closed = await conn.fetch(
            "UPDATE conversation c SET ended_at = NOW(), end_reason = 'abandoned', after_chat_state = 'pending' "
            "WHERE c.ended_at IS NULL AND GREATEST(c.started_at, "
            "  COALESCE((SELECT MAX(t.created_at) FROM conversation_turn t "
            "            WHERE t.conversation_id = c.conversation_id), c.started_at)) "
            "  < NOW() - make_interval(mins => $1) "
            "AND NOT (c.companion AND c.u_id = ANY($2::int[]) "
            "  AND NOT EXISTS (SELECT 1 FROM conversation newer WHERE newer.u_id = c.u_id "
            "                  AND newer.started_at > c.started_at) "
            "  AND GREATEST(c.started_at, "
            "    COALESCE((SELECT MAX(t.created_at) FROM conversation_turn t "
            "              WHERE t.conversation_id = c.conversation_id), c.started_at)) "
            "    >= NOW() - make_interval(hours => $3)) "
            "RETURNING c.conversation_id", ABANDON_MINUTES, _robots_present(), COMPANION_ABANDON_HOURS)
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
