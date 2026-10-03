"""Custom times and weekdays, days of supply, adherence ranges and streaks, archive/reactivate/supply/delete."""

import asyncio
import datetime
import sys
import types
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app.jobs import refill_reminder_job
from app.routers import api_medications
from app.services import adherence, schedule

TZ = ZoneInfo("Asia/Taipei")


# ── schedules ──

def test_dose_times_merge_presets_and_custom_times():
    value = {"morning": True, "night": False, "custom_times": ["07:30", "08:00", "15:45"], "before_meals": True}
    assert schedule.dose_times(value) == ["07:30", "08:00", "15:45"]
    assert schedule.weekdays(value) == (1, 2, 3, 4, 5, 6, 7)
    assert schedule.dose_times('{"noon": true}') == ["12:00"]


@pytest.mark.parametrize("value", [
    {"custom_times": ["25:00"]}, {"custom_times": ["7:30"]}, {"custom_times": "08:00"},
    {"weekdays": []}, {"weekdays": [0]}, {"weekdays": [8]}, {"weekdays": [True]},
    {"custom_times": [f"0{h}:00" for h in range(9)]},
])
def test_invalid_schedules_are_rejected(value):
    with pytest.raises(ValidationError):
        api_medications.MedicationPayload(name="X", schedule_time=value)


def test_signature_ignores_display_only_keys():
    assert schedule.signature({"morning": True, "before_meals": True}) == schedule.signature({"morning": True})
    assert schedule.signature({"morning": True}) != schedule.signature({"morning": True, "weekdays": [1]})


def test_occurrences_follow_weekdays_custom_times_and_end_date():
    start = datetime.datetime(2026, 10, 2, 9, 0, tzinfo=TZ)   # a Friday
    value = {"custom_times": ["08:00", "18:30"], "weekdays": [1, 5]}   # Mondays and Fridays
    moments = schedule.occurrences(value, start, horizon_days=8)
    assert [m.strftime("%a %d %H:%M") for m in moments] == ["Fri 02 18:30", "Mon 05 08:00", "Mon 05 18:30",
                                                            "Fri 09 08:00", "Fri 09 18:30"]
    assert schedule.occurrences(value, start, until=datetime.date(2026, 10, 4)) == moments[:1]
    assert schedule.occurrences({}, start) == []


def test_a_course_that_already_ended_gets_no_doses_today():
    """A scanned receipt dispensed 28 Sep for 5 days ends 2 Oct; saved on 4 Oct it must not remind at 12:00 and 20:00."""
    now = datetime.datetime(2026, 10, 4, 10, 0, tzinfo=TZ)
    three = {"morning": True, "noon": True, "night": True}
    assert schedule.occurrences(three, now, until=datetime.date(2026, 10, 2)) == []
    today = schedule.occurrences(three, now, until=datetime.date(2026, 10, 4))
    assert [m.strftime("%d %H:%M") for m in today] == ["04 12:00", "04 20:00"]   # the last day itself still counts


@pytest.mark.parametrize("text, expected", [
    ("114年08月22日", datetime.date(2025, 8, 22)), ("2025年8月22日", datetime.date(2025, 8, 22)),
    ("2025-08-22", datetime.date(2025, 8, 22)), ("2025/8/2", datetime.date(2025, 8, 2)),
    ("N/A", None), ("", None), (None, None), ("114年13月40日", None),
])
def test_parse_date_matches_the_frontend(text, expected):
    assert schedule.parse_date(text) == expected


def test_supply_uses_dose_size_and_weekday_rate():
    today = datetime.date(2026, 10, 2)
    daily = schedule.supply(Decimal("14"), Decimal("1"), {"morning": True, "night": True}, today)
    assert daily == {"daily_units": 2.0, "days_left": 7, "run_out_date": "2026-10-09"}
    halves = schedule.supply(Decimal("3"), Decimal("0.5"), {"morning": True}, today)
    assert halves["days_left"] == 6
    weekly = schedule.supply(Decimal("2"), Decimal("1"), {"morning": True, "weekdays": [1]}, today)
    assert weekly["days_left"] == 14
    assert schedule.supply(Decimal("5"), Decimal("1"), {}, today)["days_left"] is None


