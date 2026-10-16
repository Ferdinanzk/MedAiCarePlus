"""The weather anywhere in Taiwan, for the GPT-Live agent's get_weather tool (tool_registry.py).

The place the patient named, in Chinese or English (a city, county, district, township or town: 花蓮市, 壽豐鄉,
信義區, Taichung, Xinyi District) is resolved with OpenStreetMap's Nominatim, whose Taiwan data carries the official
Chinese and English names down to every 鄉鎮市區. Open-Meteo's own geocoder was tried first and knows few of the
Chinese names (台中, 澎湖, 壽豐鄉, 信義區 and 新北 gave it nothing on 13 Oct 2026). The forecast then comes from
Open-Meteo for those coordinates, fresh at every call: the check-in replies' weather (context_info.py) is the
configured home place's, refreshed in the background, and is not used here.

A name that fits more than one place (信義區 is in Taipei and in Keelung, 大安區 in Taipei and Taichung) is answered
as `ambiguous` with the candidates, so the Live model asks which; a name that fits a county and its namesake city
(花蓮縣 and 花蓮市, 嘉義縣 and 嘉義市) means the city. Nothing falls back to Taipei: no place is `not_found`, and the
home place is used only when the tool is called without a location, and says so (location_source).

Nominatim's usage policy: one request a second at most and a User-Agent that names the application. Requests are
spaced by NOMINATIM_GAP_SECONDS and resolved places are kept for GEOCODE_CACHE_SECONDS (a place does not move; the
weather is never cached). Only the place name goes to Nominatim, only coordinates go to Open-Meteo.
"""

import logging
import re
import threading
import time
import unicodedata

import requests

from app import config
from app.services.context_info import OPEN_METEO_URL, WEATHER_TIMEOUT, WEATHER_WORDS

log = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_TIMEOUT = 12
NOMINATIM_GAP_SECONDS = 1.1
USER_AGENT = "MedAiCarePlus/1.0 (care robot weather lookup)"
GEOCODE_CACHE_SECONDS = 24 * 3600
GEOCODE_CACHE_LIMIT = 300
CANDIDATES = 8                 # places Nominatim is asked for (the first is its best match)
MAX_AMBIGUOUS = 4              # candidates the Live model is offered
RAIN_HOURS = 6                 # rain_probability: the highest chance within the next hours
TAIWAN = {"zh-TW": "台灣", "en": "Taiwan"}
# What people say that is not part of the place's name: Chinese anywhere, English only as whole words (Tainan,
# Pingtung and Xinyi contain "in"; Taitung "at").
TRIM_WORDS = re.compile(r"(的天氣|天氣|的氣溫|氣溫|今天|現在|明天|"
                        r"\b(?:what is|what's|how is|how's|weather in|weather for|weather|forecast|the|in|at|"
                        r"today|now)\b)", re.IGNORECASE)
PLACE_CATEGORIES = ("boundary", "place")                  # Nominatim results that are places, not stations or roads
SUFFIXES = ("市", "縣", "區", "鄉", "鎮", "島")
# A bare Chinese name often names a station or a road first (台中, 嘉義, 玉里 gave only railway stations on 13 Oct
# 2026, 新北 a highway): when no place answers the name as said, it is asked with these suffixes, in this order.
VARIANT_SUFFIXES = ("市", "縣", "區", "鄉", "鎮")
CJK = re.compile(r"[㐀-鿿]")

_geocode_lock = threading.Lock()
_geocode_cache: dict[str, tuple[float, dict]] = {}
_last_request_at = 0.0


def normalize(text: str) -> str:
    """The place as asked, cleaned for the geocoder: full-width characters narrowed, surrounding words such as
    天氣 or 'weather in' dropped, spaces collapsed."""
    text = unicodedata.normalize("NFKC", str(text or ""))
    text = TRIM_WORDS.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip(" ,，。?？!！")


def _base_name(name: str) -> str:
    return name[:-1] if len(name) > 1 and name.endswith(SUFFIXES) else name


def _fold(text: str) -> str:
    """For comparing names: 臺 and 台 alike, letters lowercase, no spaces or apostrophes."""
    return re.sub(r"[\s'’]", "", str(text or "").replace("臺", "台")).lower()


def _related(candidate: dict, asked: str) -> bool:
    """Whether the place answers the name asked, so a near match (佳義 for 嘉義, 白玉里 for 玉里) is never taken.
    A Chinese place name must begin with the name asked (臺中市 for 台中, 馬祖列島 for 馬祖) or end the name asked
    (信義區 for 台北信義區); an English one may contain it or be contained (Hualien City for Hualien). A place with no
    English name is trusted for a Latin question (its ranking is Nominatim's)."""
    wanted = _fold(asked)
    name, name_en = _fold(candidate["name"]), _fold(candidate.get("name_en"))
    if name and (name.startswith(wanted) or wanted.endswith(name)):
        return True
    if name_en and (name_en in wanted or wanted in name_en):
        return True
    return not CJK.search(asked) and not candidate.get("name_en")


