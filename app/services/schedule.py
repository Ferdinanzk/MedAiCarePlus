"""Medication schedules: dose times, weekdays, generated intake rows, and days of supply.

`medication.schedule_time` (JSONB) holds the four preset slots as booleans (morning/noon/night/bedtime) plus,
optionally, `custom_times` ("HH:MM" strings) and `weekdays` (ISO numbers, 1 = Monday ... 7 = Sunday; absent means
every day). Other keys (e.g. `before_meals`) are display-only and never create intake rows.
"""

import datetime
import json
import re
from decimal import ROUND_FLOOR, Decimal
from zoneinfo import ZoneInfo

from app.config import DOSE_EARLY_MINUTES, MEDCARE_TIMEZONE

PRESET_TIMES = {"morning": "08:00", "noon": "12:00", "night": "20:00", "bedtime": "22:00"}
PRESET_LABELS = {"morning": "早上", "noon": "中午", "night": "晚上", "bedtime": "睡前"}
ALL_WEEKDAYS = (1, 2, 3, 4, 5, 6, 7)
MAX_TIMES_PER_DAY = 8
HORIZON_DAYS = 30
_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def load(schedule_time) -> dict:
    if isinstance(schedule_time, str):
        try:
            schedule_time = json.loads(schedule_time)
        except (TypeError, ValueError):
            return {}
    return schedule_time if isinstance(schedule_time, dict) else {}


def validate(schedule_time: dict | None) -> dict | None:
    """Reject custom times/weekdays the generator can't use. Returns the value unchanged."""
    if schedule_time is None:
        return None
    custom = schedule_time.get("custom_times", [])
    if not isinstance(custom, list) or not all(isinstance(t, str) and _TIME.match(t) for t in custom):
        raise ValueError("custom_times must be a list of HH:MM times")
    days = schedule_time.get("weekdays")
    if days is not None and (not isinstance(days, list) or not days
                             or not all(isinstance(d, int) and not isinstance(d, bool) and 1 <= d <= 7 for d in days)):
        raise ValueError("weekdays must be a non-empty list of 1 (Monday) to 7 (Sunday)")
    if len(dose_times(schedule_time)) > MAX_TIMES_PER_DAY:
        raise ValueError(f"At most {MAX_TIMES_PER_DAY} dose times per day")
    return schedule_time


def dose_times(schedule_time) -> list[str]:
    schedule = load(schedule_time)
    times = {time for key, time in PRESET_TIMES.items() if schedule.get(key)}
    times.update(t for t in schedule.get("custom_times") or [] if isinstance(t, str) and _TIME.match(t))
    return sorted(times)


def weekdays(schedule_time) -> tuple[int, ...]:
    days = load(schedule_time).get("weekdays")
    if not days:
        return ALL_WEEKDAYS
    return tuple(sorted({d for d in days if isinstance(d, int) and 1 <= d <= 7})) or ALL_WEEKDAYS


def signature(schedule_time) -> tuple:
    """What creates intake rows; a change in it rebuilds the future schedule."""
    return tuple(dose_times(schedule_time)), weekdays(schedule_time)


def slot_label(schedule_time, local_time: datetime.datetime) -> str:
    schedule = load(schedule_time)
    hhmm = local_time.strftime("%H:%M")
    for key, time in PRESET_TIMES.items():
        if time == hhmm and schedule.get(key):
            return PRESET_LABELS[key]
    return hhmm if hhmm in dose_times(schedule) else ""


def daily_doses(schedule_time) -> Decimal:
    """Average doses per day over a week (weekday schedules take fewer)."""
    return Decimal(len(dose_times(schedule_time)) * len(weekdays(schedule_time))) / 7


def parse_date(text: str | None) -> datetime.date | None:
    """A "use before"/end date: ROC 114年08月22日, Gregorian 2025年08月22日, or 2025-08-22 / 2025/08/22 / 2025.08.22."""
    if not text:
        return None
    try:
        cjk = re.search(r"(\d+)\s*年\s*(\d+)\s*月\s*(\d+)\s*日", text)
        if cjk:
            year, month, day = map(int, cjk.groups())
            return datetime.date(year + 1911 if year < 1000 else year, month, day)
        greg = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
        if greg:
            return datetime.date(*map(int, greg.groups()))
    except ValueError:
        return None
    return None


