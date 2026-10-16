"""The live agent's get_current_time tool (services/current_time.py): the time read at the call, here or in the place
named, in either language, and the places it resolves (Taiwan is home; Chinese names from the table; English city
and country names; IANA zones; several zones -> ambiguous; unknown -> not_found)."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import config
from app.services import current_time

NOW = datetime(2026, 10, 14, 14, 25, 30, tzinfo=ZoneInfo("Asia/Taipei"))


@pytest.mark.parametrize("place", [None, "", "台北", "臺灣", "Taipei", "Taiwan", "這裡", "現在幾點", "what time is it",
                                   "Rishi，現在幾點了", "今天星期幾"])
def test_home_is_home_in_every_wording(place):
    status, zone = current_time.resolve(place)
    assert (status, zone) == ("home", config.MEDCARE_TIMEZONE)


@pytest.mark.parametrize("place, zone", [
    ("東京", "Asia/Tokyo"), ("日本現在幾點", "Asia/Tokyo"), ("東京現在幾點", "Asia/Tokyo"), ("Tokyo", "Asia/Tokyo"),
    ("what time is it in Tokyo", "Asia/Tokyo"), ("Asia/Tokyo", "Asia/Tokyo"), ("紐約", "America/New_York"),
    ("New York", "America/New_York"), ("請問倫敦現在幾點", "Europe/London"), ("london", "Europe/London"),
    ("paris", "Europe/Paris"), ("is it late in Sydney", "Australia/Sydney"), ("japan", "Asia/Tokyo"),
    ("香港", "Asia/Hong_Kong"), ("hong kong", "Asia/Hong_Kong"), ("洛杉磯", "America/Los_Angeles"),
    ("los angeles", "America/Los_Angeles"), ("UK", "Europe/London"), ("新加坡現在的時間", "Asia/Singapore"),
])
def test_places_resolve_to_their_zone(place, zone):
    assert current_time.resolve(place) == ("ok", zone)


@pytest.mark.parametrize("place", ["美國", "USA", "the united states", "Australia", "加拿大"])
def test_a_country_with_several_zones_is_ambiguous(place):
    status, zones = current_time.resolve(place)
    assert status == "ambiguous" and isinstance(zones, tuple) and len(zones) >= 2


@pytest.mark.parametrize("place", ["火星", "Atlantis", "the moon", "xyzzy"])
def test_an_unknown_place_is_not_found(place):
    assert current_time.resolve(place) == ("not_found", None)


def test_the_home_answer_has_the_spoken_time_the_weekday_and_the_lunar_date():
    answer = current_time.answer(None, "zh-TW", now=NOW)
    assert answer["status"] == "home" and answer["home"] and answer["timezone"] == "Asia/Taipei"
    assert answer["time"] == "14:25" and answer["spoken"] == "下午2點25分" and answer["weekday"] == "星期三"
    assert answer["date"] == "2026-10-14" and answer["lunar_date"] == "農曆九月初五"
    english = current_time.answer("here", "en", now=NOW)
    assert english["spoken"] == "2:25 in the afternoon" and english["weekday"] == "Wednesday"


def test_a_place_away_from_home_gets_its_time_and_the_home_time():
    answer = current_time.answer("東京現在幾點", "zh-TW", now=NOW)
    assert answer["status"] == "ok" and not answer["home"] and answer["place"] == "東京"
    assert answer["time"] == "15:25" and answer["spoken"] == "下午3點25分" and answer["hours_from_home"] == 1.0
    assert answer["home_time"]["spoken"] == "下午2點25分" and "lunar_date" not in answer
    york = current_time.answer("New York", "en", now=NOW)
    assert york["time"] == "02:25" and york["spoken"] == "2:25 late at night" and york["hours_from_home"] == -12.0
    assert york["weekday"] == "Wednesday" and york["place"] == "New York"


def test_the_answer_is_read_from_the_clock_at_each_call(monkeypatch):
    first = current_time.answer(None, "zh-TW")
    later = current_time.answer(None, "zh-TW", now=datetime.now(ZoneInfo("Asia/Taipei")).replace(year=2030))
    assert first["date"] != later["date"] and later["date"].startswith("2030")


def test_ambiguous_and_not_found_answers_say_so():
    answer = current_time.answer("美國", "zh-TW", now=NOW)
    assert answer["status"] == "ambiguous" and [c["place"] for c in answer["candidates"]] == ["紐約", "芝加哥", "丹佛", "洛杉磯"]
    assert answer["candidates"][0]["spoken"] == "凌晨2點25分"
    assert current_time.answer("火星", "en", now=NOW) == {"status": "not_found", "place": "火星",
                                                           "note": "unknown place; ask for the city or country"}
