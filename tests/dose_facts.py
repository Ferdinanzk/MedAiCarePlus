"""dose_safety.FACTS_SQL answered from Python data, for the tests' fake databases.

The same meaning as the SQL: the same patient's and medicine's previous dose time as of the moment judged (an ad-hoc
dose made after it left out) and next scheduled dose time (ad-hoc doses left out), the other dose nearest to the
moment judged that was taken or waits for a caregiver's answer (at the time the robot asked), and the other such
doses scheduled on the row's local day.
"""

import datetime
from zoneinfo import ZoneInfo

from app.services import dose_safety


def is_facts(query: str) -> bool:
    return query.startswith(dose_safety.FACTS_SQL)


def is_lock(query: str) -> bool:
    """dose_safety.LOCK_SQL: recording paths lock the medicines' rows in a statement of their own first."""
    return query == dose_safety.LOCK_SQL


def _claims(confirmations) -> list[dict]:
    if confirmations is None:
        return []
    return list(confirmations.values()) if isinstance(confirmations, dict) else list(confirmations)


def rows(args, intakes: dict, meds: dict, *, protection=True, language=None, confirmations=None) -> list[dict]:
    """intakes: {intk_id: {"u_id", "med_id", "intake_stats", "intake_time_stamp", "actual_intake_time"}} (u_id
    defaults to the patient asked about); meds: {med_id: {"med_name", "schedule_time", "min_interval_minutes",
    "max_daily_doses"}}; protection: a bool, or {u_id: bool} (missing: on); confirmations: dose_confirmation rows
    ({"u_id", "intk_ids", "resolution", "created_at"}), as a dict or a list."""
    u_id, ids, at, zone = args
    zone = ZoneInfo(zone)
    claims = _claims(confirmations)
    result = []
    for intk_id, row in sorted(intakes.items(), key=lambda item: (item[1]["intake_time_stamp"], item[0])):
        owner = row.get("u_id", u_id)
        if intk_id not in ids or owner != u_id:
            continue
        stamp = row["intake_time_stamp"]
        same = {other_id: other for other_id, other in intakes.items()
                if other.get("u_id", u_id) == owner and other["med_id"] == row["med_id"]}

        def claimed(other_id, other):
            """When the robot asked about this waiting dose, at or before the moment judged; None otherwise."""
            if other["intake_stats"] != "pending_confirmation":
                return None
            asked = [claim["created_at"] for claim in claims
                     if claim.get("u_id", owner) == owner and other_id in claim["intk_ids"]
                     and claim.get("resolution") is None and claim["created_at"] <= at]
            return min(asked, default=None)

        doses = []      # (time it went down, waiting for family?, scheduled time)
        for other_id, other in same.items():
            if other_id == intk_id:
                continue
            if other["intake_stats"] == "taken" and isinstance(other.get("actual_intake_time"), datetime.datetime):
                doses.append((other["actual_intake_time"], False, other["intake_time_stamp"]))
            elif (asked := claimed(other_id, other)) is not None:
                doses.append((asked, True, other["intake_time_stamp"]))
        counted = [other for other_id, other in same.items() if other_id != intk_id
                   and (other["intake_stats"] == "taken" or claimed(other_id, other) is not None)]
        nearest = min(doses, key=lambda d: (abs((d[0] - at).total_seconds()), -d[0].timestamp()), default=None)
        day = stamp.astimezone(zone).date()
        med = meds.get(row["med_id"], {})
        result.append({
            "intk_id": intk_id, "u_id": owner, "med_id": row["med_id"], "intake_stats": row["intake_stats"],
            "intake_time_stamp": stamp,
            "previous_time": max((o["intake_time_stamp"] for o in same.values() if o["intake_time_stamp"] < stamp
                                  and not (dose_safety.is_ad_hoc(o["intake_time_stamp"])
                                           and o["intake_time_stamp"] > at)), default=None),
            "next_time": min((o["intake_time_stamp"] for o in same.values() if o["intake_time_stamp"] > stamp
                              and not dose_safety.is_ad_hoc(o["intake_time_stamp"])), default=None),
            "med_name": med.get("med_name"), "schedule_time": med.get("schedule_time"),
            "min_interval_minutes": med.get("min_interval_minutes"), "max_daily_doses": med.get("max_daily_doses"),
            "protection": protection.get(owner, True) if isinstance(protection, dict) else protection,
            "language": language,
            "last_taken_at": nearest[0] if nearest else None,
            "last_taken_pending": nearest[1] if nearest else False,
            "taken_that_day": sum(1 for other in counted if other["intake_time_stamp"].astimezone(zone).date() == day),
        })
    return result
