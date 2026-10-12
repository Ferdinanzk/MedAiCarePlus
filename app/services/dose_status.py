"""get_today_dose_status: the tool a check-in reply model calls to learn how today's medicine stands.

The reply model (services/conversation.py, through OpenRouter) is offered one tool. It takes no arguments: the
conversation already says whose doses and which reminder, so the model can't ask about anyone else's. The answer:

    {"status": one of STATUSES, "last_taken_time": ISO local time or null, "next_allowed_time": ISO local time or null}

Which doses it reports: the reminder task's own doses (the slot the robot just watched), or, for a conversation-only
task ("Talk to Reachy now", no doses), the latest of today's slots that is due (DOSE_EARLY ahead of its time counts),
else today's first slot still to come. A slot is the doses scheduled within SLOT_WINDOW of its first one, as the
manual task groups them (here: within SLOT_WINDOW of the slot's time).

Each dose gets a status, and the slot the first one in PRECEDENCE that any of its doses has: a dose that may be taken
now wins (the patient is encouraged to take it), then one waiting for family, then a taken one; refusals come after,
so a slot with one dose taken and another refused only for later still gets its praise. The refusals are
dose_safety's own verdicts (overdose protection), judged now: a dose they refuse is never reported as takeable.

Times come from the same verdicts: a too-soon or daily-maximum refusal gives next_allowed_time (and too-soon also the
taken dose it counts from), not-due gives the time the dose may start.
"""

import datetime
import logging

from app.config import MEDCARE_TIMEZONE
from app.services import dose_safety, schedule

log = logging.getLogger(__name__)

TOOL_NAME = "get_today_dose_status"
STATUSES = ("taken", "pending_allowed", "pending_confirmation", "refused_too_soon", "refused_max_reached",
            "refused_not_due", "refused_expired", "skipped", "none")
TOOL = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": ("Fetches current medication schedule status for today: whether the patient's current dose "
                        "was taken, is waiting for family to confirm it, may be taken now, or must not be taken now."),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}
# tool_choice that makes the model call this tool (the conversation's first call, conversation.open_dose_status).
FORCE = {"type": "function", "function": {"name": TOOL_NAME}}
# The tool's answer when the lookup itself failed: the prompt tells the model not to bring medicine up then.
UNAVAILABLE = {"status": "unknown", "last_taken_time": None, "next_allowed_time": None}

# A dose that may be taken now first; refusals after the taken ones (see the module docstring).
PRECEDENCE = ("pending_allowed", "pending_confirmation", "taken", "refused_too_soon", "refused_max_reached",
              "refused_not_due", "refused_expired", "skipped")
REFUSED = {"dose_too_soon": "refused_too_soon", "daily_max_reached": "refused_max_reached",
           "dose_not_due_yet": "refused_not_due", "dose_expired": "refused_expired"}
OPEN = ("pending", "missed")
SLOT_WINDOW = datetime.timedelta(minutes=5)

TASK_SQL = "SELECT t.intk_ids FROM reachy_task t WHERE t.task_id = $1::uuid AND t.u_id = $2"
CONVERSATION_TASK_SQL = "SELECT c.task_id FROM conversation c WHERE c.conversation_id = $1::uuid AND c.u_id = $2"
# Today's doses of active medicines ($2: the moment asked, $3: the patient's time zone).
TODAY_SQL = ("SELECT i.intk_id, i.intake_time_stamp FROM intake i JOIN medication m ON m.med_id = i.med_id "
             "WHERE i.u_id = $1 AND COALESCE(m.is_active, TRUE) "
             "AND (i.intake_time_stamp AT TIME ZONE $3::text)::date = ($2::timestamptz AT TIME ZONE $3::text)::date "
             "ORDER BY i.intake_time_stamp, i.intk_id")
DOSES_SQL = ("SELECT i.intk_id, i.intake_stats, i.intake_time_stamp, i.actual_intake_time FROM intake i "
             "WHERE i.u_id = $1 AND i.intk_id = ANY($2::int[]) ORDER BY i.intake_time_stamp, i.intk_id")


