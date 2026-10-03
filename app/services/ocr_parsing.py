"""Pure helpers for prescription OCR: usage text to dose times, ROC dates, dose forms, and identifier removal.

Nothing here does I/O or calls a model, so every rule has a unit test (`tests/test_ocr_parsing.py`).
`ocr_service.OCRService` turns the model's JSON into the scan result with them.
"""

import datetime
import re
import unicodedata

from app.services.schedule import MAX_TIMES_PER_DAY, PRESET_TIMES

SLOTS = ("morning", "noon", "night", "bedtime", "before_meals", "after_meals")
TIME_SLOTS = ("morning", "noon", "night", "bedtime")
MEAL_SLOTS = ("before_meals", "after_meals")

# N doses a day → preset slots: 1 morning, 2 morning + night, 3 + noon, 4 + bedtime.
_COUNT_SLOTS = {
    1: ("morning",),
    2: ("morning", "night"),
    3: ("morning", "noon", "night"),
    4: ("morning", "noon", "night", "bedtime"),
}
# Text that names fewer slots than its count ("一天2次 睡前") gets the rest in this order.
_FILL_ORDER = ("morning", "night", "noon", "bedtime")
# More than 4 a day: the four presets, then these (the Medications page edits them as "other times").
_EXTRA_TIMES = ("16:00", "06:00", "10:00", "14:00")

_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "壹": 1, "二": 2, "兩": 2, "两": 2, "貳": 2, "三": 3, "參": 3,
              "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_NUM = r"[0-9]+|[零〇一壹二兩两貳三參四五六七八九十]+"
_AMOUNT_NUM = rf"[0-9]+\.[0-9]+|{_NUM}"
_PILL_WORD = r"(?:顆|粒|錠|片|包|TABS?\b|CAPS?\b)"

# Taiwanese prescriptions mix Chinese and Latin abbreviations, often run together: "TIDPC", "QIDACHS", "Q6HPRN".
_CODE = r"QAM|QPM|QOD|QID|TID|BID|PRN|QD|QN|HS|AC|PC|PO|Q\d{1,2}H"
_CODE_RUN = re.compile(rf"(?:{_CODE})+")
_CODE_TOKEN = re.compile(_CODE)
_CODE_COUNT = {"QD": 1, "BID": 2, "TID": 3, "QID": 4}
# Dotted abbreviations (t.i.d., b.i.d. p.c., Q.D., h.s., q.6.h.): single letters or 1-2 digits joined by dots, no
# spaces inside. They are joined only when the result is a run of usage codes, so 1.5 or F.C. stay as they are.
_DOTTED_CODE = re.compile(r"(?<![A-Z0-9])(?:[A-Z]|\d{1,2})(?:\.(?:[A-Z]|\d{1,2})(?![A-Z0-9]))+\.?")

_AS_NEEDED = re.compile(
    r"需要時|必要時|有需要|疼痛時|發燒時|不適時|不舒服時|"
    r"(?<![A-Z])PRN(?![A-Z])|AS NEEDED|WHEN NEEDED|IF NEEDED|WHEN REQUIRED", re.IGNORECASE)
# 一天3次; a range (一天3-4次, 每日3至4次) is scheduled at its lower count and allowed up to its upper one.
_DAILY_COUNT = re.compile(
    rf"(?:每|一|壹|1)\s*[天日]\s*(?:最多|至多|不超過)?\s*(?:服用|使用|服|用|點|塗|擦)?\s*({_NUM})"
    rf"(?:\s*(?:-|~|至|到|或)\s*({_NUM}))?\s*次")
_SPLIT_COUNT = re.compile(rf"分\s*({_NUM})\s*次")                       # 每日2粒 分2次: two doses a day
# 每天1粒 / 每日一顆 with no 次 anywhere: one dose a day of that amount.
_DAILY_AMOUNT = re.compile(
    rf"(?:每|一|壹|1)\s*[天日]\s*(?:服用|使用|服|吃|用)?\s*({_AMOUNT_NUM}|半)\s*{_PILL_WORD}", re.IGNORECASE)
