"""The time now, computed when asked, for the live agent's get_current_time tool (14 Oct 2026).

GPT-Live's instructions carry the moment its session started (context_info.background), and nothing refreshes them
while the session runs: at 13:56:30 that day a patient asked 「現在幾點了」 and Reachy said 「現在下午一點五十三分」, the
start of that stretch. So the time is never read from the instructions any more (the date line says it is the start,
conversation.reply_prompt with tools) but asked for through this tool, which reads the clock at the call.

`answer(place, language)` gives the time at home (config.MEDCARE_TIMEZONE) or in the place named, in Chinese or
English (「東京現在幾點」, "what time is it in Tokyo", "Asia/Tokyo"): the question words are dropped, a place in Taiwan
is home, Chinese names come from a table, English city names from the zone table itself (Asia/Tokyo is "tokyo") and
country names from a small table; a country with several zones (美國, Australia) is `ambiguous` with the choices, and
anything else `not_found`. The spoken forms follow context_info (「下午2點25分」: written 「14:25」 the model misread the
period), and a place away from home also gets the home time, so Reachy can say both.
"""

import re
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo, available_timezones

from app import config
from app.services import context_info
from app.services.conversation import _traditional

HOME_WORDS = ("台灣", "臺灣", "台北", "臺北", "新北", "桃園", "新竹", "苗栗", "台中", "臺中", "彰化", "南投", "雲林", "嘉義",
              "台南", "臺南", "高雄", "屏東", "宜蘭", "花蓮", "台東", "臺東", "基隆", "澎湖", "金門", "馬祖", "這裡", "這邊",
              "家裡", "我們這", "taiwan", "taipei", "kaohsiung", "taichung", "tainan", "hualien", "taitung", "here", "home",
              "local")