def _aware(when: datetime.datetime) -> datetime.datetime:
    return when if when.tzinfo else when.replace(tzinfo=datetime.timezone.utc)


def slot_of(rows, at: datetime.datetime) -> list[int]:
    """The intake ids of the slot a conversation-only task reports (module docstring), from today's rows
    ({"intk_id", "intake_time_stamp"}, in time order)."""
    rows = [row for row in rows if row["intake_time_stamp"] is not None]
    if not rows:
        return []
    due = [row for row in rows if _aware(row["intake_time_stamp"]) <= at + schedule.DOSE_EARLY]
    anchor = _aware((due[-1] if due else rows[0])["intake_time_stamp"])
    return [row["intk_id"] for row in rows if abs(_aware(row["intake_time_stamp"]) - anchor) < SLOT_WINDOW]


def dose_status(row, refused) -> str:
    """One dose's status: its intake_stats, and for an open dose overdose protection's verdict now."""
    stats = row["intake_stats"]
    if stats == "taken":
        return "taken"
    if stats == "pending_confirmation":
        return "pending_confirmation"
    if stats == "skipped":
        return "skipped"
    if refused is None:
        return "pending_allowed"
    return REFUSED.get(getattr(refused, "detail", ""), "refused_not_due")


def summarize(rows, verdicts: dict) -> dict:
    """The tool's answer for a slot: rows from DOSES_SQL, verdicts {intk_id: refusal or None} for its open doses."""
    if not rows:
        return {"status": "none", "last_taken_time": None, "next_allowed_time": None}
    doses = [(dose_status(row, verdicts.get(row["intk_id"])), row, verdicts.get(row["intk_id"])) for row in rows]
    status = next(status for status in PRECEDENCE if any(dose[0] == status for dose in doses))
    refused = next((dose[2] for dose in doses if dose[0] == status), None)
    taken = [row["actual_intake_time"] for _, row, _ in doses
             if row["intake_stats"] == "taken" and row.get("actual_intake_time") is not None]
    last = max(taken, default=None)
    if last is None and isinstance(refused, dose_safety.DoseTooSoon):
        last = refused.last_taken_at
    following = None
    if isinstance(refused, (dose_safety.DoseTooSoon, dose_safety.DailyMaxReached)):
        following = refused.next_allowed_at
    elif isinstance(refused, schedule.DoseNotDueYet):
        following = refused.due_from
    return {"status": status, "last_taken_time": schedule.local_iso(last),
            "next_allowed_time": schedule.local_iso(following)}


async def lookup(conn, u_id: int, task_id: str | None, at: datetime.datetime | None = None) -> dict:
    """The tool's answer for this patient and reminder task (module docstring)."""
    at = _aware(at or schedule.current_time())
    ids: list[int] = []
    if task_id is not None:
        ids = [int(i) for i in (await conn.fetchval(TASK_SQL, str(task_id), u_id) or [])]
    if not ids:
        ids = slot_of(await conn.fetch(TODAY_SQL, u_id, at, MEDCARE_TIMEZONE), at)
    if not ids:
        return summarize([], {})
    rows = [dict(row) for row in await conn.fetch(DOSES_SQL, u_id, ids)]
    open_ids = [row["intk_id"] for row in rows if row["intake_stats"] in OPEN]
    verdicts = await dose_safety.verdicts(conn, u_id, open_ids, at=at) if open_ids else {}
    return summarize(rows, verdicts)


async def for_conversation(conn, u_id: int, conversation_id: str, at: datetime.datetime | None = None) -> dict:
    """lookup() for a check-in conversation's reminder task."""
    task_id = await conn.fetchval(CONVERSATION_TASK_SQL, str(conversation_id), u_id)
    return await lookup(conn, u_id, None if task_id is None else str(task_id), at)