_EVERY_HOURS = re.compile(rf"每\s*({_NUM})\s*(?:個)?\s*(?:小時|鐘頭)")
_ENGLISH_COUNT = (
    (re.compile(r"\bonce\s+(?:a\s+|per\s+)?day\b|\bonce\s+daily\b", re.IGNORECASE), 1),
    (re.compile(r"\btwice\s+(?:a\s+|per\s+)?day\b|\btwice\s+daily\b", re.IGNORECASE), 2),
    (re.compile(r"\bthree\s+times\s+(?:a\s+|per\s+)?(?:day|daily)\b", re.IGNORECASE), 3),
    (re.compile(r"\bfour\s+times\s+(?:a\s+|per\s+)?(?:day|daily)\b", re.IGNORECASE), 4),
)
_ENGLISH_N_TIMES = re.compile(
    r"\b(\d+)(?:\s*(?:-|~|to)\s*(\d+))?\s*(?:times|x)\s*(?:a\s+|per\s+)?(?:day|daily)\b", re.IGNORECASE)
_ENGLISH_HOURS = re.compile(r"\bevery\s+(\d+)\s*(?:hours?|hrs?|h)\b", re.IGNORECASE)

# Slot words. The CJK patterns run on the text with spaces and list punctuation removed ("早、晚" → "早晚").
_BEDTIME_WORDS = r"睡前|睡覺前|就寢|臨睡"
_CJK_SLOT_PATTERNS = (
    ("morning", re.compile(r"[早晨]|上午")),
    ("noon", re.compile(r"中午|午餐|午飯|下午|早中晚|早午晚|(?<!下)午(?!夜)")),
    ("night", re.compile(r"晚|夜(?!間?尿)")),
    ("bedtime", re.compile(_BEDTIME_WORDS)),
    ("before_meals", re.compile(r"[飯餐]前|空腹")),
    ("after_meals", re.compile(r"[飯餐]後")),
)
# 晚/夜 written right before 睡前 only says when the bedtime dose is (每晚睡前, 晚上睡前, 夜間睡前服用): it is not a
# separate evening dose. Not inside a list such as 早晚 (早晚睡前 keeps all three), and not across a separator
# (晚上、睡前 is two doses).
_NIGHT_BEFORE_BED = re.compile(
    rf"(?<![早中午])(?:每|今)?(?:晚上|晚間|夜間|夜晚|晚|夜)(?:服用|使用|服|用|於|在)?(?={_BEDTIME_WORDS})")
_THREE_MEALS = re.compile(r"三餐")
_ENGLISH_SLOT_PATTERNS = (
    ("morning", re.compile(r"\b(?:morning|breakfast)\b", re.IGNORECASE)),
    ("noon", re.compile(r"\b(?:noon|midday|lunch)\b", re.IGNORECASE)),
    ("night", re.compile(r"\b(?:evening|night|dinner|supper)\b", re.IGNORECASE)),
    ("bedtime", re.compile(r"\b(?:bedtime|at bed)\b", re.IGNORECASE)),
    ("before_meals", re.compile(r"\bbefore\s+(?:meals?|food|eating)\b|\bempty stomach\b", re.IGNORECASE)),
    ("after_meals", re.compile(r"\bafter\s+(?:meals?|food|eating)\b", re.IGNORECASE)),
)
_LIST_PUNCTUATION = re.compile(r"[\s、，,/／.。・;；:：()（）\[\]]+")


def _nfkc(text) -> str:
    return unicodedata.normalize("NFKC", text) if isinstance(text, str) else ""


