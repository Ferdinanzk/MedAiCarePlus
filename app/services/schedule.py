"""Medication schedules: dose times, weekdays, generated intake rows, and days of supply.

`medication.schedule_time` (JSONB) holds the four preset slots as booleans (morning/noon/night/bedtime) plus,
optionally, `custom_times` ("HH:MM" strings) and `weekdays` (ISO numbers, 1 = Monday ... 7 = Sunday; absent means
every day). Other keys (e.g. `before_meals`) are display-only and never create intake rows.
"""

import datetime
import json
import re
from decimal import ROUND_FLOOR, Decimal

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
