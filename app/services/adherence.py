"""Adherence over explicit date ranges: one rule for the history page, the dashboard and the weekly summary.

A dose counts once it is due: scheduled in the past and either resolved (taken, skipped, missed, awaiting a
caregiver) or still pending more than GRACE after its time. Future doses never count. Only 'taken' is adherent;
a dose awaiting caregiver confirmation is not (yet).
"""

import datetime

from app.config import MEDCARE_TIMEZONE

GRACE = datetime.timedelta(hours=1)
STREAK_LOOKBACK_DAYS = 366
STATUSES = ("taken", "missed", "skipped", "awaiting", "overdue")


async def daily_counts(conn, u_id: int, start: datetime.datetime, end: datetime.datetime,
                       now: datetime.datetime) -> list[dict]:
    """Per local day in [start, end): counts of due doses by outcome."""
    rows = await conn.fetch(
        """
        SELECT (i.intake_time_stamp AT TIME ZONE $5)::date AS day,
               COUNT(*) FILTER (WHERE i.intake_stats = 'taken')                AS taken,
               COUNT(*) FILTER (WHERE i.intake_stats = 'missed')               AS missed,
               COUNT(*) FILTER (WHERE i.intake_stats = 'skipped')              AS skipped,
               COUNT(*) FILTER (WHERE i.intake_stats = 'pending_confirmation') AS awaiting,
               COUNT(*) FILTER (WHERE i.intake_stats = 'pending')              AS overdue
        FROM intake i
        WHERE i.u_id = $1
          AND i.intake_time_stamp >= $2 AND i.intake_time_stamp < $3
          AND i.intake_time_stamp <= $4
          AND (i.intake_stats <> 'pending' OR i.intake_time_stamp <= $6)
        GROUP BY day
        ORDER BY day
        """,
        u_id, start, end, now, MEDCARE_TIMEZONE, now - GRACE)
    return [{"day": row["day"], **{status: int(row[status]) for status in STATUSES}} for row in rows]


def totals(days: list[dict]) -> dict:
    counts = {status: sum(day[status] for day in days) for status in STATUSES}
    due = sum(counts.values())
    counts["due"] = due
    counts["adherence"] = round(counts["taken"] * 100 / due, 1) if due else None
    return counts


def complete(day: dict) -> bool:
    return day["taken"] > 0 and day["taken"] == sum(day[status] for status in STATUSES)


def streak(days: list[dict], today: datetime.date) -> int:
    """Consecutive days, back from today, on which every due dose was taken. Days without doses are skipped."""
    count = 0
    for day in sorted((d for d in days if d["day"] <= today), key=lambda d: d["day"], reverse=True):
        if not complete(day):
            break
        count += 1
    return count
