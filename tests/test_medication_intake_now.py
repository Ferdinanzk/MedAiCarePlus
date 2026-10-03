import asyncio
import datetime
import os
import sys
import types

import pytest

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

sys.path.insert(0, os.path.dirname(__file__))
import dose_facts  # noqa: E402

from app.routers import api_medications  # noqa: E402
from app.services import dose_safety, schedule  # noqa: E402


class _Transaction:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, *args):
        self.conn.rolled_back = exc_type is not None   # a refusal rolls back an ad-hoc row made just before
        return False


class _Connection:
    def __init__(self, due=None, recent_taken=None, latest=None, facts=None):
        self.due = due
        self.recent_taken = recent_taken
        self.latest = latest            # today's latest dose that has come (any status)
        self.facts = facts or {}        # intk_id -> FACTS_SQL fields that differ from a plain due dose
        self.inserted = None
        self.due_query = None
        self.recent_query = None
        self.latest_query = None
        self.checked = []
        self.rolled_back = None

    def transaction(self):
        return _Transaction(self)

    async def fetch(self, query, *args):
        assert dose_facts.is_facts(query), query
        u_id, ids, at, zone = args
        self.checked.append(list(ids))
        # A fact given as a function is worked out from the moment judged (intake_now's own clock).
        return [{"intk_id": i, "u_id": u_id, "med_id": 4, "intake_stats": "pending", "intake_time_stamp": at,
                 "previous_time": None, "next_time": None, "med_name": "Rescue pill", "schedule_time": None,
                 "min_interval_minutes": None, "max_daily_doses": None, "protection": True, "language": None,
                 "last_taken_at": None, "taken_that_day": 0,
                 **{key: value(at) if callable(value) else value for key, value in self.facts.get(i, {}).items()}}
                for i in ids]

    async def fetchrow(self, query, *args):
        if "SELECT med_id, med_name" in query:
            return {
                "med_id": 4, "med_name": "Rescue pill", "dosage": "1 tab",
                "pills_remaining": 3, "warning": None, "use_before": None,
                "is_active": True,
            }
        if "SELECT i.intk_id AS id" in query:
            self.due_query = (query, args)
            return self.due
        if "SELECT intk_id, intake_stats FROM intake" in query:
            self.latest_query = (query, args)
            return self.latest
        if "INSERT INTO intake" in query:
            self.inserted = args
            return {
                "id": 9, "med_id": 4, "scheduled_time": args[2], "status": "pending",
            }
        raise AssertionError(f"unexpected query: {query}")

    async def fetchval(self, query, *args):
        if "recent" in query:
            return self.recent_taken
        if "actual_intake_time" in query:
            self.recent_query = (query, args)
            return self.recent_taken
        raise AssertionError(f"unexpected query: {query}")


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *args):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


def test_intake_now_creates_pending_row_for_unscheduled_medication(monkeypatch):
    conn = _Connection()
    monkeypatch.setattr(api_medications, "get_pool", lambda: _Pool(conn))

    result = asyncio.run(api_medications.intake_now(4, {"u_id": 7}))

    assert result["id"] == 9
    assert result["status"] == "pending"
    assert result["name"] == "Rescue pill"
    assert conn.inserted[0:2] == (7, 4)
    assert isinstance(conn.inserted[2], datetime.datetime)
    assert result["due_from"] < result["scheduled_time"]   # an ad-hoc dose made now is due at once


def test_intake_now_reuses_due_row_without_inserting(monkeypatch):
    scheduled = datetime.datetime.now(datetime.timezone.utc)
    due = {
        "id": 12, "med_id": 4, "name": "Rescue pill", "dosage": "1 tab",
        "scheduled_time": scheduled, "previous_time": scheduled - datetime.timedelta(hours=2),
        "status": "missed", "pills_remaining": 3, "warning": None,
    }
    conn = _Connection(due=due, facts={12: {"intake_time_stamp": scheduled,
                                            "previous_time": scheduled - datetime.timedelta(hours=2)}})
    monkeypatch.setattr(api_medications, "get_pool", lambda: _Pool(conn))

    result = asyncio.run(api_medications.intake_now(4, {"u_id": 7}))

    # due_from counts the previous dose (halfway: an hour before); previous_time itself is not returned.
    expected = {key: value for key, value in due.items() if key != "previous_time"}
    assert result == {**expected, "due_from": scheduled - min(schedule.DOSE_EARLY, datetime.timedelta(hours=1))}
    assert conn.inserted is None
    assert conn.due_query is not None
    due_query, due_args = conn.due_query
    assert "intake_time_stamp >= $3" in due_query
    # Today's rows that are due (the window and the previous dose: never a later slot) and, under overdose
    # protection, not expired, nearest to now first, the earlier one on a tie (as the app's dueDose and the legacy
    # page pick). The row picked is checked against every rule.
    assert schedule.due_sql("intake", "$4::timestamptz") in due_query
    assert dose_safety.open_sql("intake", "$4::timestamptz") in due_query
    assert conn.checked == [[12]]
    assert f"{schedule.previous_sql('i')} AS previous_time" in due_query
    local_start, now = due_args[2:4]
    assert len(due_args) == 4
    assert local_start.date() == now.date() and local_start <= now
    assert ("ORDER BY ABS(EXTRACT(EPOCH FROM (intake_time_stamp - $4::timestamptz))), intake_time_stamp LIMIT 1"
            in " ".join(due_query.split()))


