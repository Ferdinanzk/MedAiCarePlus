"""What Reachy knows about the day (app/services/context_info.py): date and time, weather, holidays.

Holiday dates are pinned to the DGPA office calendars for 2026 (115年) and 2027 (116年), and festival dates to the
lunar calendar, checked against published dates in Oct 2026.
"""

import asyncio
import sys
import time
import types
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
import requests

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app import config
from app.services import context_info

TAIPEI = ZoneInfo("Asia/Taipei")
# What Open-Meteo answered for Taipei at 01:30 on 3 Oct 2026, trimmed to the fields the module asks for.
OPEN_METEO = {
    "latitude": 25.06151, "longitude": 121.5194, "utc_offset_seconds": 28800, "timezone": "Asia/Taipei",
    "current": {"time": "2026-10-03T01:30", "interval": 900, "temperature_2m": 23.7, "weather_code": 95},
    "daily": {"time": ["2026-10-03", "2026-10-04", "2026-10-05"], "weather_code": [95, 55, 51],
              "temperature_2m_max": [29.1, 30.4, 29.2], "temperature_2m_min": [23.7, 23.9, 23.5],
              "precipitation_probability_max": [79, 77, 90]},
}
FESTIVALS = {"元宵節", "七夕", "中元節", "重陽節", "尾牙", "冬至", "母親節", "父親節", "祖父母節"}


def at(*when) -> datetime:
    return datetime(*when, tzinfo=TAIPEI)


class Response:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self.body


@pytest.fixture(autouse=True)
def place(monkeypatch):
    """Taipei, no cached forecast, and no network: a test that fetches puts its own fake in network.answers."""
    monkeypatch.setattr(config, "MEDCARE_TIMEZONE", "Asia/Taipei")
    monkeypatch.setattr(config, "WEATHER_ENABLED", True)
    monkeypatch.setattr(config, "WEATHER_PLACE", "台北")
    monkeypatch.setattr(config, "WEATHER_LATITUDE", "25.0330")
    monkeypatch.setattr(config, "WEATHER_LONGITUDE", "121.5654")
    monkeypatch.setattr(context_info, "_weather", None)
    network = types.SimpleNamespace(calls=[], answers=[])

    def get(url, params=None, timeout=None, **kwargs):
        network.calls.append({"url": url, "params": params, "timeout": timeout, **kwargs})
        if not network.answers:
            raise AssertionError("unexpected network call")
        answer = network.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(context_info.requests, "get", get)
    return network


def _cached(fetched_at: datetime, body: dict = OPEN_METEO) -> None:
    context_info._weather = context_info.parse_weather(body, fetched_at)


# ── date and time ──

@pytest.mark.parametrize("hour, minute, spoken", [
    (0, 0, "凌晨12點整"), (2, 5, "凌晨2點05分"), (4, 59, "凌晨4點59分"), (5, 0, "早上5點整"), (11, 59, "早上11點59分"),
    (12, 0, "中午12點整"), (12, 59, "中午12點59分"), (13, 0, "下午1點整"), (17, 59, "下午5點59分"),
    (18, 0, "晚上6點整"), (22, 59, "晚上10點59分"), (23, 0, "深夜11點整"), (23, 59, "深夜11點59分"),
])
def test_the_time_is_written_the_way_it_is_said(hour, minute, spoken):
    """12-hour clock after the period word. Written 「深夜 02:05」, the model said it was just past midnight."""
    assert context_info.time_of_day(hour)[0] == spoken[:2]
    assert f"（星期六）{spoken}，農曆" in context_info.background("zh-TW", at(2026, 10, 3, hour, minute))


def test_the_day_turns_over_at_midnight_taipei_time_not_utc():
    before = context_info.background("zh-TW", datetime(2026, 10, 3, 15, 59, tzinfo=timezone.utc))
    after = context_info.background("zh-TW", datetime(2026, 10, 3, 16, 0, tzinfo=timezone.utc))
    assert before.startswith("現在是2026年10月3日（星期六）深夜11點59分，農曆八月廿三。")
    assert after.startswith("現在是2026年10月4日（星期日）凌晨12點整，農曆八月廿四。")
    # and with it the holiday of the day
    eve = context_info.background("zh-TW", datetime(2026, 9, 24, 15, 59, tzinfo=timezone.utc))
    festival = context_info.background("zh-TW", datetime(2026, 9, 24, 16, 0, tzinfo=timezone.utc))
    assert "今天沒有節日" in eve and "9月25日（星期五）中秋節，就是明天" in eve
    assert "今天是中秋節" in festival


