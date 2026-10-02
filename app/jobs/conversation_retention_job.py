"""Daily: enforce the robot notice's retention (§5) for check-in transcripts.

Transcript turns are kept 30 days; turns quoted in a safety alert, 180 days. Conversation rows (summary and mood)
stay until the patient deletes them or the account.
"""

from app.database import get_pool

TRANSCRIPT_DAYS = 30
SAFETY_QUOTE_DAYS = 180


async def purge_old_transcripts():
    async with get_pool().acquire() as conn:
        await conn.execute(
            "DELETE FROM conversation_turn WHERE "
            "(NOT flagged AND created_at < NOW() - make_interval(days => $1)) OR "
            "(flagged AND created_at < NOW() - make_interval(days => $2))",
            TRANSCRIPT_DAYS, SAFETY_QUOTE_DAYS)
