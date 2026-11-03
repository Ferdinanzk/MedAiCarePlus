"""System-wide admin switch that stops LINE medication reminders.

The state lives in memory, so the senders can ask without touching the database, and in ``ops_state`` (the row exists
only while paused), so a restart keeps it. Every change is also written to ``line_switch_log``.
"""
import datetime
import json
import logging

log = logging.getLogger(__name__)

MED_REMINDER = "med_reminder"
OTHER = "other"
PAUSABLE_KINDS = frozenset({MED_REMINDER})

STATE_KEY = "line_reminders_paused"

_state = {"paused": False, "since": None, "by": None}


def blocks(kind: str) -> bool:
    return _state["paused"] and kind in PAUSABLE_KINDS


def state() -> dict:
    return dict(_state)


async def load(conn) -> None:
    raw = await conn.fetchval("SELECT value FROM ops_state WHERE key = $1", STATE_KEY)
    if raw is None:
        _state.update(paused=False, since=None, by=None)
        return
    try:
        data = json.loads(raw)
        since = datetime.datetime.fromisoformat(data["since"])
        by = data.get("by")
    except (ValueError, KeyError, TypeError):
        # A paused row we cannot read still means someone paused: stay paused rather than send reminders
        log.warning("ops_state %s is unreadable; keeping LINE medication reminders paused", STATE_KEY)
        _state.update(paused=True, since=None, by=None)
        return
    _state.update(paused=True, since=since, by=by)


async def set_paused(conn, paused: bool, by_u_id: int, now: datetime.datetime) -> dict:
    async with conn.transaction():
        await conn.execute(
            "INSERT INTO line_switch_log (paused, changed_by, changed_at) VALUES ($1, $2, $3)",
            paused, by_u_id, now)
        if paused:
            # DO NOTHING: pausing again while paused keeps the first since/by
            await conn.execute(
                "INSERT INTO ops_state (key, value) VALUES ($1, $2) ON CONFLICT (key) DO NOTHING",
                STATE_KEY, json.dumps({"since": now.isoformat(), "by": by_u_id}))
        else:
            await conn.execute("DELETE FROM ops_state WHERE key = $1", STATE_KEY)
    if not paused:
        _state.update(paused=False, since=None, by=None)
    elif not _state["paused"]:
        _state.update(paused=True, since=now, by=by_u_id)
    return state()
