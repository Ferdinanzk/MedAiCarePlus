"""The Scan page's save rules (frontend_source/src/lib/scan.ts), ported line by line so the server builds the very
drafts and POST /api/medications bodies the page builds from an OCR result. Pure functions; dicts in the TypeScript
shapes; `today` is an ISO 'YYYY-MM-DD' string. tests/scan_parity.json, made from scan.ts by
frontend_source/tests/scan_parity.gen.ts, holds the two equal (tests/test_scan_rules.py and scan.unit.ts).

JavaScript semantics kept on purpose: Math.round rounds halves up; numbers print as String() prints them; Number()
parses text as JS does; a key that is `undefined` in TypeScript is left out of the dict (JSON.stringify drops it),
while `null` stays None; new Date(y, m - 1, d + days) rolls an impossible date over; dates compare as text; \\s and
trim() use JavaScript's whitespace."""

import math
import re
import unicodedata
from decimal import Decimal

DOSE_FORMS = ("solid_oral", "liquid", "inhaler", "injection", "topical", "other")
TIME_SLOTS = ("morning", "noon", "night", "bedtime")
MEAL_SLOTS = ("before_meals", "after_meals")
SLOTS = TIME_SLOTS + MEAL_SLOTS
MAX_TIMES = 8
# scan.ts's own constant, not config.DOSE_EARLY_MINUTES: the page sends 120 whatever the server's environment says.
DOSE_EARLY_MINUTES = 120

# JavaScript's WhiteSpace and LineTerminator: what trim() removes and what \s matches (Python's differ: they add
# \x1c-\x1f and \x85, and lack \ufeff).
_JS_SPACE = "\t\n\v\f\r \u00a0\u1680" + "".join(map(chr, range(0x2000, 0x200B))) + "\u2028\u2029\u202f\u205f\u3000\ufeff"
# JS's \d is ASCII and its $ is the very end (Python's \d takes any digit and its $ allows a final newline).
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_CLOCK = re.compile(r"[0-9]{2}:[0-9]{2}")
_NAME_GAPS = re.compile(f"[{re.escape(_JS_SPACE)}.\uff0e\u00b7\u30fb]+")   # . ．·・
# What Number() accepts; Python's float() and int() also take '1_000', 'inf', signs and other scripts' digits.
_DECIMAL = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_RADIX = re.compile(r"0[xX][0-9a-fA-F]+|0[oO][0-7]+|0[bB][01]+")

_ABSENT = object()   # TypeScript's undefined: the key is left out


def _trim(text: str) -> str:
    return text.strip(_JS_SPACE)


def _is_iso(value) -> bool:
    return isinstance(value, str) and _ISO_DATE.fullmatch(value) is not None


def _js_number(text: str) -> float:
    """Number(text): JS whitespace trimmed, '' is 0, a decimal literal, Infinity, 0x/0o/0b; anything else NaN."""
    text = _trim(text)
    if text == "":
        return 0.0
    if _DECIMAL.fullmatch(text):
        return float(text)
    if text in ("Infinity", "+Infinity", "-Infinity"):
        return float("-inf") if text[0] == "-" else float("inf")
    if _RADIX.fullmatch(text):
        return float(int(text[2:], {"x": 16, "o": 8, "b": 2}[text[1].lower()]))
    return math.nan