def test_the_english_date_line():
    assert context_info.background("en", at(2026, 10, 3, 15, 5)).startswith(
        "It is Saturday 3 October 2026, 15:05 in the afternoon (local time); lunar month 8, day 23.")


@pytest.mark.parametrize("day, lunar", [
    (date(2026, 10, 3), "農曆八月廿三"), (date(2026, 9, 25), "農曆八月十五"), (date(2027, 2, 6), "農曆正月初一"),
    (date(2027, 2, 5), "農曆十二月廿九"),   # 除夕 2027: the twelfth month of 2026 has 29 days
    (date(2026, 12, 22), "農曆十一月十四"),
    (date(2028, 6, 23), "農曆閏五月初一"),  # 2028 has a leap fifth month
])
def test_lunar_dates(day, lunar):
    assert context_info.lunar_date(day)[0] == lunar


def test_a_broken_lunar_calendar_leaves_just_the_lunar_date_out(monkeypatch):
    def broken(day):
        raise RuntimeError("no lunar calendar")

    monkeypatch.setattr(context_info, "lunar_date", broken)
    assert context_info.background("zh-TW", at(2026, 10, 3, 1, 45)).startswith(
        "現在是2026年10月3日（星期六）凌晨1點45分。\n")


# ── holidays ──

@pytest.mark.parametrize("day, name", [
    # DGPA 115年 (2026): 除夕前一日 2/15 is a Sunday, made up on 2/20 (小年夜補假: inside the break, not after 除夕)
    (date(2026, 1, 1), "元旦"), (date(2026, 2, 15), "除夕前一日"), (date(2026, 2, 16), "除夕"),
    (date(2026, 2, 17), "春節"), (date(2026, 2, 19), "春節"), (date(2026, 2, 20), "春節補假"),
    (date(2026, 2, 27), "和平紀念日補假"), (date(2026, 4, 3), "兒童節補假"), (date(2026, 4, 6), "清明節補假"),
    (date(2026, 5, 1), "勞動節"), (date(2026, 6, 19), "端午節"), (date(2026, 9, 25), "中秋節"),
    (date(2026, 9, 28), "教師節"), (date(2026, 10, 9), "國慶日補假"), (date(2026, 10, 10), "國慶日"),
    (date(2026, 10, 25), "臺灣光復節"), (date(2026, 10, 26), "臺灣光復節補假"), (date(2026, 12, 25), "行憲紀念日"),
    # DGPA 116年 (2027): 春節 2/6-2/7 fall on the weekend, made up on 2/9-2/10; 1/1/2028 is made up on 12/31
    (date(2027, 1, 1), "元旦"), (date(2027, 2, 4), "除夕前一日"), (date(2027, 2, 5), "除夕"), (date(2027, 2, 6), "春節"),
    (date(2027, 2, 9), "春節補假"), (date(2027, 2, 10), "春節補假"), (date(2027, 3, 1), "和平紀念日補假"),
    (date(2027, 4, 5), "清明節"), (date(2027, 4, 6), "兒童節補假"), (date(2027, 4, 30), "勞動節補假"),
    (date(2027, 6, 9), "端午節"), (date(2027, 9, 15), "中秋節"), (date(2027, 10, 11), "國慶日補假"),
    (date(2027, 12, 24), "行憲紀念日補假"), (date(2027, 12, 31), "元旦補假"),
])
def test_official_holidays_and_make_up_days(day, name):
    assert name in context_info.days_of(day.year, "zh-TW")[day]


@pytest.mark.parametrize("year, total", [(2026, 120), (2027, 121)])
def test_days_off_add_up_to_the_dgpa_total(year, total):
    """The DGPA calendars give 120 days off in 2026 and 121 in 2027, weekends included."""
    official = {day for day, names in context_info.days_of(year, "zh-TW").items() if set(names) - FESTIVALS}
    days = [date(year, 1, 1) + timedelta(days=n) for n in range(366)]
    assert sum(1 for day in days if day.year == year and (day.weekday() >= 5 or day in official)) == total