@pytest.mark.parametrize("remaining, schedule_time, expected", [
    (Decimal("14"), {"morning": True, "night": True}, True),     # 7 days left
    (Decimal("16"), {"morning": True, "night": True}, False),    # 8 days left
    (Decimal("7"), {}, True),                                    # unscheduled: 7 doses left
    (Decimal("8"), {}, False),
])
def test_refill_is_due_by_days_of_supply(remaining, schedule_time, expected):
    row = {"pills_remaining": remaining, "units_per_dose": Decimal("1"), "schedule_time": schedule_time}
    assert (refill_reminder_job.needs_refill(row, datetime.date(2026, 10, 2)) is not None) is expected


# ── adherence ──

def _day(day, **counts):
    return {"day": datetime.date(2026, 10, day), **{s: counts.get(s, 0) for s in adherence.STATUSES}}


def test_totals_count_awaiting_as_not_taken_and_report_none_without_doses():
    totals = adherence.totals([_day(1, taken=3, missed=1), _day(2, taken=2, awaiting=2)])
    assert totals["due"] == 8 and totals["taken"] == 5 and totals["adherence"] == 62.5
    assert adherence.totals([])["adherence"] is None


def test_streak_skips_days_without_doses_and_stops_at_the_first_incomplete_day():
    days = [_day(1, taken=2, missed=1), _day(2, taken=2), _day(4, taken=1), _day(5, taken=3)]
    assert adherence.streak(days, datetime.date(2026, 10, 5)) == 3
    assert adherence.streak(days + [_day(6, taken=1, overdue=1)], datetime.date(2026, 10, 6)) == 0
    assert adherence.streak(days + [_day(9, taken=1)], datetime.date(2026, 10, 5)) == 3   # future ignored


def test_daily_counts_exclude_future_and_recently_due_pending_doses():
    class Conn:
        async def fetch(self, query, *args):
            self.query, self.args = query, args
            return [{"day": datetime.date(2026, 10, 2), "taken": 1, "missed": 0, "skipped": 0,
                     "awaiting": 0, "overdue": 1}]

    conn = Conn()
    now = datetime.datetime(2026, 10, 2, 12, tzinfo=TZ)
    days = asyncio.run(adherence.daily_counts(conn, 7, now - datetime.timedelta(days=1), now, now))
    assert days == [_day(2, taken=1, overdue=1)]
    assert "i.intake_time_stamp <= $4" in conn.query
    assert conn.args[3] == now and conn.args[5] == now - adherence.GRACE


# ── archive / reactivate / supply / delete ──

class _Conn:
    def __init__(self, results):
        self.results = results   # first matching substring -> return value
        self.calls = []

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _answer(self, kind, query, args):
        self.calls.append((kind, query, args))
        for key, value in self.results.items():
            if key in query:
                return value
        return None

    async def fetchrow(self, query, *args):
        return self._answer("fetchrow", query, args)

    async def fetchval(self, query, *args):
        return self._answer("fetchval", query, args)

    async def fetch(self, query, *args):
        return self._answer("fetch", query, args) or []

    async def execute(self, query, *args):
        self._answer("execute", query, args)

    async def executemany(self, query, args):
        self.calls.append(("executemany", query, args))


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return self.conn


def _run(monkeypatch, conn, coro_fn, *args):
    monkeypatch.setattr(api_medications, "get_pool", lambda: _Pool(conn))
    return asyncio.run(coro_fn(*args))