CHINESE = {
    "東京": "Asia/Tokyo", "日本": "Asia/Tokyo", "大阪": "Asia/Tokyo", "京都": "Asia/Tokyo", "北海道": "Asia/Tokyo",
    "沖繩": "Asia/Tokyo", "首爾": "Asia/Seoul", "韓國": "Asia/Seoul", "北京": "Asia/Shanghai", "上海": "Asia/Shanghai",
    "大陸": "Asia/Shanghai", "中國": "Asia/Shanghai", "廣州": "Asia/Shanghai", "深圳": "Asia/Shanghai",
    "香港": "Asia/Hong_Kong", "澳門": "Asia/Macau", "新加坡": "Asia/Singapore", "曼谷": "Asia/Bangkok",
    "泰國": "Asia/Bangkok", "馬尼拉": "Asia/Manila", "菲律賓": "Asia/Manila", "雅加達": "Asia/Jakarta",
    "印尼": "Asia/Jakarta", "吉隆坡": "Asia/Kuala_Lumpur", "馬來西亞": "Asia/Kuala_Lumpur", "河內": "Asia/Ho_Chi_Minh",
    "胡志明": "Asia/Ho_Chi_Minh", "越南": "Asia/Ho_Chi_Minh", "印度": "Asia/Kolkata", "新德里": "Asia/Kolkata",
    "杜拜": "Asia/Dubai", "阿聯": "Asia/Dubai", "莫斯科": "Europe/Moscow", "俄羅斯": "Europe/Moscow",
    "倫敦": "Europe/London", "英國": "Europe/London", "巴黎": "Europe/Paris", "法國": "Europe/Paris",
    "柏林": "Europe/Berlin", "德國": "Europe/Berlin", "羅馬": "Europe/Rome", "義大利": "Europe/Rome",
    "馬德里": "Europe/Madrid", "西班牙": "Europe/Madrid", "阿姆斯特丹": "Europe/Amsterdam", "荷蘭": "Europe/Amsterdam",
    "瑞士": "Europe/Zurich", "蘇黎世": "Europe/Zurich", "雪梨": "Australia/Sydney", "悉尼": "Australia/Sydney",
    "墨爾本": "Australia/Melbourne", "伯斯": "Australia/Perth", "布里斯本": "Australia/Brisbane",
    "奧克蘭": "Pacific/Auckland", "紐西蘭": "Pacific/Auckland", "夏威夷": "Pacific/Honolulu", "檀香山": "Pacific/Honolulu",
    "紐約": "America/New_York", "華盛頓": "America/New_York", "波士頓": "America/New_York", "邁阿密": "America/New_York",
    "芝加哥": "America/Chicago", "休士頓": "America/Chicago", "達拉斯": "America/Chicago", "丹佛": "America/Denver",
    "洛杉磯": "America/Los_Angeles", "舊金山": "America/Los_Angeles", "西雅圖": "America/Los_Angeles",
    "拉斯維加斯": "America/Los_Angeles", "溫哥華": "America/Vancouver", "多倫多": "America/Toronto",
    "加拿大": ("America/Toronto", "America/Vancouver"), "美國": ("America/New_York", "America/Chicago",
                                                               "America/Denver", "America/Los_Angeles"),
    "澳洲": ("Australia/Sydney", "Australia/Perth"), "巴西": "America/Sao_Paulo", "聖保羅": "America/Sao_Paulo",
    "墨西哥": "America/Mexico_City", "開羅": "Africa/Cairo", "埃及": "Africa/Cairo", "伊斯坦堡": "Europe/Istanbul",
    "土耳其": "Europe/Istanbul",
}
COUNTRIES = {
    "japan": "Asia/Tokyo", "korea": "Asia/Seoul", "south korea": "Asia/Seoul", "china": "Asia/Shanghai",
    "mainland china": "Asia/Shanghai", "hongkong": "Asia/Hong_Kong", "hong kong": "Asia/Hong_Kong",
    "macau": "Asia/Macau", "macao": "Asia/Macau", "thailand": "Asia/Bangkok", "philippines": "Asia/Manila",
    "indonesia": "Asia/Jakarta", "malaysia": "Asia/Kuala_Lumpur", "vietnam": "Asia/Ho_Chi_Minh", "india": "Asia/Kolkata",
    "uae": "Asia/Dubai", "emirates": "Asia/Dubai", "russia": "Europe/Moscow", "uk": "Europe/London",
    "england": "Europe/London", "britain": "Europe/London", "united kingdom": "Europe/London", "france": "Europe/Paris",
    "germany": "Europe/Berlin", "italy": "Europe/Rome", "spain": "Europe/Madrid", "netherlands": "Europe/Amsterdam",
    "holland": "Europe/Amsterdam", "switzerland": "Europe/Zurich", "turkey": "Europe/Istanbul", "egypt": "Africa/Cairo",
    "new zealand": "Pacific/Auckland", "hawaii": "Pacific/Honolulu", "brazil": "America/Sao_Paulo",
    "mexico": "America/Mexico_City", "san francisco": "America/Los_Angeles", "seattle": "America/Los_Angeles",
    "las vegas": "America/Los_Angeles", "washington": "America/New_York", "boston": "America/New_York",
    "miami": "America/New_York", "houston": "America/Chicago", "dallas": "America/Chicago",
    "usa": ("America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles"),
    "us": ("America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles"),
    "america": ("America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles"),
    "united states": ("America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles"),
    "canada": ("America/Toronto", "America/Vancouver"), "australia": ("Australia/Sydney", "Australia/Perth"),
}
LABELS = {"Asia/Tokyo": ("東京", "Tokyo"), "Asia/Seoul": ("首爾", "Seoul"), "Asia/Shanghai": ("北京", "Beijing"),
          "Asia/Hong_Kong": ("香港", "Hong Kong"), "Asia/Singapore": ("新加坡", "Singapore"), "Asia/Bangkok": ("曼谷", "Bangkok"),
          "Europe/London": ("倫敦", "London"), "Europe/Paris": ("巴黎", "Paris"), "Europe/Berlin": ("柏林", "Berlin"),
          "Australia/Sydney": ("雪梨", "Sydney"), "Australia/Perth": ("伯斯", "Perth"), "America/New_York": ("紐約", "New York"),
          "America/Chicago": ("芝加哥", "Chicago"), "America/Denver": ("丹佛", "Denver"),
          "America/Los_Angeles": ("洛杉磯", "Los Angeles"), "America/Toronto": ("多倫多", "Toronto"),
          "America/Vancouver": ("溫哥華", "Vancouver"), "Pacific/Auckland": ("奧克蘭", "Auckland"),
          "Pacific/Honolulu": ("夏威夷", "Hawaii")}
# The question around the place (「東京現在幾點」, "what time is it in Tokyo now"), dropped before the lookup
QUESTION_ZH = re.compile(r"(現在|目前|今天|今日|那邊|那裡|這邊|這裡|的|是|時間|幾點|幾點鐘|點鐘|幾號|幾月|星期幾|禮拜幾|什麼時候|什麼日子|"
                         r"請問|告訴我|我想知道|你知道|知道|嗎|呢|了|啊|喔|哦|啦|呀|和|跟|與|在|rishi|reachy|日希|瑞奇|瑞希|"
                         r"richy|richie)")
QUESTION_EN = re.compile(r"\b(what|whats|what's|is|the|time|date|day|it|now|right|currently|in|at|please|tell|me|do|you|"
                         r"know|over|there|here|today|and|reachy|rishi|richy)\b")
_PUNCT = re.compile(r"[^\w\s]")