def _exact(candidate: dict, asked: str) -> bool:
    wanted = _fold(asked)
    return wanted in (_fold(candidate["name"]), _fold(candidate.get("name_en")))


def _parent(address: dict) -> str:
    """The county or city a place belongs to, as Nominatim's address gives it; "" for a county or city itself."""
    for key in ("county", "city", "state"):
        value = address.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _candidate(entry: dict) -> dict | None:
    if not isinstance(entry, dict) or entry.get("category") not in PLACE_CATEGORIES:
        return None
    name = str(entry.get("name") or "").strip()
    try:
        lat, lon = float(entry["lat"]), float(entry["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    if not name:
        return None
    address = entry.get("address") if isinstance(entry.get("address"), dict) else {}
    names = entry.get("namedetails") if isinstance(entry.get("namedetails"), dict) else {}
    parent = _parent(address)
    if parent == name:
        parent = ""
    return {"name": name, "name_en": str(names.get("name:en") or "").strip() or None, "parent": parent,
            "kind": str(entry.get("addresstype") or entry.get("type") or ""),
            "admin": entry.get("category") == "boundary", "latitude": lat, "longitude": lon}


def choose(entries: list, asked: str) -> dict:
    """The place meant among Nominatim's answers for `asked` (its order is by importance): only places that answer
    the name (never a near match), administrative areas before other places, one entry per name and parent, the one
    named exactly before the others (Taipei: 臺北市, not 新北市), and a county and its namesake city mean the city.
    One left: {"status": "ok", "place"}; several: {"status": "ambiguous", "candidates"}; none: {"status":
    "not_found"}."""
    candidates = [c for c in (_candidate(entry) for entry in entries) if c is not None and _related(c, asked)]
    if any(c["admin"] for c in candidates):
        candidates = [c for c in candidates if c["admin"]]
    distinct: list[dict] = []
    for candidate in candidates:
        if not any(c["name"] == candidate["name"] and c["parent"] == candidate["parent"] for c in distinct):
            distinct.append(candidate)
    if len(distinct) > 1:
        exact = [c for c in distinct if _exact(c, asked)]
        if len(exact) == 1:
            distinct = exact
    if len(distinct) > 1:
        names, bases = {c["name"] for c in distinct}, {_base_name(c["name"]) for c in distinct}
        if len(names) > 1 and len(bases) == 1:   # 花蓮縣 and 花蓮市, 嘉義縣 and 嘉義市: the city, where people are
            cities = [c for c in distinct if c["name"].endswith("市")]
            distinct = cities[:1] if cities else distinct[:1]
    if len(distinct) > 1:
        whole = [c for c in distinct if not c["parent"]]   # Taoyuan: the city, not 桃園區 in it or 桃源區 in 高雄市
        if len(whole) == 1:
            distinct = whole
        # the same name in two places (信義區 in 臺北市 and 基隆市) stays ambiguous
    if not distinct:
        return {"status": "not_found"}
    if len(distinct) > 1:
        return {"status": "ambiguous",
                "candidates": [{"name": c["name"], "parent": c["parent"], "kind": c["kind"]}
                               for c in distinct[:MAX_AMBIGUOUS]]}
    return {"status": "ok", "place": distinct[0]}


def _nominatim(query: str) -> list:
    global _last_request_at
    with _geocode_lock:
        wait = _last_request_at + NOMINATIM_GAP_SECONDS - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            response = requests.get(NOMINATIM_URL, timeout=NOMINATIM_TIMEOUT, headers={"User-Agent": USER_AGENT},
                                    params={"q": query, "countrycodes": "tw", "format": "jsonv2",
                                            "addressdetails": 1, "namedetails": 1, "accept-language": "zh-TW,en",
                                            "limit": CANDIDATES})
        finally:
            _last_request_at = time.monotonic()
    response.raise_for_status()
    body = response.json()
    return body if isinstance(body, list) else []


def geocode(place: str) -> dict:
    """Resolve a place in Taiwan: choose() over Nominatim's answers for the normalized name, remembered for
    GEOCODE_CACHE_SECONDS. {"status": "geocoder_unavailable", "error"} when Nominatim cannot be reached."""
    query = normalize(place)
    if not query:
        return {"status": "not_found"}
    now = time.monotonic()
    cached = _geocode_cache.get(query)
    if cached is not None and cached[0] > now:
        return cached[1]
    variants = [query]
    if CJK.search(query) and not query.endswith(SUFFIXES):
        variants += [query + suffix for suffix in VARIANT_SUFFIXES]
    result = {"status": "not_found"}
    for variant in variants:
        try:
            entries = _nominatim(variant)
        except Exception as error:
            log.warning("weather lookup: the geocoder did not answer for %r (%s)", variant, type(error).__name__)
            return {"status": "geocoder_unavailable", "error": type(error).__name__}
        result = choose(entries, query)
        if result["status"] != "not_found":
            break
    if len(_geocode_cache) >= GEOCODE_CACHE_LIMIT:
        _geocode_cache.clear()
    _geocode_cache[query] = (now + GEOCODE_CACHE_SECONDS, result)
    return result


def _words(code, language: str) -> str | None:
    words = WEATHER_WORDS.get(code)
    return None if words is None else words[0 if language == "zh-TW" else 1]


def _round(value):
    return None if value is None else int(round(float(value)))


def parse_forecast(body: dict, language: str) -> dict:
    """The tool's weather fields from Open-Meteo's answer: now (temperature, feel, condition, humidity), today's
    high, low and rain chance, tomorrow's, and when the current values were observed."""
    current = body.get("current") or {}
    daily = body.get("daily") or {}
    hourly = body.get("hourly") or {}

    def day(key: str, i: int):
        values = daily.get(key) or []
        return values[i] if i < len(values) else None

    rain = None
    times, chances = hourly.get("time") or [], hourly.get("precipitation_probability") or []
    observed = current.get("time")
    if isinstance(observed, str) and times:
        upcoming = [chance for stamp, chance in zip(times, chances) if stamp >= observed[:13] and chance is not None]
        if upcoming:
            rain = max(upcoming[:RAIN_HOURS])
    if rain is None:
        rain = day("precipitation_probability_max", 0)
    result = {"temperature_c": _round(current.get("temperature_2m")),
              "feels_like_c": _round(current.get("apparent_temperature")),
              "condition": _words(current.get("weather_code"), language),
              "condition_en": _words(current.get("weather_code"), "en"),
              "humidity": _round(current.get("relative_humidity_2m")),
              "rain_probability": _round(rain),
              "high_c": _round(day("temperature_2m_max", 0)), "low_c": _round(day("temperature_2m_min", 0)),
              "observation_time": observed,
              "tomorrow": {"high_c": _round(day("temperature_2m_max", 1)), "low_c": _round(day("temperature_2m_min", 1)),
                           "rain_probability": _round(day("precipitation_probability_max", 1)),
                           "condition": _words(day("weather_code", 1), language)}}
    if result["temperature_c"] is None and result["high_c"] is None:
        raise ValueError("no forecast in Open-Meteo's answer")
    return result


def fetch_forecast(latitude: float, longitude: float, language: str) -> dict:
    """One fresh Open-Meteo forecast for the coordinates (no key; only the coordinates are sent)."""
    response = requests.get(OPEN_METEO_URL, timeout=WEATHER_TIMEOUT, params={
        "latitude": round(float(latitude), 4), "longitude": round(float(longitude), 4),
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code",
        "hourly": "precipitation_probability",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "timezone": config.MEDCARE_TIMEZONE, "forecast_days": 2})
    response.raise_for_status()
    return parse_forecast(response.json(), language)


def describe(place: dict, language: str) -> str:
    """The place for the reply: 花蓮市, 花蓮縣, 台灣 / Hualien City, 花蓮縣, Taiwan."""
    name = place["name"]
    if language != "zh-TW" and place.get("name_en"):
        name = place["name_en"]
    parts = [name]
    if place.get("parent"):
        parts.append(place["parent"])
    parts.append(TAIWAN.get(language, TAIWAN["en"]))
    return ", ".join(parts)


def lookup(location: str | None, language: str = "zh-TW") -> dict:
    """The weather for `location` (any place in Taiwan), fetched now. Without a location, the home place from the
    server's settings (WEATHER_PLACE and its coordinates), said so in location_source; with none configured,
    {"status": "no_location"}. Statuses: ok, ambiguous (candidates), not_found, no_location, geocoder_unavailable,
    weather_unavailable."""
    if location is None or not normalize(location):
        if not (config.WEATHER_LATITUDE and config.WEATHER_LONGITUDE):
            return {"status": "no_location"}
        try:
            latitude, longitude = float(config.WEATHER_LATITUDE), float(config.WEATHER_LONGITUDE)
        except ValueError:
            return {"status": "no_location"}
        place = {"name": config.WEATHER_PLACE or "home", "name_en": None, "parent": "", "latitude": latitude,
                 "longitude": longitude}
        source = "home_setting"
    else:
        found = geocode(location)
        if found["status"] != "ok":
            return {**found, "asked": normalize(location)}
        place, source = found["place"], "geocoded"
    try:
        weather = fetch_forecast(place["latitude"], place["longitude"], language)
    except Exception as error:
        log.warning("weather lookup: no forecast for %s (%s)", place["name"], type(error).__name__)
        return {"status": "weather_unavailable", "location": describe(place, language), "error": type(error).__name__}
    return {"status": "ok", "location": describe(place, language), "location_source": source,
            "latitude": round(place["latitude"], 3), "longitude": round(place["longitude"], 3), **weather}