def cn_number(text: str) -> int | None:
    """3, "３", "三", "兩", "十二" → int; None when it is not a number."""
    text = _nfkc(text).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if "十" in text:
        tens, _, ones = text.partition("十")
        ten_value = _CN_DIGITS.get(tens) if tens else 1
        one_value = _CN_DIGITS.get(ones) if ones else 0
        if ten_value is None or one_value is None or "十" in ones:
            return None
        return ten_value * 10 + one_value
    if len(text) == 1:
        return _CN_DIGITS.get(text)
    return None


def slots_for_count(count: int | None) -> tuple[dict, list[str]]:
    """The default dose times for N doses a day: (six slot booleans, custom "HH:MM" times)."""
    slots = {slot: False for slot in SLOTS}
    custom: list[str] = []
    if not count or count < 1:
        return slots, custom
    for slot in _COUNT_SLOTS.get(min(count, 4), ()):
        slots[slot] = True
    if count > 4:
        custom = list(_EXTRA_TIMES[:min(count, MAX_TIMES_PER_DAY) - 4])
    return slots, custom


def _undot_codes(upper: str) -> str:
    """T.I.D. → TID, B.I.D. P.C. → BID PC, Q.6.H. → Q6H; anything that does not join into usage codes is kept."""
    def join(match: re.Match) -> str:
        joined = match.group(0).replace(".", "")
        return joined if _CODE_RUN.fullmatch(joined) else match.group(0)
    return _DOTTED_CODE.sub(join, upper)