def _is_integer(value) -> bool:
    """Number.isInteger."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) \
        and math.floor(value) == value


def _num(value):
    """A number as JSON.stringify writes it: a whole float becomes an int (15, never 15.0)."""
    if isinstance(value, float) and math.isfinite(value) and value == math.floor(value) and abs(value) < 2 ** 53:
        return int(value)
    return value


def _js_round(value: float):
    """Math.round: the nearest integer, halves toward +infinity (Python's round() takes halves to even)."""
    if not math.isfinite(value):
        return value
    floor = math.floor(value)
    return floor + 1 if value - floor >= 0.5 else floor


def _round2(value: float):
    return _num(_js_round(value * 100) / 100)


def _js_str(value) -> str:
    """String(value) for the values a scan holds: JS's shortest round-trip form of a number."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if not isinstance(value, float):
        return str(value)
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    _, digit_tuple, exponent = Decimal(repr(abs(value))).normalize().as_tuple()
    digits = "".join(map(str, digit_tuple))
    k, n = len(digits), len(digits) + exponent   # Number::toString's k digits and point position n
    if k <= n <= 21:
        return sign + digits + "0" * (n - k)
    if 0 < n <= 21:
        return sign + digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return sign + "0." + "0" * -n + digits
    mantissa = digits if k == 1 else digits[0] + "." + digits[1:]
    return f"{sign}{mantissa}e{'+' if n - 1 >= 0 else '-'}{abs(n - 1)}"


def _na(value):
    """Missing ('', blank or 'N/A') is absent; anything else comes back unchanged, untrimmed."""
    if not value or _trim(value) == "" or _trim(value) == "N/A":
        return _ABSENT
    return value


def _or(value, default):
    """`value ?? default` for a value that may be absent."""
    return default if value is _ABSENT or value is None else value


def _days_from_civil(year: int, month: int, day: int) -> int:
    year -= month <= 2
    era = year // 400
    yoe = year - era * 400
    doy = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    return era * 146097 + yoe * 365 + yoe // 4 - yoe // 100 + doy - 719468


def _civil_from_days(days: int) -> tuple[int, int, int]:
    days += 719468
    era = days // 146097
    doe = days - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    month = mp + (3 if mp < 10 else -9)
    return yoe + era * 400 + (month <= 2), month, doy - (153 * mp + 2) // 5 + 1


def _add_days(iso: str, days) -> str:
    """addDays: new Date(y, m - 1, d + days) in JS, which rolls an impossible date over (2026-02-30 is 2 March),
    truncates a fractional day, and reads years 0-99 as 1900-1999. Not limited to Python's years 1-9999."""
    year, month, day = (int(part) for part in iso.split("-"))
    if 0 <= year <= 99:
        year += 1900
    year += (month - 1) // 12
    month = (month - 1) % 12 + 1
    y, m, d = _civil_from_days(_days_from_civil(year, month, 1) + math.trunc(day + days) - 1)
    return f"{y}-{m:02d}-{d:02d}"


def medicines_of(result: dict) -> list[dict]:
    medications = result.get("medications")
    return medications if isinstance(medications, list) and len(medications) > 0 else [result]


def _times_per_day_of(draft: dict) -> int:
    return sum(1 for slot in TIME_SLOTS if draft["slots"].get(slot)) + len(draft["customTimes"])


def course_start(result: dict, today: str) -> str:
    for date in (result.get("date_dispensed"), result.get("visit_date")):
        if date and _is_iso(date):
            return date
    return today


def last_day_for(start: str, days, use_before=None) -> str:
    if days and days > 0:
        return _add_days(start, days - 1)
    return use_before if use_before and _is_iso(use_before) else ""


def _count_text(count: int) -> str:
    return _js_str(count) if count > 0 else ""


def _printed_maximum(med: dict):
    return _or(med.get("max_per_day"), med.get("times_per_day"))


def draft_from(med: dict, result: dict, today: str) -> dict:
    schedule = med.get("schedule_time") or {}
    slots = {slot: schedule.get(slot) is True for slot in SLOTS}
    custom_times = [time for time in (schedule.get("custom_times") or [])
                    if isinstance(time, str) and _CLOCK.fullmatch(time)]
    days = med.get("days")
    dose_form = med.get("dose_form") if med.get("dose_form") in DOSE_FORMS else "solid_oral"
    # An absent `stock` (an older server) falls back to the printed count, for tablets only; `stock: null` does not.
    if "stock" in med:
        stock = med["stock"]
    else:
        stock = med.get("pill_count") if dose_form == "solid_oral" else None
    as_needed = med.get("as_needed") is True
    last_day = last_day_for(course_start(result, today), days, result.get("use_before"))
    maximum = _printed_maximum(med)
    if as_needed:
        times_per_day = _count_text(_times_per_day_of({"slots": slots, "customTimes": custom_times}))
    else:
        times_per_day = _js_str(med["times_per_day"]) if med.get("times_per_day") else ""
    return {
        "include": not (last_day != "" and last_day < today),
        "name": _or(_na(med.get("med_name")), ""),
        "dosage": _or(_na(med.get("dosage")), ""),
        "doseForm": dose_form,
        "stock": _js_str(stock) if stock is not None else "",
        "unitsPerDose": _js_str(_or(med.get("units_per_dose"), 1)),
        "timesPerDay": times_per_day,
        "maxDaily": _js_str(maximum) if as_needed and maximum else "",
        "days": _js_str(days) if days else "",
        "lastDay": last_day,
        "slots": slots,
        "customTimes": custom_times,
        "asNeeded": as_needed,
        "source": med,
        "status": "idle",
    }


def same_name(a: str, b: str) -> bool:
    def key(name: str) -> str:
        return _NAME_GAPS.sub("", unicodedata.normalize("NFKC", name).upper())
    return key(a) != "" and key(a) == key(b)


def mark_duplicates(drafts: list[dict], active_names: list[str]) -> list[dict]:
    marked = []
    for draft in drafts:
        if draft["status"] == "saved" or draft.get("duplicate") or \
                not any(same_name(name, draft["name"]) for name in active_names):
            marked.append(draft)
        else:
            marked.append({**draft, "duplicate": True, "include": False})
    return marked


def draft_problem(draft: dict, today: str | None = None) -> str | None:
    if not _trim(draft["name"]):
        return "name"
    stock = 0 if _trim(draft["stock"]) == "" else _js_number(draft["stock"])
    if not math.isfinite(stock) or stock < 0 or stock >= 1_000_000:
        return "stock"
    units = 1 if _trim(draft["unitsPerDose"]) == "" else _js_number(draft["unitsPerDose"])
    if not math.isfinite(units) or units <= 0 or units >= 100:
        return "unitsPerDose"
    if stock < units:
        return "stockLow"
    if draft["asNeeded"] and _trim(draft["maxDaily"]) != "":
        most = _js_number(draft["maxDaily"])
        if not _is_integer(most) or most < 1 or most > 24:
            return "maxDaily"
    if _trim(draft["days"]) != "":
        days = _js_number(draft["days"])
        if not _is_integer(days) or days < 1 or days > 365:
            return "days"
    if _times_per_day_of(draft) > MAX_TIMES:
        return "tooManyTimes"
    if draft["lastDay"] != "" and not _is_iso(draft["lastDay"]):
        return "lastDay"
    if today and draft["lastDay"] != "" and draft["lastDay"] < today:
        return "lastDayPast"
    return None


def dose_limits(draft: dict) -> dict:
    med = draft["source"]
    interval = med.get("interval_hours")
    if draft["asNeeded"]:
        most = _js_number(draft["maxDaily"])
        return {
            "max_daily_doses": _num(min(24, max(1, most)))
            if _trim(draft["maxDaily"]) != "" and _is_integer(most) else None,
            "min_interval_minutes": _num(min(2880, max(30, interval * 60))) if interval else None,
        }
    limits = {}
    max_per_day = med.get("max_per_day")
    if max_per_day and max_per_day > _times_per_day_of(draft):
        limits["max_daily_doses"] = _num(min(24, max_per_day))
    if interval and interval > 24:
        limits["min_interval_minutes"] = _num(min(2880, max(30, interval * 60 - DOSE_EARLY_MINUTES)))
    return limits


def medication_payload(draft: dict, result: dict) -> dict:
    med = draft["source"]
    stock = 0 if _trim(draft["stock"]) == "" else _round2(_js_number(draft["stock"]))
    units = 1 if _trim(draft["unitsPerDose"]) == "" else _round2(_js_number(draft["unitsPerDose"]))
    slots = draft["slots"]
    has_schedule = _times_per_day_of(draft) > 0 or any(slots.get(slot) for slot in MEAL_SLOTS)
    days = None if _trim(draft["days"]) == "" else _num(_js_number(draft["days"]))
    times_per_day = _times_per_day_of(draft) or med.get("times_per_day")
    use_before = result.get("use_before")
    printed_use_before = use_before if use_before and _is_iso(use_before) else _ABSENT
    last_day = draft["lastDay"]
    meta = {
        "course_end": last_day if last_day and last_day != printed_use_before else _ABSENT,
        "printed_use_before": printed_use_before,
        "as_needed": draft["asNeeded"] or _ABSENT,
        "clinical_uses": _na(med.get("clinical_uses")),
        "manufacturer": _na(med.get("manufacturer")),
        "hospital": _na(result.get("hospital")),
        "pharmacy": _na(result.get("pharmacy")),
        "prescription_no": _na(result.get("prescription_no")),
        "physician": _na(result.get("physician")),
        "pharmacist": _na(result.get("pharmacist")),
        "patient_name": _na(result.get("patient_name")),
        "date_dispensed": _na(result.get("date_dispensed")),
        "visit_date": _na(result.get("visit_date")),
        "quantity": _na(med.get("quantity")),
        "amount_each_intake": _na(med.get("amount_each_intake")),
        "frequency_text": _na(med.get("frequency_text")),
        "times_per_day": _num(times_per_day),
        "days": days,
        "form": med.get("form", _ABSENT),
        "total_intake_calc": _na(med.get("total_intake")),
        "intake_time_label": _na(med.get("intake_time_label")),
    }
    return {
        "name": _trim(draft["name"]),
        "dosage": _trim(draft["dosage"]) or None,
        "total_pills": _num(_js_round(stock)),
        "pills_remaining": stock,
        "instructions": _or(_na(med.get("instructions")), None),
        "warning": _or(_na(med.get("warning")), None),
        "pill_description": _or(_na(med.get("pill_description")), None),
        "use_before": last_day or None,
        "is_active": True,
        "dose_form": draft["doseForm"],
        "units_per_dose": units,
        "schedule_time": {**{slot: slots[slot] for slot in SLOTS}, "custom_times": draft["customTimes"]}
        if has_schedule else None,
        **dose_limits(draft),
        "prescription_meta": {key: value for key, value in meta.items() if value is not _ABSENT},
    }