@pytest.mark.parametrize("day, name", [
    (date(2026, 3, 3), "元宵節"), (date(2026, 8, 19), "七夕"), (date(2026, 8, 27), "中元節"),
    (date(2026, 10, 18), "重陽節"), (date(2026, 12, 22), "冬至"), (date(2025, 12, 21), "冬至"),
    (date(2027, 1, 23), "尾牙"), (date(2027, 2, 20), "元宵節"), (date(2027, 8, 8), "七夕"), (date(2027, 8, 8), "父親節"),
    (date(2027, 10, 8), "重陽節"), (date(2027, 12, 22), "冬至"), (date(2026, 5, 10), "母親節"),
    (date(2027, 5, 9), "母親節"), (date(2026, 8, 23), "祖父母節"),
])
def test_traditional_festivals(day, name):
    assert name in context_info.days_of(day.year, "zh-TW")[day]


def test_the_day_before_new_years_eve_is_not_called_new_years_eve():
    assert context_info.days_of(2026, "zh-TW")[date(2026, 2, 15)] == ("除夕前一日",)
    assert context_info.days_of(2026, "zh-TW")[date(2026, 2, 16)] == ("除夕",)
    assert context_info.days_of(2026, "en")[date(2026, 2, 15)] == ("the day before Lunar New Year's Eve",)


def test_english_holiday_names():
    days = context_info.days_of(2026, "en")
    assert days[date(2026, 9, 25)] == ("Mid-Autumn Festival",)
    assert days[date(2026, 10, 9)] == ("National Day (day off in lieu)",)
    assert days[date(2026, 10, 18)] == ("Double Ninth Festival",)
    assert days[date(2026, 2, 20)] == ("Lunar New Year (day off in lieu)",)


@pytest.mark.parametrize("day, line", [
    # The make-up day goes with its holiday: listed first on its own, the model said 國慶日 was 6 days away.
    (date(2026, 10, 3), "節日：今天沒有節日。接下來：10月10日（星期六）國慶日，還有7天（10月9日星期五補假）。"),
    (date(2026, 10, 24), "節日：今天沒有節日。接下來：10月25日（星期日）臺灣光復節，就是明天（10月26日星期一補假）。"),
    # its holiday is today, so the make-up day stands alone
    (date(2026, 10, 25), "節日：今天是臺灣光復節。接下來：10月26日（星期一）臺灣光復節補假，就是明天。"),
    (date(2027, 2, 5), "節日：今天是除夕。接下來：2月6日（星期六）春節，就是明天（2月9日星期二、2月10日星期三補假）；"
                       "2月7日（星期日）春節，還有2天。"),
    (date(2026, 3, 25), "節日：今天沒有節日。接下來：4月4日（星期六）兒童節，還有10天（4月3日星期五補假）；"
                        "4月5日（星期日）清明節，還有11天（4月6日星期一補假）。"),
    (date(2026, 9, 24), "節日：今天沒有節日。接下來：9月25日（星期五）中秋節，就是明天；9月28日（星期一）教師節，還有4天。"),
    # fourteen days ahead still counts; its holiday, 15 days ahead, does not
    (date(2026, 9, 25), "節日：今天是中秋節。接下來：9月28日（星期一）教師節，還有3天；10月9日（星期五）國慶日補假，還有14天。"),
    (date(2026, 10, 12), "節日：今天沒有節日。接下來：10月18日（星期日）重陽節，還有6天；"
                         "10月25日（星期日）臺灣光復節，還有13天（10月26日星期一補假）。"),
    (date(2026, 12, 28), "節日：今天沒有節日。接下來：1月1日（星期五）元旦，還有4天。"),   # into next year
    (date(2027, 12, 25), "節日：今天是行憲紀念日。接下來：1月1日（星期六）元旦，還有7天（12月31日星期五補假）。"),
    (date(2026, 7, 1), "節日：今天沒有節日。接下來兩週沒有節日。"),
    (date(2027, 8, 8), "節日：今天是七夕、父親節。接下來：8月16日（星期一）中元節，還有8天；8月22日（星期日）祖父母節，還有14天。"),
])
def test_the_holiday_line(day, line):
    assert context_info.background("zh-TW", at(day.year, day.month, day.day, 9, 0)).splitlines()[-1] == line


