"""The weather for any place in Taiwan (services/weather_lookup.py): the name as asked is cleaned, resolved with
Nominatim (a county and its namesake city mean the city; a name in two cities is ambiguous; nothing falls back to
Taipei), and the forecast comes fresh from Open-Meteo. No network: requests.get is faked."""

import pytest

from app import config
from app.services import weather_lookup


def nominatim(name, kind, parent, lat, lon, category="boundary", name_en=None):
    address = {"county": parent} if parent and parent.endswith("縣") else ({"city": parent} if parent else {})
    return {"name": name, "category": category, "type": "administrative" if category == "boundary" else kind,
            "addresstype": kind, "lat": str(lat), "lon": str(lon), "address": address,
            "namedetails": {"name": name, **({"name:en": name_en} if name_en else {})}}


OPEN_METEO = {"current": {"time": "2026-10-13T23:15", "temperature_2m": 26.6, "apparent_temperature": 29.1,
                          "relative_humidity_2m": 78, "weather_code": 2},
              "hourly": {"time": ["2026-10-13T22:00", "2026-10-13T23:00", "2026-10-14T00:00", "2026-10-14T01:00"],
                         "precipitation_probability": [10, 20, 35, 15]},
              "daily": {"time": ["2026-10-13", "2026-10-14"], "temperature_2m_max": [30.2, 29.0],
                        "temperature_2m_min": [23.8, 23.1], "precipitation_probability_max": [40, 60],
                        "weather_code": [3, 61]}}


class Response:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.body


class Web:
    """requests.get: Nominatim answers by query, Open-Meteo by coordinates."""

    def __init__(self, places, forecast=OPEN_METEO):
        self.places, self.forecast, self.calls = places, forecast, []

    def get(self, url, timeout=None, params=None, headers=None):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        if url == weather_lookup.NOMINATIM_URL:
            answer = self.places.get(params["q"], [])
            return answer if isinstance(answer, Response) else Response(answer)
        if url == weather_lookup.OPEN_METEO_URL:
            return self.forecast if isinstance(self.forecast, Response) else Response(self.forecast)
        raise AssertionError(url)


@pytest.fixture
def web(monkeypatch):
    web = Web({
        "花蓮": [nominatim("花蓮縣", "county", "", 23.99, 121.62), nominatim("花蓮市", "town", "花蓮縣", 23.98, 121.61,
                                                                          name_en="Hualien City")],
        "壽豐鄉": [nominatim("壽豐鄉", "town", "花蓮縣", 23.87, 121.51, name_en="Shoufeng")],
        "信義區": [nominatim("信義區", "suburb", "臺北市", 25.03, 121.57), nominatim("信義區", "suburb", "基隆市", 25.13, 121.75)],
        "台北信義區": [nominatim("信義區", "suburb", "臺北市", 25.03, 121.57, name_en="Xinyi District")],
        "台中": [nominatim("台中", "railway", "臺中市", 24.11, 120.62, category="railway"),
               nominatim("臺中", "railway", "臺中市", 24.14, 120.69, category="railway")],
        "台中市": [nominatim("臺中市", "city", "", 24.14, 120.69, name_en="Taichung")],
        "嘉義": [nominatim("佳義", "hamlet", "屏東縣", 22.5, 120.6, category="place")],   # a near match, never taken
        "嘉義市": [nominatim("嘉義縣", "county", "", 23.46, 120.32, name_en="Chiayi County"),
                nominatim("嘉義市", "city", "", 23.48, 120.44, name_en="Chiayi City")],
        "Taipei": [nominatim("臺北市", "city", "", 25.04, 121.56, name_en="Taipei"),
                   nominatim("新北市", "city", "", 25.01, 121.47, name_en="New Taipei")],
        "Taoyuan": [nominatim("桃園市", "city", "", 24.99, 121.30, name_en="Taoyuan City"),
                    nominatim("桃源區", "suburb", "高雄市", 23.16, 120.76, name_en="Taoyuan District"),
                    nominatim("桃園區", "town", "桃園市", 25.00, 121.30, name_en="Taoyuan District")],
        "玉里": [nominatim("玉里", "railway", "花蓮縣", 23.33, 121.31, category="railway")],
        "玉里市": [nominatim("白玉里", "village", "桃園市", 24.9, 121.2)],              # a near match, never taken
        "玉里縣": [], "玉里區": [], "玉里鄉": [],
        "玉里鎮": [nominatim("玉里鎮", "town", "花蓮縣", 23.33, 121.32, name_en="Yuli")],
        "澎湖": [nominatim("澎湖縣", "county", "", 23.57, 119.57), nominatim("澎湖群島", "archipelago", "澎湖縣", 23.56, 119.61,
                                                                          category="place")],
        "馬祖": [nominatim("馬祖列島", "archipelago", "連江縣", 26.37, 120.50, category="place"),
               nominatim("連江縣", "county", "", 26.16, 119.95, name_en="Lienchiang County")],
        "金門": [nominatim("金門縣", "county", "", 24.45, 118.39), nominatim("金門縣", "county", "", 24.44, 118.32)],
        "Atlantis": [],
    })
    monkeypatch.setattr(weather_lookup.requests, "get", web.get)
    monkeypatch.setattr(weather_lookup.time, "sleep", lambda seconds: None)
    weather_lookup._geocode_cache.clear()
    return web