def parse_frequency(text: str) -> dict:
    """What a medicine's usage text says about when to take it.

    Returns {"times_per_day", "max_per_day", "interval_hours", "as_needed", "schedule": {six slots},
    "custom_times": [...]}. Counts come from 一天/一日/每日/每天 N 次 (Arabic or Chinese numerals; a range 3-4次 is
    scheduled at 3 and allowed up to 4, "max_per_day"), 分N次, QD/BID/TID/QID (also dotted: t.i.d.), Q4H-Q12H,
    每 N 小時 and English phrases; 每天1粒 with no 次 is once a day. Slots come from 早/中午/晚/睡前, 早晚, 早中晚, 三餐,
    HS, QAM/QPM and English words; 飯前/飯後 (AC/PC) add the meal marks. Words that name slots win; a count adds the
    usual slots for the doses they leave out (1/day: morning, 2: + night, 3: + noon, 4: + bedtime). 晚 right before
    睡前 (每晚睡前) is the bedtime dose, not an evening one too. An as-needed medicine (需要時, 必要時, PRN) gets no fixed
    times unless its text names them; its count is then the most it may be taken a day. QOD (every other day) gives
    an interval of 48 h and no daily times: the medication schedule cannot skip days.
    """
    raw = _nfkc(text)
    schedule = {slot: False for slot in SLOTS}
    result = {"times_per_day": None, "max_per_day": None, "interval_hours": None, "as_needed": False,
              "schedule": schedule, "custom_times": []}
    if not raw.strip() or raw.strip().casefold() == "n/a":
        return result
    upper = _undot_codes(raw.upper())
    as_needed = bool(_AS_NEEDED.search(raw))

    count = None
    most = None
    interval = None
    match = _DAILY_COUNT.search(raw)
    if match:
        count = cn_number(match.group(1))
        upper_count = cn_number(match.group(2)) if match.group(2) else None
        if count and upper_count and upper_count > count:
            most = upper_count
    if count is None:
        split = _SPLIT_COUNT.search(raw)
        if split:
            count = cn_number(split.group(1))
    hours_match = _EVERY_HOURS.search(raw) or _ENGLISH_HOURS.search(raw)
    if hours_match:
        interval = cn_number(hours_match.group(1))

    code_slots = set()
    for run in re.findall(r"[A-Z0-9]+", upper):
        if not _CODE_RUN.fullmatch(run):
            continue          # a word such as DROPS or TABLETS, not a run of usage codes
        for token in _CODE_TOKEN.findall(run):
            if token in _CODE_COUNT and count is None:
                count = _CODE_COUNT[token]
            elif token.startswith("Q") and token.endswith("H") and token[1:-1].isdigit() and interval is None:
                interval = int(token[1:-1])
            elif token == "QOD" and interval is None:
                interval = 48
            elif token == "HS":
                code_slots.add("bedtime")
            elif token == "QAM":
                code_slots.add("morning")
            elif token in ("QPM", "QN"):
                code_slots.add("night")
            elif token == "AC":
                code_slots.add("before_meals")
            elif token == "PC":
                code_slots.add("after_meals")
            elif token == "PRN":
                as_needed = True
    if count is None:
        for pattern, value in _ENGLISH_COUNT:
            if pattern.search(raw):
                count = value
                break
    if count is None:
        english_n = _ENGLISH_N_TIMES.search(raw)
        if english_n:
            count = int(english_n.group(1))
            if english_n.group(2) and int(english_n.group(2)) > count:
                most = int(english_n.group(2))
    if count is None and "次" not in raw and _DAILY_AMOUNT.search(raw):
        count = 1
    if count is None and interval and 1 <= interval <= 24:
        count = max(1, round(24 / interval))
    if count is not None and not 1 <= count <= 24:
        count = None
    if most is not None and (count is None or not count < most <= 24):
        most = None
    if interval is not None and not 1 <= interval <= 72:
        interval = None

    compact = _LIST_PUNCTUATION.sub("", _NIGHT_BEFORE_BED.sub("", raw))
    for slot, pattern in _CJK_SLOT_PATTERNS:
        if pattern.search(compact):
            schedule[slot] = True
    if _THREE_MEALS.search(compact):
        schedule.update(morning=True, noon=True, night=True)
    for slot, pattern in _ENGLISH_SLOT_PATTERNS:
        if pattern.search(raw):
            schedule[slot] = True
    for slot in code_slots:
        schedule[slot] = True

    named = [slot for slot in TIME_SLOTS if schedule[slot]]
    # More named times than the printed count (一天1次 晚上 睡前): the evening and the bedtime are one dose.
    if count and not as_needed and len(named) > count and schedule["night"] and schedule["bedtime"]:
        schedule["night"] = False
        named.remove("night")
    custom: list[str] = []
    if count and not (as_needed and not named):
        if not named:
            defaults, custom = slots_for_count(count)
            for slot in TIME_SLOTS:
                schedule[slot] = defaults[slot]
        elif len(named) < count:
            for slot in _FILL_ORDER:
                if sum(schedule[s] for s in TIME_SLOTS) >= count:
                    break
                schedule[slot] = True
            if count > 4:
                custom = list(_EXTRA_TIMES[:min(count, MAX_TIMES_PER_DAY) - 4])

    times = sum(schedule[slot] for slot in TIME_SLOTS) + len(custom)
    result.update(
        times_per_day=count if count else (times or None),
        max_per_day=most,
        interval_hours=interval,
        as_needed=as_needed,
        custom_times=custom,
    )
    return result


_DAYS_IN_TEXT = re.compile(rf"共\s*({_NUM})\s*[天日]|({_NUM})\s*(?:天份|日份)|\bfor\s+(\d+)\s+days?\b", re.IGNORECASE)


def days_from_text(text: str) -> int | None:
    """Days of treatment written in usage text: 共7天, 7天份, for 7 days."""
    match = _DAYS_IN_TEXT.search(_nfkc(text))
    if not match:
        return None
    return cn_number(next(group for group in match.groups() if group))


def has_schedule(schedule: dict | None, custom_times=None) -> bool:
    """Whether a schedule says anything about when to take the medicine (a time, a custom time or a meal mark)."""
    schedule = schedule or {}
    return any(bool(schedule.get(slot)) for slot in SLOTS) or bool(custom_times or schedule.get("custom_times"))


def dose_times(schedule: dict, custom_times=()) -> list[str]:
    times = {PRESET_TIMES[slot] for slot in TIME_SLOTS if schedule.get(slot)}
    times.update(custom_times or ())
    return sorted(times)