def test_the_english_holiday_line():
    assert context_info.background("en", at(2026, 9, 24, 9, 0)).splitlines()[-1] == (
        "Holidays: none today. Coming up: Mid-Autumn Festival on Friday 25 September, tomorrow; "
        "Teachers' Day on Monday 28 September, in 4 days.")
    assert context_info.background("en", at(2026, 10, 3, 9, 0)).splitlines()[-1] == (
        "Holidays: none today. Coming up: National Day on Saturday 10 October, in 7 days "
        "(day off in lieu on Friday 9 October).")


def test_broken_holidays_leave_just_that_line_out(monkeypatch):
    def broken(year, language):
        raise RuntimeError("no holiday table")

    monkeypatch.setattr(context_info, "days_of", broken)
    _cached(at(2026, 10, 3, 1, 30))
    lines = context_info.background("zh-TW", at(2026, 10, 3, 1, 45)).splitlines()
    assert len(lines) == 2 and lines[0].startswith("現在是") and lines[1].startswith("天氣：現在24度")


# ── weather ──

def test_the_weather_line_from_a_fresh_forecast():
    _cached(at(2026, 10, 3, 1, 30))
    assert context_info.background("zh-TW", at(2026, 10, 3, 1, 45)).splitlines()[1] == (
        "天氣：現在24度，雷雨；今天24到29度，雷雨，降雨機率79%；明天24到30度，毛毛雨，降雨機率77%。")
    assert context_info.background("en", at(2026, 10, 3, 1, 45)).splitlines()[1] == (
        "Local weather: now 24°C, thunderstorms; today 24-29°C, thunderstorms, 79% chance of rain; "
        "tomorrow 24-30°C, drizzle, 77% chance of rain.")


def test_the_place_never_goes_to_the_model(monkeypatch):
    """The robot notice says only the conversation's text goes to the language model: no town name."""
    monkeypatch.setattr(config, "WEATHER_PLACE", "新北市三峽區")
    _cached(at(2026, 10, 3, 1, 30))
    for language in ("zh-TW", "en"):
        block = context_info.background(language, at(2026, 10, 3, 1, 45))
        assert "三峽" not in block and "台北" not in block and "Taipei" not in block and "24" in block


def test_after_midnight_today_and_tomorrow_move_on():
    _cached(at(2026, 10, 3, 23, 50))
    assert context_info.background("zh-TW", at(2026, 10, 4, 0, 10)).splitlines()[1] == (
        "天氣：現在24度，雷雨；今天24到30度，毛毛雨，降雨機率77%；明天24到29度，毛毛雨，降雨機率90%。")


def test_the_current_temperature_is_left_out_after_an_hour():
    _cached(at(2026, 10, 3, 1, 30))
    line = context_info.background("zh-TW", at(2026, 10, 3, 2, 31)).splitlines()[1]
    assert line == "天氣：今天24到29度，雷雨，降雨機率79%；明天24到30度，毛毛雨，降雨機率77%。"


@pytest.mark.parametrize("language, missing", [
    ("zh-TW", "天氣：不知道，沒有拿到預報；可以請對方看看窗外或問家人。"),
    ("en", "Weather: unknown, no forecast available; they could look outside or ask family."),
])
def test_a_stale_or_missing_forecast_is_left_out(language, missing):
    assert context_info.background(language, at(2026, 10, 3, 1, 45)).splitlines()[1] == missing   # never fetched
    _cached(at(2026, 10, 3, 1, 30))
    assert "24" in context_info.background(language, at(2026, 10, 3, 4, 30)).splitlines()[1]       # 3 h: still in
    stale = context_info.background(language, at(2026, 10, 3, 4, 31))
    assert stale.splitlines()[1] == missing and "29" not in stale
    _cached(at(2026, 10, 1, 1, 30))   # days old, and today is not in it anyway
    assert context_info.background(language, at(2026, 10, 3, 1, 45)).splitlines()[1] == missing


