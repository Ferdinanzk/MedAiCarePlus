"""Daily: enforce retention for check-in transcripts (robot notice §5) and memory notes (memory notice §4).

Transcript turns are kept 30 days; turns quoted in a safety alert, 180 days. Conversation rows (summary and
mood) and memory notes stay until the patient deletes them or the account; event notes go 30 days after the
event; deletion tombstones go after 7 days. Also run by replay_ledger after a restore."""

from app import config
from app.database import get_pool

TRANSCRIPT_DAYS = 30
SAFETY_QUOTE_DAYS = 180
EVENT_KEEP_DAYS = 30
TOMBSTONE_DAYS = 7


async def run_retention(conn) -> None:
    await conn.execute(
        "DELETE FROM conversation_turn WHERE "
        "(NOT flagged AND created_at < NOW() - make_interval(days => $1)) OR "
        "(flagged AND created_at < NOW() - make_interval(days => $2))",
        TRANSCRIPT_DAYS, SAFETY_QUOTE_DAYS)
    await conn.execute(
        "DELETE FROM patient_memory WHERE kind = 'event' AND event_date < (NOW() AT TIME ZONE $1)::date - $2::int",
        config.MEDCARE_TIMEZONE, EVENT_KEEP_DAYS)
    await conn.execute(
        "DELETE FROM patient_memory_deleted WHERE deleted_at < NOW() - make_interval(days => $1)", TOMBSTONE_DAYS)


async def purge_old_transcripts():
    async with get_pool().acquire() as conn:
        await run_retention(conn)
