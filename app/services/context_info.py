"""What Reachy knows about the day, for its check-in replies: the date and time, the weather and Taiwan's holidays.

A few lines go into the reply prompt (conversation.reply_prompt), so Reachy can chat about the day ("今天好熱",
"明天要下雨記得帶傘", "中秋節快到了"). They are not tool calls, and nothing here waits on the network while a reply is
being written. The date, time and holidays are worked out locally. The weather is read from memory, where refresh()
keeps the latest Open-Meteo forecast; it is a scheduler job that runs every 30 minutes and once at startup. Only the
configured coordinates go to Open-Meteo, never anything about the patient.

Holidays are Taiwan's official days off, with their make-up days (補假), from python-holidays, which follows the
DGPA calendar. They also include the traditional festivals elderly people keep even when there is no day off, with
lunar dates from lunar_python.
"""

import logging
from datetime import date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import requests

from app import config

log = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
WEATHER_TIMEOUT = 15
WEATHER_MAX_AGE = timedelta(hours=3)   # an older forecast is left out, never passed off as today's
CURRENT_MAX_AGE = timedelta(hours=1)   # "now 24 degrees" only from a recent fetch
UPCOMING_DAYS = 14
MAX_UPCOMING = 2                       # dates; a make-up day goes inside its holiday's entry
LANGUAGES = ("zh-TW", "en")

WEEKDAYS = {"zh-TW": ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"),
            "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")}
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")
# (first hour, 中文, English): an hour belongs to the last period that starts at or before it. The word goes before
# a 12-hour clock time, as people say it: 凌晨2點, 早上11點, 中午12點, 深夜11點.
PERIODS = ((0, "凌晨", "late at night"), (5, "早上", "in the morning"), (12, "中午", "around noon"),
           (13, "下午", "in the afternoon"), (18, "晚上", "in the evening"), (23, "深夜", "late at night"))
# As Taiwan's calendars write them: 十一月 and 十二月, not 冬月 and 臘月.
LUNAR_MONTHS = ("正", "二", "三", "四", "五", "六", "七", "八", "九", "十", "十一", "十二")

# Open-Meteo's WMO weather codes, in the words a weather report uses.
WEATHER_WORDS = {
    0: ("晴天", "clear"), 1: ("晴時多雲", "mostly clear"), 2: ("多雲", "partly cloudy"), 3: ("陰天", "overcast"),
    45: ("有霧", "fog"), 48: ("有霧", "fog"),
    51: ("毛毛雨", "drizzle"), 53: ("毛毛雨", "drizzle"), 55: ("毛毛雨", "drizzle"),
    56: ("凍雨", "freezing drizzle"), 57: ("凍雨", "freezing drizzle"),
    61: ("小雨", "light rain"), 63: ("下雨", "rain"), 65: ("大雨", "heavy rain"),
    66: ("凍雨", "freezing rain"), 67: ("凍雨", "freezing rain"),
    71: ("下雪", "snow"), 73: ("下雪", "snow"), 75: ("大雪", "heavy snow"), 77: ("下雪", "snow"),
    80: ("陣雨", "showers"), 81: ("陣雨", "showers"), 82: ("大陣雨", "heavy showers"),
    85: ("陣雪", "snow showers"), 86: ("陣雪", "snow showers"),
    95: ("雷雨", "thunderstorms"), 96: ("雷雨夾冰雹", "thunderstorms with hail"),
    99: ("雷雨夾冰雹", "thunderstorms with hail"),
}
# When there is no forecast. With only "no data right now", English replies sent the patient to a doctor or
# pharmacist about the weather (5 of 19, 3 Oct 2026); with somewhere else to point, 0 of 11. Here, not in the rules,
# so the model doesn't hedge when the weather is given.
NO_WEATHER = {"zh-TW": "天氣：不知道，沒有拿到預報；可以請對方看看窗外或問家人。",
              "en": "Weather: unknown, no forecast available; they could look outside or ask family."}