def test_refresh_sends_only_the_configured_place(place, monkeypatch):
    monkeypatch.setattr(context_info, "local_now", lambda: at(2026, 10, 3, 1, 31))
    place.answers.append(Response(OPEN_METEO))
    context_info.refresh()
    assert len(place.calls) == 1 and place.calls[0]["url"] == "https://api.open-meteo.com/v1/forecast"
    assert place.calls[0]["params"] == {
        "latitude": 25.033, "longitude": 121.5654, "current": "temperature_2m,weather_code",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "timezone": "Asia/Taipei", "forecast_days": 3}
    assert place.calls[0]["timeout"] and context_info._weather["fetched_at"] == at(2026, 10, 3, 1, 31)
    assert context_info._weather["days"]["2026-10-04"] == {"max": 30.4, "min": 23.9, "rain": 77, "code": 55}


@pytest.mark.parametrize("failure", [requests.ConnectionError("offline"), Response({"error": True}, status=400),
                                     Response({})])
def test_a_failed_fetch_keeps_the_last_forecast_until_it_is_stale(place, failure):
    _cached(at(2026, 10, 3, 1, 30))
    kept = context_info._weather
    place.answers.append(failure)
    context_info.refresh()
    assert len(place.calls) == 1 and context_info._weather is kept


@pytest.mark.parametrize("setting, value", [("WEATHER_ENABLED", False), ("WEATHER_LATITUDE", ""),
                                            ("WEATHER_LONGITUDE", "")])
def test_weather_can_be_turned_off(place, monkeypatch, setting, value):
    _cached(at(2026, 10, 3, 1, 30))
    monkeypatch.setattr(config, setting, value)
    context_info.refresh()
    assert place.calls == []
    assert context_info.background("zh-TW", at(2026, 10, 3, 1, 45)).splitlines()[1] == context_info.NO_WEATHER["zh-TW"]


def test_the_background_never_touches_the_network(place):
    context_info.days_of(2026, "zh-TW")   # built ahead, as refresh() does at startup
    context_info.days_of(2026, "en")
    for language in ("zh-TW", "en"):
        started = time.perf_counter()
        context_info.background(language, at(2026, 10, 3, 9, 0))
        assert time.perf_counter() - started < 0.1   # runs on the event loop the robot's frames share
    assert place.calls == []


def test_the_block_stays_short():
    """A busy day: fresh weather, a festival today and two more coming."""
    body = {**OPEN_METEO, "daily": {**OPEN_METEO["daily"], "time": ["2026-09-25", "2026-09-26", "2026-09-27"]}}
    _cached(at(2026, 9, 25, 9, 0), body)
    for language, most in (("zh-TW", 200), ("en", 420)):
        block = context_info.background(language, at(2026, 9, 25, 9, 10))
        assert len(block.splitlines()) == 3 and len(block) <= most, block


def test_the_weather_is_fetched_every_30_minutes_and_once_at_startup(monkeypatch):
    from app.jobs import scheduler as jobs

    added = {}

    class Scheduler:
        def add_job(self, function, trigger, id, **kwargs):
            added[id] = (function, trigger, kwargs)

        def start(self):
            pass

    monkeypatch.setattr(jobs, "scheduler", Scheduler())
    jobs.start_scheduler()
    function, trigger, kwargs = added["checkin_background"]
    assert function is context_info.refresh and trigger.interval == timedelta(minutes=30)
    assert abs(kwargs["next_run_time"] - datetime.now(TAIPEI)) < timedelta(minutes=1)
    # a run that starts late still runs, once
    assert kwargs["misfire_grace_time"] is None and kwargs["coalesce"] is True
    # a plain function: APScheduler runs it in a worker thread, so startup and replies never wait on the fetch
    assert not asyncio.iscoroutinefunction(function)


def test_a_busy_startup_still_fetches_the_weather(monkeypatch):
    """The real scheduler, with the event loop held for 1.5 s right after it starts (as the rest of startup may):
    with APScheduler's default 1 s misfire limit the first run was skipped, and the weather waited 30 minutes."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from app.jobs import scheduler as jobs

    ran = []
    monkeypatch.setattr(context_info, "refresh", lambda: ran.append(datetime.now(TAIPEI)))
    real = AsyncIOScheduler(timezone=jobs.TZ_TAIPEI)
    monkeypatch.setattr(jobs, "scheduler", real)

    async def startup():
        jobs.start_scheduler()
        time.sleep(1.5)   # the event loop is busy
        for _ in range(50):
            await asyncio.sleep(0.1)
            if ran:
                break
        real.shutdown(wait=False)

    asyncio.run(startup())
    assert len(ran) == 1