def test_a_place_is_cleaned_resolved_and_answered_with_the_live_forecast(web):
    result = weather_lookup.lookup("壽豐鄉的天氣", "zh-TW")
    assert result["status"] == "ok" and result["location"] == "壽豐鄉, 花蓮縣, 台灣"
    assert result["location_source"] == "geocoded" and (result["latitude"], result["longitude"]) == (23.87, 121.51)
    assert (result["temperature_c"], result["feels_like_c"], result["humidity"]) == (27, 29, 78)
    assert result["condition"] == "多雲" and result["condition_en"] == "partly cloudy"
    assert result["rain_probability"] == 35          # the highest chance within the next hours, from 23:00 on
    assert (result["high_c"], result["low_c"]) == (30, 24) and result["observation_time"] == "2026-10-13T23:15"
    assert result["tomorrow"] == {"high_c": 29, "low_c": 23, "rain_probability": 60, "condition": "小雨"}
    geocode, forecast = web.calls
    assert geocode[1]["q"] == "壽豐鄉" and geocode[1]["countrycodes"] == "tw" and "MedAiCarePlus" in geocode[2]["User-Agent"]
    assert forecast[1]["latitude"] == 23.87 and forecast[1]["timezone"] == config.MEDCARE_TIMEZONE


def test_english_answers_use_the_english_name_and_the_forecast_is_fetched_every_time(web):
    first = weather_lookup.lookup("花蓮", "en")
    second = weather_lookup.lookup("花蓮", "en")
    assert first["location"] == "Hualien City, 花蓮縣, Taiwan"   # the county and its city: the city
    assert first["condition"] == "partly cloudy"
    assert [call[0] for call in web.calls] == [weather_lookup.NOMINATIM_URL, weather_lookup.OPEN_METEO_URL,
                                               weather_lookup.OPEN_METEO_URL]   # the place is remembered, not the weather
    assert second == first


def test_a_name_in_two_cities_is_ambiguous_until_the_city_is_named(web):
    result = weather_lookup.lookup("信義區", "zh-TW")
    assert result["status"] == "ambiguous" and result["asked"] == "信義區"
    assert result["candidates"] == [{"name": "信義區", "parent": "臺北市", "kind": "suburb"},
                                    {"name": "信義區", "parent": "基隆市", "kind": "suburb"}]
    assert "temperature_c" not in result
    assert weather_lookup.lookup("台北信義區", "zh-TW")["location"] == "信義區, 臺北市, 台灣"