def test_intake_now_uses_typed_python_cutoff_for_recent_duplicate(monkeypatch):
    conn = _Connection(recent_taken=1)
    monkeypatch.setattr(api_medications, "get_pool", lambda: _Pool(conn))

    response = asyncio.run(api_medications.intake_now(4, {"u_id": 7}))

    assert response.status_code == 409
    assert conn.recent_query is not None
    recent_query, recent_args = conn.recent_query
    assert "- INTERVAL" not in recent_query
    assert isinstance(recent_args[2], datetime.datetime)


# ── Overdose protection on Take Now ─────────────────────────────────────────

TWICE_DAILY = {"morning": True, "night": True}   # 08:00 and 20:00: 6 h apart at least, 2 a day


def _hours(n):
    return lambda at: at + datetime.timedelta(hours=n)


def _scheduled(n):
    """A scheduled dose time n hours from the moment judged: on a whole minute, as the schedule makes them (an ad-hoc
    dose keeps the moment it was made, dose_safety.is_ad_hoc)."""
    return lambda at: (at + datetime.timedelta(hours=n)).replace(second=0, microsecond=0)


def test_a_missed_dose_that_expired_is_not_made_up_with_an_ad_hoc_dose(monkeypatch):
    """08:00 missed, 20:00 not due yet: at 15:00 the 08:00 dose expired at 14:00 (halfway). Take Now must not give
    the patient a pill now and another at 20:00."""
    conn = _Connection(latest={"intk_id": 5, "intake_stats": "missed"},
                       facts={5: {"intake_time_stamp": _scheduled(-7), "next_time": _scheduled(5),
                                  "intake_stats": "missed", "schedule_time": TWICE_DAILY}})
    monkeypatch.setattr(api_medications, "get_pool", lambda: _Pool(conn))
    with pytest.raises(dose_safety.DoseExpired) as refused:
        asyncio.run(api_medications.intake_now(4, {"u_id": 7}))
    body = refused.value.body()
    assert body["detail"] == "dose_expired" and body["intk_id"] == 5
    assert body["reply"].endswith("的藥已經錯過了，請不要補吃，等下一次就好。")
    assert conn.inserted is None and conn.checked == [[5]]
    query, args = conn.latest_query
    assert "intake_time_stamp >= $3 AND intake_time_stamp <= $4" in " ".join(query.split())
    # Only a scheduled dose stands for the schedule here: an earlier ad-hoc dose left pending is no missed dose.
    assert f"AND NOT {dose_safety.ad_hoc_sql('intake')}" in " ".join(query.split())
    assert args[0:2] == (4, 7)


def test_an_ad_hoc_dose_left_pending_never_expires():
    """Take Now at 13:00:12 with nothing open, then abandoned: it is no missed dose to refuse ("don't make it up")
    hours later, and it moves no scheduled dose's halfway point (next_sql leaves ad-hoc rows out)."""
    taipei = datetime.timezone(datetime.timedelta(hours=8))
    ad_hoc = datetime.datetime(2026, 10, 3, 13, 0, 12, 345678, tzinfo=taipei)
    scheduled = datetime.datetime(2026, 10, 3, 12, 0, tzinfo=taipei)
    assert dose_safety.is_ad_hoc(ad_hoc) and not dose_safety.is_ad_hoc(scheduled)
    assert dose_safety.expires_at(ad_hoc, ad_hoc + datetime.timedelta(hours=7)) is None
    assert dose_safety.expires_at(scheduled, scheduled + datetime.timedelta(hours=8)) == scheduled.replace(hour=16)
    assert f"AND NOT {dose_safety.ad_hoc_sql('next_dose')}" in dose_safety.next_sql("i")
    assert dose_safety.expired_sql("i").startswith(f"(NOT {dose_safety.ad_hoc_sql('i')} AND ")