def _cities() -> dict[str, str]:
    """English city names from the zone table: "tokyo" -> Asia/Tokyo, "new york" -> America/New_York."""
    out: dict[str, str] = {}
    for zone in sorted(available_timezones()):
        if "/" not in zone or zone.startswith(("Etc/", "US/", "SystemV/", "Canada/", "Brazil/", "Mexico/", "Chile/")):
            continue
        city = zone.rsplit("/", 1)[1].lower().replace("_", " ")
        out.setdefault(city, zone)
    out.update(COUNTRIES)
    return out


_CITIES: dict[str, str] | None = None


def _normalize(place: str) -> tuple[str, str]:
    """(chinese, english): the place as written, Traditional and without the question words; and lower-case ASCII
    without the English question words."""
    text = unicodedata.normalize("NFKC", place or "").strip()
    chinese = QUESTION_ZH.sub("", _PUNCT.sub("", _traditional(text).lower()))
    chinese = re.sub(r"[a-z0-9\s]+", "", chinese)                       # ASCII words are the English side's
    english = re.sub(r"[^a-z0-9 ]+", " ", _PUNCT.sub(" ", text.lower()))
    english = " ".join(QUESTION_EN.sub(" ", english).split())
    return chinese, english


def resolve(place: str | None) -> tuple[str, str | tuple | None]:
    """("home", zone) for no place or a place in Taiwan; ("ok", zone); ("ambiguous", (zones...)); ("not_found", None)."""
    global _CITIES
    home = config.MEDCARE_TIMEZONE
    if not place or not str(place).strip():
        return "home", home
    chinese, english = _normalize(str(place))
    if not chinese and not english:
        return "home", home
    if any(word in chinese or word in english for word in HOME_WORDS):
        return "home", home
    for name, zone in CHINESE.items():
        if name in chinese:
            return ("ambiguous" if isinstance(zone, tuple) else "ok"), zone
    if _CITIES is None:
        _CITIES = _cities()
    raw = str(place).strip()
    if raw in available_timezones():
        return "ok", raw
    for key in sorted(_CITIES, key=len, reverse=True):   # the longest name first: "new york" before "york"
        if key == english or f" {key} " in f" {english} ":
            zone = _CITIES[key]
            return ("ambiguous" if isinstance(zone, tuple) else "ok"), zone
    return "not_found", None


def _label(zone: str, language: str) -> str:
    pair = LABELS.get(zone)
    if pair:
        return pair[0] if language == "zh-TW" else pair[1]
    return zone.rsplit("/", 1)[-1].replace("_", " ")


def describe(now: datetime, language: str, *, lunar: bool) -> dict:
    """One moment in words: the 24-hour time, the weekday, and the spoken form (context_info's period and clock)."""
    period_zh, period_en = context_info.time_of_day(now.hour)
    spoken_zh = f"{period_zh}{context_info.clock(now.hour, now.minute)}"
    spoken_en = f"{now.hour % 12 or 12}:{now.minute:02d} {period_en}"
    out = {"iso": now.isoformat(timespec="seconds"), "date": now.date().isoformat(), "time": now.strftime("%H:%M"),
           "weekday": context_info.WEEKDAYS[language][now.weekday()],
           "spoken": spoken_zh if language == "zh-TW" else spoken_en}
    if lunar:
        pair = context_info._safely(context_info.lunar_date, now.date())
        if pair:
            out["lunar_date"] = pair[0] if language == "zh-TW" else pair[1]
    return out


def answer(place: str | None, language: str, now: datetime | None = None) -> dict:
    """The tool's answer: status home / ok / ambiguous / not_found, the zone and place, and the time there now, read
    from the clock at this call. Away from home the home time comes too."""
    language = "zh-TW" if language == "zh-TW" else "en"
    status, zone = resolve(place)
    home = config.MEDCARE_TIMEZONE
    moment = now or datetime.now(ZoneInfo(home))
    if status == "ambiguous":
        return {"status": "ambiguous", "place": str(place).strip(), "candidates": [
            {"place": _label(z, language), "timezone": z, **describe(moment.astimezone(ZoneInfo(z)), language, lunar=False)}
            for z in zone]}
    if status == "not_found":
        return {"status": "not_found", "place": str(place).strip(), "note": "unknown place; ask for the city or country"}
    local = moment.astimezone(ZoneInfo(zone))
    out = {"status": status, "place": (_label(zone, language) if status == "ok" else ("這裡" if language == "zh-TW" else "here")),
           "timezone": zone, "home": status == "home", **describe(local, language, lunar=status == "home")}
    if status != "home":
        here = moment.astimezone(ZoneInfo(home))
        out["home_time"] = describe(here, language, lunar=False)
        out["hours_from_home"] = round((local.utcoffset() - here.utcoffset()).total_seconds() / 3600, 1)
    return out
