"""Every 10 minutes: close check-ins the robot never closed, and finish post-chat work that was missed
(background tasks die with every container rebuild)."""

from app.database import get_pool
from app.services import after_chat

ABANDON_MINUTES = 15   # the robot ends a check-in after 5 minutes (reachy_app CHECKIN_MAX); this leaves a margin
BATCH = 20


async def run_after_chat_sweep():
    async with get_pool().acquire() as conn:
        await conn.execute(
            "UPDATE conversation SET ended_at = NOW(), end_reason = 'abandoned', after_chat_state = 'pending' "
            "WHERE ended_at IS NULL AND started_at < NOW() - make_interval(mins => $1)", ABANDON_MINUTES)
        rows = await conn.fetch(
            "SELECT conversation_id, u_id FROM conversation "
            "WHERE after_chat_state IN ('pending','failed') AND after_chat_attempts < $1 "
            "AND ended_at < NOW() - INTERVAL '1 minute' AND ended_at > NOW() - INTERVAL '2 days' "
            "ORDER BY ended_at LIMIT $2", after_chat.MAX_ATTEMPTS, BATCH)
    for row in rows:
        await after_chat.process(str(row["conversation_id"]), row["u_id"])