def test_an_ad_hoc_row_is_never_made_on_a_whole_minute(monkeypatch):
    """That is how it is told from a scheduled row: a Take Now at exactly 21:00:00.000000 is stored a microsecond
    later."""
    whole = datetime.datetime(2026, 10, 3, 21, 0, tzinfo=api_medications._MEDCARE_TZ)

    class _Clock(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return whole

    monkeypatch.setattr(api_medications, "datetime", types.SimpleNamespace(
        datetime=_Clock, timedelta=datetime.timedelta, time=datetime.time, date=datetime.date))
    conn = _Connection()
    monkeypatch.setattr(api_medications, "get_pool", lambda: _Pool(conn))
    asyncio.run(api_medications.intake_now(4, {"u_id": 7}))
    assert conn.inserted[2] == whole + datetime.timedelta(microseconds=1)
    assert dose_safety.is_ad_hoc(conn.inserted[2])


def test_with_protection_off_an_expired_dose_gives_an_ad_hoc_dose_as_before(monkeypatch):
    conn = _Connection(latest={"intk_id": 5, "intake_stats": "missed"},
                       facts={5: {"intake_time_stamp": _scheduled(-7), "next_time": _scheduled(5),
                                  "protection": False},
                              9: {"protection": False, "last_taken_at": _hours(-0.5)}})
    monkeypatch.setattr(api_medications, "get_pool", lambda: _Pool(conn))
    result = asyncio.run(api_medications.intake_now(4, {"u_id": 7}))
    assert result["id"] == 9 and conn.inserted is not None and conn.rolled_back is False


def test_a_dose_already_taken_today_needs_its_gap_before_an_ad_hoc_one(monkeypatch):
    """20:00 taken at 20:00; Take Now at 21:00 (no dose open): an ad-hoc dose would be the second within 6 h."""
    conn = _Connection(latest={"intk_id": 6, "intake_stats": "taken"},
                       facts={9: {"last_taken_at": _hours(-1), "schedule_time": TWICE_DAILY}})
    monkeypatch.setattr(api_medications, "get_pool", lambda: _Pool(conn))
    with pytest.raises(dose_safety.DoseTooSoon) as refused:
        asyncio.run(api_medications.intake_now(4, {"u_id": 7}))
    body = refused.value.body()
    assert body["detail"] == "dose_too_soon" and body["min_interval_minutes"] == 360
    assert body["reply"].startswith("這個藥您") and body["reply"].endswith("已經吃過了，請先不要再吃。")
    assert conn.checked == [[9]] and conn.rolled_back is True   # the ad-hoc row made for it is rolled back


def test_an_ad_hoc_dose_counts_toward_the_daily_maximum(monkeypatch):
    conn = _Connection(facts={9: {"taken_that_day": 2, "schedule_time": TWICE_DAILY,
                                  "last_taken_at": _hours(-7)}})
    monkeypatch.setattr(api_medications, "get_pool", lambda: _Pool(conn))
    with pytest.raises(dose_safety.DailyMaxReached) as refused:
        asyncio.run(api_medications.intake_now(4, {"u_id": 7}))
    assert refused.value.body()["reply"] == "今天的 Rescue pill 已經吃滿 2 次了，請不要再吃。"
    assert conn.rolled_back is True


def test_an_unscheduled_medicine_keeps_four_hours_but_has_no_daily_maximum(monkeypatch):
    conn = _Connection(facts={9: {"taken_that_day": 6, "last_taken_at": _hours(-4)}})
    monkeypatch.setattr(api_medications, "get_pool", lambda: _Pool(conn))
    assert asyncio.run(api_medications.intake_now(4, {"u_id": 7}))["id"] == 9
    conn = _Connection(facts={9: {"last_taken_at": _hours(-3.9)}})
    monkeypatch.setattr(api_medications, "get_pool", lambda: _Pool(conn))
    with pytest.raises(dose_safety.DoseTooSoon) as refused:
        asyncio.run(api_medications.intake_now(4, {"u_id": 7}))
    assert refused.value.gap == datetime.timedelta(hours=4)