def test_stations_and_islands_give_way_to_the_administrative_area_and_duplicates_merge(web):
    assert weather_lookup.lookup("澎湖", "zh-TW")["location"] == "澎湖縣, 台灣"
    assert weather_lookup.lookup("金門", "zh-TW")["location"] == "金門縣, 台灣"
    assert weather_lookup.lookup("馬祖", "zh-TW")["location"] == "馬祖列島, 連江縣, 台灣"   # 連江縣 does not answer to 馬祖


def test_a_bare_chinese_name_that_names_a_station_is_asked_again_with_a_suffix_and_near_matches_are_refused(web):
    assert weather_lookup.lookup("台中", "en")["location"] == "Taichung, Taiwan"
    assert [call[1]["q"] for call in web.calls if call[0] == weather_lookup.NOMINATIM_URL] == ["台中", "台中市"]
    web.calls.clear()
    assert weather_lookup.lookup("嘉義", "zh-TW")["location"] == "嘉義市, 台灣"        # 佳義 is not 嘉義; the city
    assert [call[1]["q"] for call in web.calls if call[0] == weather_lookup.NOMINATIM_URL] == ["嘉義", "嘉義市"]
    assert weather_lookup.lookup("Taipei", "en")["location"] == "Taipei, Taiwan"     # the exact name, not 新北市
    assert weather_lookup.lookup("Taoyuan", "en")["location"] == "Taoyuan City, Taiwan"   # the city, not a district
    web.calls.clear()
    assert weather_lookup.lookup("玉里", "zh-TW")["location"] == "玉里鎮, 花蓮縣, 台灣"   # 白玉里 is not 玉里
    assert [call[1]["q"] for call in web.calls if call[0] == weather_lookup.NOMINATIM_URL] == [
        "玉里", "玉里市", "玉里縣", "玉里區", "玉里鄉", "玉里鎮"]


def test_an_unknown_place_is_not_found_and_never_becomes_taipei(web):
    result = weather_lookup.lookup("Atlantis", "en")
    assert result == {"status": "not_found", "asked": "Atlantis"}
    assert [call[0] for call in web.calls] == [weather_lookup.NOMINATIM_URL]   # no forecast, no Chinese variants


def test_without_a_location_the_home_place_is_used_and_said_so(web, monkeypatch):
    monkeypatch.setattr(config, "WEATHER_PLACE", "花蓮")
    monkeypatch.setattr(config, "WEATHER_LATITUDE", "23.98")
    monkeypatch.setattr(config, "WEATHER_LONGITUDE", "121.61")
    result = weather_lookup.lookup(None, "zh-TW")
    assert result["status"] == "ok" and result["location"] == "花蓮, 台灣" and result["location_source"] == "home_setting"
    assert [call[0] for call in web.calls] == [weather_lookup.OPEN_METEO_URL]
    monkeypatch.setattr(config, "WEATHER_LATITUDE", "")
    assert weather_lookup.lookup("", "zh-TW") == {"status": "no_location"}


def test_a_failing_geocoder_or_weather_service_is_reported_not_guessed(web):
    web.places["花蓮"] = Response({}, status=503)
    assert weather_lookup.lookup("花蓮", "zh-TW")["status"] == "geocoder_unavailable"
    web.forecast = Response({}, status=500)
    result = weather_lookup.lookup("壽豐鄉", "zh-TW")
    assert result["status"] == "weather_unavailable" and result["location"] == "壽豐鄉, 花蓮縣, 台灣"
    web.forecast = Response({"current": {}, "daily": {}})
    assert weather_lookup.lookup("壽豐鄉", "zh-TW")["status"] == "weather_unavailable"


def test_normalize_drops_the_words_around_the_place():
    assert weather_lookup.normalize("  今天花蓮市的天氣？") == "花蓮市"
    assert weather_lookup.normalize("What is the weather in Hualien City") == "Hualien City"
    assert weather_lookup.normalize("weather in Taitung") == "Taitung"
    assert weather_lookup.normalize("Tainan") == "Tainan" and weather_lookup.normalize("Pingtung") == "Pingtung"
    assert weather_lookup.normalize("ＴＡＩＰＥＩ") == "TAIPEI"
    assert weather_lookup.normalize("") == "" and weather_lookup.normalize(None) == ""