# ── dates ────────────────────────────────────────────────────────────────────

_CJK_DATE = re.compile(r"(\d{2,4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?")
_SEPARATED_DATE = re.compile(r"(?<!\d)(\d{2,4})\s*[./\-]\s*(\d{1,2})\s*[./\-]\s*(\d{1,2})(?!\d)")
_COMPACT_DATE = re.compile(r"(?<!\d)(\d{7,8})(?!\d)")


def _calendar_date(year: int, month: int, day: int) -> datetime.date | None:
    if year < 1000:
        year += 1911        # ROC (民國) year
    try:
        return datetime.date(year, month, day)
    except ValueError:
        return None


def parse_date(text) -> datetime.date | None:
    """A date in ROC or Gregorian form: 1150928, 115/09/28, 115.9.28, 115年9月28日, 2026-09-28, 20260928.

    Compact forms: 7 digits are ROC YYYMMDD (0650101 is ROC 65); 8 digits are Gregorian YYYYMMDD.
    """
    text = _nfkc(text) if isinstance(text, str) else (str(text) if isinstance(text, int) else "")
    if not text.strip():
        return None
    for pattern in (_CJK_DATE, _SEPARATED_DATE):
        match = pattern.search(text)
        if match:
            return _calendar_date(*(int(group) for group in match.groups()))
    match = _COMPACT_DATE.search(text)
    if match:
        digits = match.group(1)
        if len(digits) == 7:
            return _calendar_date(int(digits[:3]), int(digits[3:5]), int(digits[5:]))
        if digits[:2] in ("19", "20"):
            return _calendar_date(int(digits[:4]), int(digits[4:6]), int(digits[6:]))
    return None


def normalize_date(text, kind: str, today: datetime.date) -> str | None:
    """ISO YYYY-MM-DD, or None when the text holds no plausible date of this kind.

    kind "past" (a visit or dispensing date): within the last 5 years, at most a day ahead.
    kind "until" (a use-before date): from 5 years ago to 10 years ahead.
    """
    date = parse_date(text)
    if date is None:
        return None
    earliest = today - datetime.timedelta(days=5 * 365 + 1)
    latest = today + (datetime.timedelta(days=1) if kind == "past" else datetime.timedelta(days=10 * 365 + 2))
    if not earliest <= date <= latest:
        return None
    return date.isoformat()


# ── dose form ────────────────────────────────────────────────────────────────

# Fine-grained form → the medication API's dose_form. Only tablets and capsules are solid_oral: the robot and the
# browser may auto-record only one solid_oral unit, so drops, creams and anything unrecognised need a person.
FORM_TO_DOSE_FORM = {
    "tablet": "solid_oral", "capsule": "solid_oral", "oral_liquid": "liquid", "inhaler": "inhaler",
    "injection": "injection", "topical": "topical", "ear_drops": "other", "eye_drops": "other",
    "nasal_spray": "other", "drops": "other", "suppository": "other", "powder": "other", "unknown": "other",
}
_FORM_PATTERNS = (
    ("ear_drops", re.compile(r"\bOTIC\b|\bEAR\b|耳")),
    ("eye_drops", re.compile(r"\bEYE\b|\bOPHTH|\bOPH\b|\bE/D\b|眼")),
    ("nasal_spray", re.compile(r"\bNASAL\b|鼻")),
    ("inhaler", re.compile(r"INHAL|HALER\b|吸入")),
    ("injection", re.compile(r"\bINJ|\bSYRINGE\b|注射|針劑")),
    ("topical", re.compile(r"\bOINT|\bCREAM\b|\bGEL\b|\bLOTION\b|\bPATCH|膏|乳液|貼片|貼布|凝膠|外用")),
    # Vaginal and rectal forms before tablets: a 陰道錠 is not swallowed.
    ("suppository", re.compile(r"\bSUPP|栓劑|塞劑|\bVAG(?:INAL)?\b|陰道|\bRECTAL\b|肛門")),
    ("oral_liquid", re.compile(r"\bSYR(?:UP)?\b|\bSOL(?:N|UTION)\b|\bSUSP|\bELIXIR\b|糖漿|內服液|口服液|水劑")),
    ("drops", re.compile(r"\bDROPS?\b|滴劑")),
    ("powder", re.compile(r"\bPOWDER\b|\bGRAN(?:ULES?)?\b|顆粒|散劑|藥粉")),
    ("capsule", re.compile(r"\bCAP(?:S|SULES?)?\b|膠囊")),
    ("tablet", re.compile(r"\bTAB(?:S|LETS?)?\b|\bF\.?C\.?(?![A-Z])|錠|片劑")),
)
# Units that count single tablets or capsules, and units that are a container or an amount of liquid.
_PILL_UNITS = re.compile(r"錠|顆|粒|片|\bTAB|\bCAP", re.IGNORECASE)
_CONTAINER_UNITS = re.compile(r"瓶|支|條|罐|盒|管|\bBOT|\bBTL|\bTUBE|\bVIAL|ML\b|CC\b", re.IGNORECASE)
_SACHET_UNITS = re.compile(r"包|\bPACK|\bSACHET", re.IGNORECASE)