# python-holidays uses the official names; these are the ones people say.
SHORT_NAMES = {
    "中華民國開國紀念日": "元旦", "農曆除夕": "除夕", "民族掃墓節": "清明節", "孔子誕辰紀念日": "教師節",
    "臺灣光復暨金門古寧頭大捷紀念日": "臺灣光復節",
    "Founding Day of the Republic of China": "New Year's Day", "Chinese New Year's Eve": "Lunar New Year's Eve",
    "Chinese New Year": "Lunar New Year", "Confucius' Birthday": "Teachers' Day",
    "Taiwan Restoration and Guningtou Victory Memorial Day": "Taiwan Retrocession Day",
}
MAKE_UP = {"zh-TW": ("（補假）", "補假"), "en": (" (observed)", " (day off in lieu)")}
# python-holidays names the make-up day of 除夕前一日 after 除夕 (2026-02-20, 農曆除夕（補假）), so Reachy would say
# 除夕補假 after 除夕 has passed. Any make-up day of the two falls inside the Spring Festival break: 春節補假.
MAKE_UP_BASE = {"農曆除夕": "春節", "Chinese New Year's Eve": "Chinese New Year"}
# The law's 除夕 holiday starts the day before (除夕前一日), and python-holidays gives both days the 除夕 name.
EVE = {"zh-TW": "農曆除夕", "en": "Chinese New Year's Eve"}
DAY_BEFORE_EVE = {"zh-TW": "除夕前一日", "en": "the day before Lunar New Year's Eve"}
# Traditional festivals elderly people keep, mostly without a day off: (中文, English, lunar month, day).
# 春節, 端午 and 中秋 are days off and come from python-holidays.
LUNAR_FESTIVALS = (("元宵節", "Lantern Festival", 1, 15), ("七夕", "Qixi Festival", 7, 7),
                   ("中元節", "Ghost Festival", 7, 15), ("重陽節", "Double Ninth Festival", 9, 9),
                   ("尾牙", "Weiya, the year-end feast", 12, 16))

_weather: dict | None = None   # the last good forecast, replaced whole, so a reply never reads half an update


def local_now() -> datetime:
    return datetime.now(ZoneInfo(config.MEDCARE_TIMEZONE))


def _safely(function, *args):
    """function(*args), or None (logged) when it fails: one broken part never costs the reply the rest."""
    try:
        return function(*args)
    except Exception:
        log.exception("check-in background: %s failed", getattr(function, "__name__", function))
        return None


# ── date and time ──

def time_of_day(hour: int) -> tuple[str, str]:
    return next((zh, en) for first, zh, en in reversed(PERIODS) if hour >= first)


def clock(hour: int, minute: int) -> str:
    """「2點05分」, on the 12-hour clock people speak, after the period word. Written 「深夜 02:05」, the model said
    「深夜十二點多」 in 9 of 16 replies to 現在幾點了 (3 Oct 2026); 「凌晨2點05分」 was right 12 of 12."""
    return f"{hour % 12 or 12}點" + (f"{minute:02d}分" if minute else "整")


def lunar_date(day: date) -> tuple[str, str]:
    """("農曆八月廿三", "lunar month 8, day 23"); a leap month is 閏."""
    from lunar_python import Solar

    lunar = Solar.fromYmd(day.year, day.month, day.day).getLunar()
    month, leap = abs(lunar.getMonth()), lunar.getMonth() < 0
    return (f"農曆{'閏' if leap else ''}{LUNAR_MONTHS[month - 1]}月{lunar.getDayInChinese()}",
            f"lunar {'leap ' if leap else ''}month {month}, day {lunar.getDay()}")


def _date_line(now: datetime, language: str) -> str:
    period_zh, period_en = time_of_day(now.hour)
    lunar_zh, lunar_en = _safely(lunar_date, now.date()) or ("", "")
    if language == "zh-TW":
        line = (f"現在是{now.year}年{now.month}月{now.day}日（{WEEKDAYS['zh-TW'][now.weekday()]}）"
                f"{period_zh}{clock(now.hour, now.minute)}")
        return line + (f"，{lunar_zh}。" if lunar_zh else "。")
    line = (f"It is {WEEKDAYS['en'][now.weekday()]} {now.day} {MONTHS[now.month - 1]} {now.year}, "
            f"{now:%H:%M} {period_en} (local time)")
    return line + (f"; {lunar_en}." if lunar_en else ".")


# ── weather ──

def weather_enabled() -> bool:
    return bool(config.WEATHER_ENABLED and config.WEATHER_LATITUDE and config.WEATHER_LONGITUDE)


def parse_weather(body: dict, fetched_at: datetime) -> dict:
    """The parts of an Open-Meteo answer the background uses: the current temperature and weather, and each day's
    max/min, rain chance and weather by local date ("2026-10-03"). Raises ValueError when it has none of them."""
    current = body.get("current") or {}
    daily = body.get("daily") or {}

    def at(key: str, i: int):
        values = daily.get(key) or []
        return values[i] if i < len(values) else None

    days = {day: {"max": at("temperature_2m_max", i), "min": at("temperature_2m_min", i),
                  "rain": at("precipitation_probability_max", i), "code": at("weather_code", i)}
            for i, day in enumerate(daily.get("time") or [])}
    now = {"temperature": current.get("temperature_2m"), "code": current.get("weather_code")}
    if not days and now["temperature"] is None:
        raise ValueError("no forecast in Open-Meteo's answer")
    return {"fetched_at": fetched_at, "current": now, "days": days}