def occurrences(schedule_time, start: datetime.datetime, *, until: datetime.date | None = None,
                horizon_days: int = HORIZON_DAYS) -> list[datetime.datetime]:
    """Dose times after `start` (aware, local) for the next `horizon_days`, ending on `until` if earlier."""
    times = [tuple(map(int, t.split(":"))) for t in dose_times(schedule_time)]
    days = set(weekdays(schedule_time))
    if not times:
        return []
    last = start.date() + datetime.timedelta(days=horizon_days - 1)
    if until is not None:
        last = min(last, max(until, start.date()))
    result = []
    day = start.date()
    while day <= last:
        if day.isoweekday() in days:
            for hour, minute in times:
                moment = datetime.datetime.combine(day, datetime.time(hour, minute), tzinfo=start.tzinfo)
                if moment > start:
                    result.append(moment)
        day += datetime.timedelta(days=1)
    return result


def supply(pills_remaining, units_per_dose, schedule_time, today: datetime.date) -> dict:
    """Daily use and how long the stock lasts. Unscheduled medications have no run-out date."""
    remaining = Decimal(str(pills_remaining or 0))
    daily_units = daily_doses(schedule_time) * Decimal(str(units_per_dose or 1))
    if daily_units <= 0:
        return {"daily_units": 0.0, "days_left": None, "run_out_date": None}
    days_left = (remaining / daily_units).to_integral_value(rounding=ROUND_FLOOR)
    return {"daily_units": float(round(daily_units, 2)), "days_left": int(days_left),
            "run_out_date": (today + datetime.timedelta(days=int(days_left))).isoformat()}


# ── When a dose is due ──────────────────────────────────────────────────────
# One rule for every path that starts a dose (robot task, camera session) or records it as taken (camera commit,
# caregiver confirmation, manual tap, the patient's "I've finished"): a scheduled dose is due from DOSE_EARLY before
# its time, but never before halfway from the same medicine's previous dose. On 3 Oct 2026 three test alerts sent
# just after midnight used that day's 08:00, 12:00 and 20:00 doses, and family confirmations recorded all three as
# taken by 01:18. The halfway bound keeps close doses apart: with 20:00 and 22:00, the 22:00 dose is due from 21:00,
# not from 20:00 together with the 20:00 one. Skipping a later dose stays allowed: it takes no pill.
# This is rule R1 of overdose protection; services/dose_safety.py adds the other three (minimum gap, daily maximum,
# missed doses expire) and the patient's on/off switch, and is what every path calls.
DOSE_EARLY = datetime.timedelta(minutes=DOSE_EARLY_MINUTES)
_LOCAL_TZ = ZoneInfo(MEDCARE_TIMEZONE)


def _aware(when: datetime.datetime) -> datetime.datetime:
    return when if when.tzinfo is not None else when.replace(tzinfo=datetime.timezone.utc)


def _now() -> datetime.datetime:
    """The rule's clock (tests freeze it)."""
    return datetime.datetime.now(datetime.timezone.utc)


def current_time() -> datetime.datetime:
    """The moment the rule uses, for callers that pass it to SQL (due_sql)."""
    return _now()


def due_by(now: datetime.datetime | None = None) -> datetime.datetime:
    """The latest scheduled time that can be due at `now`: an upper bound (a close previous dose makes it later)."""
    return _aware(now or _now()) + DOSE_EARLY


def _lead(scheduled_time: datetime.datetime, previous: datetime.datetime | None) -> datetime.timedelta:
    """How long before its time a dose is due: DOSE_EARLY, or half the gap from the previous dose if that is less."""
    if previous is None or _aware(previous) >= scheduled_time:
        return DOSE_EARLY
    return min(DOSE_EARLY, (scheduled_time - _aware(previous)) / 2)


def due_from(scheduled_time: datetime.datetime | None,
             previous: datetime.datetime | None = None) -> datetime.datetime | None:
    """When a dose becomes due; `previous` is the same medicine's previous dose time (previous_sql). None for a row
    without a time, which is always due."""
    if scheduled_time is None:
        return None
    scheduled_time = _aware(scheduled_time)
    return scheduled_time - _lead(scheduled_time, previous)