def guess_form(name: str, unit: str = "", other_text: str = "") -> str:
    """tablet / capsule / ear_drops / eye_drops / topical / … / unknown, from the name first, then unit and usage."""
    for text in (name, f"{unit} {other_text}"):
        upper = _nfkc(text).upper()
        for form, pattern in _FORM_PATTERNS:
            if pattern.search(upper):
                return form
    units = _nfkc(f"{unit} {other_text}")
    if _PILL_UNITS.search(units):
        return "tablet"
    if _SACHET_UNITS.search(units):
        return "powder"
    return "unknown"


def is_container_unit(unit: str) -> bool:
    return bool(_CONTAINER_UNITS.search(_nfkc(unit)))


_TABLET_WORD = r"[顆粒錠片]"
_FRACTION = re.compile(r"(?<![\d/.])([1-3])\s*/\s*([2-4])(?![\d/])")             # 1/2, 1/4, 3/4 (not 9/28)
# 半 is half a tablet only next to a tablet word or 每次 (一顆半, 半顆, 每次半); 飯前半小時 and 7點半 are times.
_AND_A_HALF = re.compile(rf"([0-9]+|[一二兩三四五])\s*{_TABLET_WORD}\s*半(?!\s*(?:小時|時|點|鐘|個))")
_HALF_TABLET = re.compile(rf"(?<!點)半\s*{_TABLET_WORD}")
_HALF_EACH = re.compile(r"(?:每次|一次)\s*(?:服用|服|吃)?\s*半(?!\s*(?:小時|時|點|鐘|個))")
_EACH_TIME = re.compile(rf"每次\s*(?:服用|服|吃)?\s*({_AMOUNT_NUM})")
_WITH_UNIT = re.compile(rf"({_AMOUNT_NUM})\s*(?:顆|粒|錠|片|包|TABS?\b|CAPS?\b|#)", re.IGNORECASE)
_NUMBER_ONLY = re.compile(rf"\s*({_AMOUNT_NUM})\s*")
_NOT_STOCK_UNITS = re.compile(r"滴|毫升|ML\b|CC\b|噴|PUFF|公克|劑量", re.IGNORECASE)


def _amount_number(text: str) -> float | None:
    if text == "半":
        return 0.5
    return float(text) if "." in text else cn_number(text)