def fetch_weather() -> dict:
    """One Open-Meteo forecast for the configured place: no key, and only its coordinates are sent."""
    response = requests.get(OPEN_METEO_URL, timeout=WEATHER_TIMEOUT, params={
        "latitude": float(config.WEATHER_LATITUDE), "longitude": float(config.WEATHER_LONGITUDE),
        "current": "temperature_2m,weather_code",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        # Daily values by local date. Three days, so tomorrow is still there just after midnight.
        "timezone": config.MEDCARE_TIMEZONE, "forecast_days": 3})
    response.raise_for_status()
    return parse_weather(response.json(), local_now())


def _day_weather(entry: dict | None, zh: bool) -> str | None:
    if not entry:
        return None
    parts = []
    if entry.get("min") is not None and entry.get("max") is not None:
        low, high = round(entry["min"]), round(entry["max"])
        parts.append(f"{low}到{high}度" if zh else f"{low}-{high}°C")
    words = WEATHER_WORDS.get(entry.get("code"))
    if words:
        parts.append(words[0 if zh else 1])
    if entry.get("rain") is not None:
        parts.append(f"降雨機率{round(entry['rain'])}%" if zh else f"{round(entry['rain'])}% chance of rain")
    return ("，" if zh else ", ").join(parts) or None


def _weather_line(now: datetime, language: str) -> str:
    """Now, today and tomorrow from the cached forecast. When there is none fresh enough, the line says so, and the
    prompt tells the model to say it isn't sure rather than make the weather up."""
    zh = language == "zh-TW"
    weather = _weather
    parts = []
    if weather_enabled() and weather and now - weather["fetched_at"] <= WEATHER_MAX_AGE:
        current = weather["current"]
        if now - weather["fetched_at"] <= CURRENT_MAX_AGE and current.get("temperature") is not None:
            temperature = round(current["temperature"])
            text = f"{temperature}度" if zh else f"{temperature}°C"
            words = WEATHER_WORDS.get(current.get("code"))
            if words:
                text += ("，" if zh else ", ") + words[0 if zh else 1]
            parts.append(("現在" if zh else "now ") + text)
        today = now.date()
        for label_zh, label_en, day in (("今天", "today", today), ("明天", "tomorrow", today + timedelta(days=1))):
            text = _day_weather(weather["days"].get(day.isoformat()), zh)
            if text:
                parts.append((label_zh if zh else label_en + " ") + text)
    if not parts:
        return NO_WEATHER[language]
    # No place name: the model only needs "here", and the robot notice says only conversation text goes to it.
    return ("天氣：" + "；".join(parts) + "。") if zh else ("Local weather: " + "; ".join(parts) + ".")


# ── holidays ──

def _nth_sunday(year: int, month: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))


def _festivals(year: int) -> list[tuple[date, str, str]]:
    """(date, 中文, English) of the festivals in LUNAR_FESTIVALS, 冬至 and the family days, in Gregorian `year`."""
    from lunar_python import Lunar, Solar

    found = []
    for lunar_year in (year - 1, year):   # 尾牙 of the previous lunar year falls in January or February
        for name_zh, name_en, month, day in LUNAR_FESTIVALS:
            solar = Lunar.fromYmd(lunar_year, month, day).getSolar()
            found.append((date(solar.getYear(), solar.getMonth(), solar.getDay()), name_zh, name_en))
    # 冬至 is a solar term, not a lunar date: the December day the calendar marks with it (the 21st or 22nd).
    found += [(date(year, 12, day), "冬至", "Winter Solstice") for day in range(20, 24)
              if Solar.fromYmd(year, 12, day).getLunar().getJieQi() == "冬至"]
    found += [(_nth_sunday(year, 5, 2), "母親節", "Mother's Day"), (date(year, 8, 8), "父親節", "Father's Day"),
              (_nth_sunday(year, 8, 4), "祖父母節", "Grandparents' Day")]
    return [entry for entry in found if entry[0].year == year]


def _official_name(name: str, language: str) -> str:
    suffix, spoken = MAKE_UP[language]
    if name.endswith(suffix):
        base = MAKE_UP_BASE.get(name[:-len(suffix)], name[:-len(suffix)])
        return SHORT_NAMES.get(base, base) + spoken
    return SHORT_NAMES.get(name, name)