def test_archive_stops_future_doses_and_keeps_history(monkeypatch):
    conn = _Conn({"UPDATE medication SET is_active=FALSE": 4})
    assert _run(monkeypatch, conn, api_medications.archive_medication, 4, {"u_id": 7}) == {"id": 4, "is_active": False}
    deleted = next(q for kind, q, _ in conn.calls if "DELETE FROM intake" in q)
    assert "intake_time_stamp > NOW()" in deleted and "intake_stats IN ('pending', 'missed')" in deleted
    assert not any("DELETE FROM medication" in q for _, q, _ in conn.calls)


def test_reactivate_rebuilds_the_schedule_from_now(monkeypatch):
    conn = _Conn({"UPDATE medication SET is_active=TRUE": {"med_id": 4, "schedule_time": '{"night": true}',
                                                           "use_before": None}})
    assert _run(monkeypatch, conn, api_medications.reactivate_medication, 4, {"u_id": 7}) == {"id": 4, "is_active": True}
    inserted = next(args for kind, _, args in conn.calls if kind == "executemany")
    assert inserted and all(row[2].strftime("%H:%M") == "20:00" for row in inserted)


def test_supply_adds_stock_and_records_the_refill(monkeypatch):
    conn = _Conn({"UPDATE medication SET pills_remaining=pills_remaining+$3": {
        "pills_remaining": Decimal("30.50"), "units_per_dose": Decimal("1"), "schedule_time": {"morning": True},
        "is_active": True}})
    payload = api_medications.SupplyPayload(quantity="28", note=" pharmacy ")
    result = _run(monkeypatch, conn, api_medications.add_supply, 4, payload, {"u_id": 7})
    assert result["pills_remaining"] == 30.5 and result["days_left"] == 30
    insert = next(args for _, q, args in conn.calls if "INSERT INTO medication_supply" in q)
    assert insert == (7, 4, Decimal("28"), "pharmacy")


@pytest.mark.parametrize("quantity", ["0", "-1", "1.234", "10000"])
def test_supply_rejects_bad_quantities(quantity):
    with pytest.raises(ValidationError):
        api_medications.SupplyPayload(quantity=quantity)


def test_delete_refuses_a_medication_with_history(monkeypatch):
    conn = _Conn({"SELECT EXISTS": True})
    response = _run(monkeypatch, conn, api_medications.delete_medication, 4, {"u_id": 7})
    assert response.status_code == 409 and response.body == b'{"detail":"has_history"}'
    assert not any("DELETE FROM medication" in q for _, q, _ in conn.calls)
    conn = _Conn({"SELECT EXISTS": False, "DELETE FROM medication": 4})
    assert _run(monkeypatch, conn, api_medications.delete_medication, 4, {"u_id": 7}) == {"deleted": 4}


def test_deactivating_through_edit_clears_future_doses_without_regenerating(monkeypatch):
    conn = _Conn({"SELECT schedule_time, use_before, is_active": {"schedule_time": {"morning": True},
                                                                  "use_before": None, "is_active": True},
                  "UPDATE medication": {"med_id": 4, "min_interval_minutes": None, "max_daily_doses": None}})
    payload = api_medications.MedicationPayload(name="X", is_active=False, schedule_time={"morning": True})
    assert _run(monkeypatch, conn, api_medications.update_medication, 4, payload, {"u_id": 7}) == {
        "id": 4, "min_interval_minutes": None, "max_daily_doses": None}
    assert any("DELETE FROM intake" in q for _, q, _ in conn.calls)
    assert not any(kind == "executemany" for kind, _, _ in conn.calls)


def test_every_scheduled_job_can_be_imported():
    """The scheduler imports its jobs inside start_scheduler(), so a renamed job only fails at app startup."""
    import importlib
    import re
    from pathlib import Path

    source = Path(__file__).resolve().parent.parent.joinpath("app", "jobs", "scheduler.py").read_text(encoding="utf-8")
    imports = re.findall(r"from (app\.jobs\.\w+) import (\w+)", source)
    assert len(imports) >= 7
    for module, name in imports:
        assert callable(getattr(importlib.import_module(module), name)), f"{module}.{name}"