def units_per_dose(amount_text: str) -> float | None:
    """Tablets per dose from 每次1顆, 1.5錠, 半顆, 一顆半, 1/2 TAB, 每次一顆, 每日2粒分2次; None for drops, ml or an
    unreadable amount. 半 counts only next to a tablet word or 每次, so 飯前半小時 is not half a tablet."""
    text = _nfkc(amount_text)
    if not text.strip() or text.strip().casefold() == "n/a" or _NOT_STOCK_UNITS.search(text):
        return None
    value = None
    fraction = _FRACTION.search(text)
    and_a_half = _AND_A_HALF.search(text)
    daily = _DAILY_AMOUNT.search(text)
    split = _SPLIT_COUNT.search(text)
    if fraction and int(fraction.group(1)) < int(fraction.group(2)):
        value = int(fraction.group(1)) / int(fraction.group(2))
    elif and_a_half:
        value = (cn_number(and_a_half.group(1)) or 0) + 0.5
    elif _HALF_TABLET.search(text) or _HALF_EACH.search(text):
        value = 0.5
    elif daily and split and cn_number(split.group(1)):
        amount = _amount_number(daily.group(1))       # a day's amount split into doses
        value = amount / cn_number(split.group(1)) if amount else None
    else:
        match = _EACH_TIME.search(text) or _WITH_UNIT.search(text) or _NUMBER_ONLY.fullmatch(text)
        if match:
            value = _amount_number(match.group(1))
    if value is None or not 0 < value < 100:
        return None
    return round(float(value), 2)


def per_dose_from_totals(count: int | None, times_per_day: int | None, days: int | None) -> float | None:
    """Tablets per dose when only the total, doses a day and days are printed (15 錠, 一天3次, 5 日 → 1)."""
    if not count or not times_per_day or not days:
        return None
    value = count / (times_per_day * days)
    for usual in (0.25, 0.5, 1, 1.5, 2, 2.5, 3, 4):
        if abs(value - usual) < 1e-9:
            return float(usual)
    return None


# ── identifiers and names ────────────────────────────────────────────────────

_MASK_CHARS = "*＊●○◯xXｘＸ"
# A label is removed only with a value of an ID's shape after it (a digit or a mask character in it), and English
# labels only as whole words: "Calcium Dobesilate", "ACID NORMAL" or "Valid number" are medicine text, not IDs.
_ID_LABELLED = re.compile(
    r"(?:身[分份]證(?:統一編號|統一號碼|字號|號碼|號)?|統一證號|居留證(?:統一證號|號碼)?|"
    r"(?<![A-Za-z])(?:National\s*ID|ID\s*(?:No\.?|number|#))(?![A-Za-z]))"
    rf"\s*[:：]?\s*[A-Za-z0-9{_MASK_CHARS}\-]*[0-9{_MASK_CHARS}][A-Za-z0-9{_MASK_CHARS}\-]*", re.IGNORECASE)
# A birth-date label only with a date after it (bare 出生 is common in warnings: 新生兒出生後請勿使用).
_BIRTH_LABELLED = re.compile(
    r"(?:出生(?:年月)?日期|出生年月日|生日|(?<![A-Za-z])DOB(?![A-Za-z])|Date\s+of\s+birth|Birth\s*date|Birthday)"
    r"\s*[:：]?\s*(?:民國)?\s*[0-9０-９][0-9０-９./\-年月日\s]*", re.IGNORECASE)
# A Taiwanese national ID or resident certificate (letter + 1/2/8/9 + 8 digits), or a masked one (A12*****89, also
# with a mask a character longer or shorter, as a model may copy it, and the older two-letter resident certificate
# masked: AB*****123). Two letters + 8 digits unmasked is left alone: that is also the shape of an NHI drug code
# (AC12345100) on a bag, and a drug code is never masked.
_NATIONAL_ID = re.compile(rf"(?<![A-Za-z0-9])([A-Za-z]{{1,2}})([0-9{_MASK_CHARS}]{{7,11}})(?![A-Za-z0-9])")
_MASK = re.compile(rf"[{_MASK_CHARS}]")


def _looks_like_id(match: re.Match) -> bool:
    letters, body = match.group(1), match.group(2)
    if body.isdigit():
        return len(letters) == 1 and len(body) == 9 and body[0] in "1289"
    return (9 <= len(letters) + len(body) <= 12 and sum(ch.isdigit() for ch in body) >= 2
            and len(_MASK.findall(body)) >= 2)