@lru_cache(maxsize=8)
def days_of(year: int, language: str) -> dict[date, tuple[str, ...]]:
    """Every official day off and festival of `year`, by date, named in `language`. The first build of a year takes
    ~0.3 s, so refresh() builds them ahead in a worker thread rather than on the event loop of a reply."""
    import holidays

    official = holidays.country_holidays("TW", years=year, language="zh_TW" if language == "zh-TW" else "en_US")
    eve = EVE[language]
    days: dict[date, list[str]] = {}
    for day in sorted(day for day in list(official) if day.year == year):
        for name in official.get_list(day):
            if name == eve and eve in official.get_list(day + timedelta(days=1)):
                name = DAY_BEFORE_EVE[language]
            days.setdefault(day, []).append(_official_name(name, language))
    for day, name_zh, name_en in _festivals(year):
        days.setdefault(day, []).append(name_zh if language == "zh-TW" else name_en)
    return {day: tuple(names) for day, names in days.items()}


def _names(day: date, language: str) -> tuple[str, ...]:
    return days_of(day.year, language).get(day, ())


def _on(day: date, language: str) -> str:
    """「10月9日星期五」 / "Friday 9 October"."""
    if language == "zh-TW":
        return f"{day.month}月{day.day}日{WEEKDAYS['zh-TW'][day.weekday()]}"
    return f"{WEEKDAYS['en'][day.weekday()]} {day.day} {MONTHS[day.month - 1]}"


def _holiday_line(today: date, language: str) -> str:
    """Today's holidays and the next MAX_UPCOMING dates with one within UPCOMING_DAYS, with the days to go.

    A make-up day goes inside its holiday's entry when both are coming. Listed first on its own
    (「10月9日國慶日補假，還有6天；10月10日國慶日，還有7天」), it made the model say 國慶日 was 6 days away in 5 of 8
    replies (3 Oct 2026).
    """
    zh = language == "zh-TW"
    spoken = MAKE_UP[language][1]
    window = [today + timedelta(days=n) for n in range(1, UPCOMING_DAYS + 1)]
    names = {day: list(_names(day, language)) for day in window}
    first: dict[str, date] = {}
    for day in window:
        for name in names[day]:
            first.setdefault(name, day)
    make_up: dict[date, list[date]] = {}   # holiday -> its make-up days
    for day in window:
        for name in list(names[day]):
            holiday = first.get(name[:-len(spoken)]) if name.endswith(spoken) else None
            if holiday:
                names[day].remove(name)
                make_up.setdefault(holiday, []).append(day)
    entries = []
    for day in [day for day in window if names[day]][:MAX_UPCOMING]:
        n = (day - today).days
        extra = make_up.get(day)
        if zh:
            entries.append(f"{day.month}月{day.day}日（{WEEKDAYS[language][day.weekday()]}）{'、'.join(names[day])}，"
                           + ("就是明天" if n == 1 else f"還有{n}天")
                           + (f"（{'、'.join(_on(d, language) for d in extra)}補假）" if extra else ""))
        else:
            entries.append(f"{' and '.join(names[day])} on {_on(day, language)}, "
                           + ("tomorrow" if n == 1 else f"in {n} days")
                           + (f" (day off in lieu on {' and '.join(_on(d, language) for d in extra)})" if extra else ""))
    today_names = _names(today, language)
    if zh:
        line = "節日：" + (f"今天是{'、'.join(today_names)}。" if today_names else "今天沒有節日。")
        return line + ("接下來：" + "；".join(entries) + "。" if entries else "接下來兩週沒有節日。")
    line = "Holidays: " + (f"today is {' and '.join(today_names)}." if today_names else "none today.")
    return line + (" Coming up: " + "; ".join(entries) + "." if entries else " None in the next two weeks.")


# ── the block and its refresh ──

def background(language: str, now: datetime | None = None) -> str:
    """The lines for the reply prompt: date and time, weather, holidays. Memory and local data only."""
    language = language if language in LANGUAGES else "en"
    now = (now or local_now()).astimezone(ZoneInfo(config.MEDCARE_TIMEZONE))
    lines = [_date_line(now, language), _safely(_weather_line, now, language),
             _safely(_holiday_line, now.date(), language)]
    return "\n".join(line for line in lines if line)


def refresh() -> None:
    """Scheduler job, every 30 minutes and once at startup, in a worker thread (so startup never waits on it).

    Builds this year's and next year's holiday tables ahead of the replies, then fetches the weather. A failed fetch
    keeps the last forecast, which drops out of the background after WEATHER_MAX_AGE.
    """
    global _weather
    this_year = local_now().year
    for year in (this_year, this_year + 1):
        for language in LANGUAGES:
            _safely(days_of, year, language)
    if not weather_enabled():
        return
    try:
        _weather = fetch_weather()
    except Exception as exc:
        log.warning("weather for %s from Open-Meteo failed, keeping the last forecast: %s",
                    config.WEATHER_PLACE or "the configured coordinates", exc)