def is_due(scheduled_time: datetime.datetime | None, now: datetime.datetime | None = None,
           previous: datetime.datetime | None = None) -> bool:
    """Ad-hoc rows (Take Now) are created at the current time, so they are always due."""
    return scheduled_time is None or _aware(now or _now()) >= due_from(scheduled_time, previous)


def previous_sql(alias: str = "i") -> str:
    """SQL for the same medicine's previous dose time of intake row `alias` (any status, ad-hoc rows included).
    Select it AS previous_time wherever a dose is checked, and pass that to is_due/require_due."""
    return (f"(SELECT MAX(prev_dose.intake_time_stamp) FROM intake prev_dose WHERE prev_dose.u_id = {alias}.u_id "
            f"AND prev_dose.med_id = {alias}.med_id AND prev_dose.intake_time_stamp < {alias}.intake_time_stamp)")


def due_sql(alias: str = "i", now_sql: str = "NOW()") -> str:
    """is_due in SQL for intake row `alias` at the SQL moment `now_sql`, for queries that pick or lease doses."""
    early = f"make_interval(secs => {int(DOSE_EARLY.total_seconds())})"
    return (f"{alias}.intake_time_stamp <= {now_sql} + LEAST({early}, "
            f"COALESCE(({alias}.intake_time_stamp - {previous_sql(alias)}) / 2, {early}))")


def local_iso(when: datetime.datetime | None) -> str | None:
    """A moment as the patient's local time (Asia/Taipei: +08:00), as refusals give it."""
    return _aware(when).astimezone(_LOCAL_TZ).isoformat() if when is not None else None


class DoseRefused(Exception):
    """A dose that overdose protection (services/dose_safety.py) will not start or record. Not a ValueError, so no
    router's generic 409 swallows it: main.py answers 409 with body() for every route, on both ports.

    `at` is the moment judged (now, or when a caregiver's patient was asked); `u_id` is kept for the family alert and
    is not in the body. Subclasses name their reason (`detail`) and add their fields.

    `after_intake` is set on paths with evidence the patient already swallowed something (a camera commit, the
    robot's confirmation request): the sentence then says the dose was not recorded rather than "don't take it".
    `family_alerted` says a double-dose alert went (or had gone within the hour) to family."""

    detail = "dose_refused"
    after_intake = False
    family_alerted = False

    def __init__(self, message: str, *, intk_id: int | None = None, scheduled_time: datetime.datetime | None = None,
                 med_name: str | None = None, language: str | None = None, at: datetime.datetime | None = None,
                 u_id: int | None = None):
        super().__init__(message)
        self.intk_id = intk_id
        self.scheduled_time = _aware(scheduled_time) if scheduled_time is not None else None
        self.med_name = med_name
        self.language = language
        self.at = _aware(at) if at is not None else None
        self.u_id = u_id

    def fields(self) -> dict:
        return {}

    def body(self) -> dict:
        # The patient's sentence (Traditional Chinese or English) and the robot's spoken form live with the rules.
        from app.services import dose_safety

        return dose_safety.refusal_body(self)


class DoseNotDueYet(DoseRefused):
    """A scheduled dose that is not due yet: 409 {"detail": "dose_not_due_yet", "scheduled_time", "due_from", ...}."""

    detail = "dose_not_due_yet"

    def __init__(self, scheduled_time: datetime.datetime, intk_id: int | None = None,
                 previous: datetime.datetime | None = None, **context):
        scheduled_time = _aware(scheduled_time)
        super().__init__(f"dose_not_due_yet: dose {intk_id} is scheduled at {scheduled_time.isoformat()}",
                         intk_id=intk_id, scheduled_time=scheduled_time, **context)
        self.due_from = due_from(self.scheduled_time, previous)

    def fields(self) -> dict:
        return {"due_from": local_iso(self.due_from)}


def require_due(scheduled_time: datetime.datetime | None, intk_id: int | None = None,
                now: datetime.datetime | None = None, previous: datetime.datetime | None = None) -> None:
    """Raise DoseNotDueYet unless the dose is due (rule R1 alone). The app's paths check every rule, under the
    patient's switch, through dose_safety.check."""
    if not is_due(scheduled_time, now, previous):
        raise DoseNotDueYet(scheduled_time, intk_id, previous, at=now)