def strip_identifiers(text: str) -> str:
    """The text without national ID numbers (full or masked) or labelled dates of birth."""
    if not isinstance(text, str) or not text:
        return text
    cleaned = _ID_LABELLED.sub(" ", text)
    cleaned = _BIRTH_LABELLED.sub(" ", cleaned)
    cleaned = _NATIONAL_ID.sub(lambda m: " " if _looks_like_id(m) else m.group(0), cleaned)
    if cleaned == text:
        return text
    return re.sub(r"\s{2,}", " ", cleaned).strip(" ,;:：，；")


# Words that make a "person name" something else: a fee, a code label or an institution.
_NOT_A_PERSON = re.compile(r"費|藥事|合計|健保|負擔|代號|代碼|序號|醫院|診所|藥局|藥房|地址|電話|收據|處方")
_LETTER = re.compile(r"[A-Za-z㐀-鿿]")


def person_name(value: str) -> str:
    """A printed person name, or "" when the value is a number, a code, a fee label or an institution."""
    value = (value or "").strip()
    if not value or value.casefold() in ("n/a", "na", "none", "null", "unknown"):
        return ""
    if re.search(r"\d{3,}", value) or not _LETTER.search(value) or _NOT_A_PERSON.search(value):
        return ""
    return value


# Document titles a model sometimes appends to the issuer's name (安心藥局藥品明細收據 → 安心藥局).
_DOCUMENT_TITLE = re.compile(
    r"\s*(?:門診|急診|住院)?(?:藥品(?:明細|調劑)?收據|調劑收據|明細收據|收據|處方箋|處方籤|領藥單|藥袋|藥品明細)\s*$")


# An institution code with its label (健保代碼:S945001122; the label alone when the code was already removed as
# an ID shape), or a stand-alone code (letters + 6 or more digits).
_CODE_WITH_LABEL = re.compile(
    r"(?:健保(?:特約)?(?:代碼|代號)|醫事機構(?:代碼|代號)|機構(?:代碼|代號)|特約(?:代碼|代號))\s*[:：]?\s*"
    r"(?:[A-Za-z]{0,2}\d{4,})?")
_LONG_CODE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{0,2}\d{6,}(?![A-Za-z0-9])")
# Address or phone appended to a name: cut there. Labels that are never part of a name: nothing is kept.
_ADDRESS_OR_PHONE = re.compile(r"\s*(?:地址|電話|TEL|Tel)\s*[:：].*$")
_NOT_AN_INSTITUTION = re.compile(r"代碼|代號|開立處方箋|序號|病歷號|地址|電話")


def institution_name(value: str) -> str:
    """A printed hospital, clinic or pharmacy name, or "" when the value is only a code, a number or a label.

    Codes are removed first (健保代碼:S945001122 安心藥局 → 安心藥局), then the document title after the name.
    """
    value = (value or "").strip()
    if not value or value.casefold() in ("n/a", "na", "none", "null", "unknown"):
        return ""
    value = _ADDRESS_OR_PHONE.sub("", value)
    value = _LONG_CODE.sub(" ", _CODE_WITH_LABEL.sub(" ", value))
    value = re.sub(r"\s{2,}", " ", value).strip(" ,;:：，；/／-")
    value = _DOCUMENT_TITLE.sub("", value).strip(" ,;:：，；/／-")
    if not value or not _LETTER.search(value) or _NOT_AN_INSTITUTION.search(value):
        return ""
    if re.fullmatch(r"[\dA-Za-z\s\-/:：]*\d{4,}[\dA-Za-z\s\-/:：]*", value):
        return ""
    return value


def is_pharmacy(name: str) -> bool:
    return bool(re.search(r"藥局|藥房|PHARMACY", name or "", re.IGNORECASE))
