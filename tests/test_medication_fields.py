import asyncio
import datetime
import sys
import types
from decimal import Decimal

import pytest
from pydantic import ValidationError

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app.jobs import weekly_summary_job
from app.routers import api_history, api_medications
from app.services import intake_repository


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


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


class _Conn:
    def __init__(self, rows=None, fetchval_result=None, previous=None):
        self.rows = rows or []
        self.fetchval_result = fetchval_result
        self.previous = previous
        self.calls = []

    def transaction(self):
        return _Transaction()

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        return self.rows

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        if query.lstrip().startswith("UPDATE medication") and self.fetchval_result is not None:
            # RETURNING the stored limits: set when given ($16/$18), else as stored (none here).
            return {"med_id": self.fetchval_result, "min_interval_minutes": args[16] if args[15] else None,
                    "max_daily_doses": args[18] if args[17] else None}
        return self.previous

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", query, args))
        return self.fetchval_result

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))

    async def executemany(self, query, args):
        self.calls.append(("executemany", query, args))


def _payload(**overrides):
    data = {"name": "Metformin", "total_pills": 10}
    data.update(overrides)
    return api_medications.MedicationPayload(**data)


# ── validation ──

def test_payload_defaults_leave_fields_unset():
    payload = _payload()
    assert payload.dose_form is None
    assert payload.units_per_dose is None


@pytest.mark.parametrize("form", ["solid_oral", "liquid", "inhaler", "injection", "topical", "other"])
def test_payload_accepts_known_dose_forms(form):
    assert _payload(dose_form=form).dose_form == form


@pytest.mark.parametrize("fields", [
    {"dose_form": "capsule"}, {"dose_form": ""}, {"units_per_dose": 0}, {"units_per_dose": -1},
    {"units_per_dose": 100}, {"units_per_dose": "1.234"}, {"units_per_dose": "abc"},
])
def test_payload_rejects_invalid_fields(fields):
    with pytest.raises(ValidationError):
        _payload(**fields)


def test_payload_accepts_fractional_units():
    assert _payload(units_per_dose="0.5").units_per_dose == Decimal("0.5")
    assert _payload(units_per_dose=2).units_per_dose == Decimal("2")


# ── create / update / list ──

def test_create_stores_defaults_when_fields_missing(monkeypatch):
    conn = _Conn(fetchval_result=11)
    monkeypatch.setattr(api_medications, "get_pool", lambda: _Pool(conn))
    result = asyncio.run(api_medications.create_medication(_payload(), {"u_id": 7}))
    assert result == {"id": 11, "name": "Metformin", "dose_form": "solid_oral", "units_per_dose": 1.0,
                      "min_interval_minutes": None, "max_daily_doses": None}
    query, args = next((c[1], c[2]) for c in conn.calls if "INSERT INTO medication" in c[1])
    assert "dose_form" in query and "units_per_dose" in query
    assert "min_interval_minutes, max_daily_doses" in query
    assert args[-4:] == ("solid_oral", Decimal("1"), None, None)   # overdose limits: the schedule's defaults


def test_create_stores_given_fields(monkeypatch):
    conn = _Conn(fetchval_result=12)
    monkeypatch.setattr(api_medications, "get_pool", lambda: _Pool(conn))
    result = asyncio.run(api_medications.create_medication(
        _payload(dose_form="liquid", units_per_dose="2.5"), {"u_id": 7}))
    assert result["dose_form"] == "liquid"
    assert result["units_per_dose"] == 2.5
    args = next(c[2] for c in conn.calls if "INSERT INTO medication" in c[1])
    assert args[-4:-2] == ("liquid", Decimal("2.5"))


@pytest.mark.parametrize("fields, expected", [
    ({}, (None, None)),
    ({"dose_form": "inhaler", "units_per_dose": 2}, ("inhaler", Decimal("2"))),
])
def test_update_keeps_existing_fields_unless_given(monkeypatch, fields, expected):
    conn = _Conn(fetchval_result=5, previous={"schedule_time": None, "use_before": None})
    monkeypatch.setattr(api_medications, "get_pool", lambda: _Pool(conn))
    assert asyncio.run(api_medications.update_medication(5, _payload(**fields), {"u_id": 7})) == {
        "id": 5, "min_interval_minutes": None, "max_daily_doses": None}
    query, args = next((c[1], c[2]) for c in conn.calls if "UPDATE medication" in c[1])
    assert "COALESCE($14" in query and "COALESCE($15" in query
    assert args[13:15] == expected


@pytest.mark.parametrize("fields, expected", [
    ({}, (False, None, False, None)),                                          # left out: kept
    ({"min_interval_minutes": 90, "max_daily_doses": 3}, (True, 90, True, 3)),
    ({"min_interval_minutes": None}, (True, None, False, None)),               # null: back to the default
])
def test_update_sets_overdose_limits_only_when_given(monkeypatch, fields, expected):
    conn = _Conn(fetchval_result=5, previous={"schedule_time": None, "use_before": None})
    monkeypatch.setattr(api_medications, "get_pool", lambda: _Pool(conn))
    result = asyncio.run(api_medications.update_medication(5, _payload(**fields), {"u_id": 7}))
    assert result == {"id": 5, "min_interval_minutes": expected[1], "max_daily_doses": expected[3]}   # as stored
    query, args = next((c[1], c[2]) for c in conn.calls if "UPDATE medication" in c[1])
    assert "RETURNING med_id, min_interval_minutes, max_daily_doses" in query
    assert "min_interval_minutes=CASE WHEN $16 THEN $17::int ELSE min_interval_minutes END" in query
    assert "max_daily_doses=CASE WHEN $18 THEN $19::int ELSE max_daily_doses END" in query
    assert args[15:19] == expected


@pytest.mark.parametrize("fields", [
    {"min_interval_minutes": 29}, {"min_interval_minutes": 2881}, {"max_daily_doses": 0}, {"max_daily_doses": 25},
])
def test_payload_rejects_overdose_limits_outside_the_database_checks(fields):
    with pytest.raises(ValidationError):
        _payload(**fields)


def test_list_gives_the_overdose_defaults_of_each_schedule(monkeypatch):
    rows = [{"id": 1, "name": "allegra", "schedule_time": {"morning": True, "noon": True, "night": True,
                                                            "bedtime": True}},
            {"id": 2, "name": "daily", "schedule_time": {"morning": True}, "min_interval_minutes": 600,
             "max_daily_doses": 2}]
    conn = _Conn(rows=rows)
    monkeypatch.setattr(api_medications, "get_pool", lambda: _Pool(conn))
    allegra, daily = asyncio.run(api_medications.list_medications({"u_id": 7}))
    assert (allegra["default_min_interval_minutes"], allegra["default_max_daily_doses"]) == (60, 4)
    assert (daily["default_min_interval_minutes"], daily["default_max_daily_doses"]) == (720, 1)
    assert (daily["min_interval_minutes"], daily["max_daily_doses"]) == (600, 2)   # its own, as stored


def test_list_returns_dose_fields(monkeypatch):
    conn = _Conn(rows=[{"id": 1, "name": "Metformin", "dose_form": "solid_oral", "units_per_dose": Decimal("1.00")}])
    monkeypatch.setattr(api_medications, "get_pool", lambda: _Pool(conn))
    result = asyncio.run(api_medications.list_medications({"u_id": 7}))
    # Unscheduled: overdose protection keeps 4 h between doses and sets no daily maximum unless the medicine does.
    assert result == [{"id": 1, "name": "Metformin", "dose_form": "solid_oral", "units_per_dose": 1.0,
                       "pills_remaining": None, "daily_units": None, "days_left": None, "run_out_date": None,
                       "default_min_interval_minutes": 240, "default_max_daily_doses": None}]
    assert "dose_form" in conn.calls[0][1] and "units_per_dose" in conn.calls[0][1]
    assert "min_interval_minutes, max_daily_doses" in conn.calls[0][1]


def test_list_reports_days_of_supply_for_active_scheduled_medication(monkeypatch):
    conn = _Conn(rows=[{"id": 1, "name": "Metformin", "is_active": True, "pills_remaining": Decimal("10.00"),
                        "units_per_dose": Decimal("0.50"), "schedule_time": {"morning": True, "night": True}}])
    monkeypatch.setattr(api_medications, "get_pool", lambda: _Pool(conn))
    (row,) = asyncio.run(api_medications.list_medications({"u_id": 7}))
    # two half-tablet doses a day = 1 tablet a day
    assert row["pills_remaining"] == 10.0 and row["daily_units"] == 1.0 and row["days_left"] == 10
    assert row["run_out_date"] is not None


# ── pending_confirmation propagation ──

def test_manual_patch_of_pending_confirmation_returns_409(monkeypatch):
    conn = _Conn(fetchval_result="pending_confirmation")
    monkeypatch.setattr(api_medications, "get_pool", lambda: _Pool(conn))

    async def refuse(*args, **kwargs):
        raise ValueError("awaiting_caregiver_confirmation")

    monkeypatch.setattr(intake_repository, "transition_intake", refuse)
    response = asyncio.run(api_medications.update_intake_status(9, {"status": "taken"}, {"u_id": 7}))
    assert response.status_code == 409
    assert response.body == b'{"detail":"awaiting_caregiver_confirmation"}'


def test_transition_intake_refuses_pending_confirmation_under_row_lock(monkeypatch):
    class LockedConn:
        def __init__(self):
            self.writes = []
            self.locked_query = None

        def transaction(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def fetchrow(self, query, *args):
            self.locked_query = query
            return {"intk_id": 9, "med_id": 4, "intake_stats": "pending_confirmation"}

        async def fetchval(self, query, *args):
            self.writes.append(query)

        async def execute(self, query, *args):
            self.writes.append(query)

    conn = LockedConn()

    class Pool:
        def acquire(self):
            return conn

    monkeypatch.setattr(intake_repository, "get_pool", lambda: Pool())
    for status in ("taken", "skipped", "missed", "pending"):
        with pytest.raises(ValueError, match="awaiting_caregiver_confirmation"):
            asyncio.run(intake_repository.transition_intake(7, 9, status))
    assert "FOR UPDATE" in conn.locked_query
    assert conn.writes == []


def test_manual_patch_of_other_status_still_transitions(monkeypatch):
    conn = _Conn(fetchval_result="pending")
    monkeypatch.setattr(api_medications, "get_pool", lambda: _Pool(conn))
    calls = []

    async def transition(u_id, intk_id, status):
        calls.append((u_id, intk_id, status))
        return {"intk_id": intk_id, "status": status, "changed": True}

    monkeypatch.setattr(intake_repository, "transition_intake", transition)
    result = asyncio.run(api_medications.update_intake_status(9, {"status": "taken"}, {"u_id": 7}))
    assert result["status"] == "taken"
    assert calls == [(7, 9, "taken")]


def test_today_and_history_return_pending_confirmation_as_is(monkeypatch):
    stamp = datetime.datetime(2026, 9, 30, 0, 0, tzinfo=datetime.timezone.utc)
    row = {"intake_id": 9, "id": 9, "med_id": 4, "name": "Metformin", "status": "pending_confirmation",
           "scheduled_time": stamp, "previous_time": None, "schedule_time": None, "use_before": None, "total": 1}
    conn = _Conn(rows=[row])
    monkeypatch.setattr(api_medications, "get_pool", lambda: _Pool(conn))
    monkeypatch.setattr(api_history, "get_pool", lambda: _Pool(conn))
    today = asyncio.run(api_medications.today_medications({"u_id": 7}, "2026-09-30"))
    assert today[0]["status"] == "pending_confirmation"
    history = asyncio.run(api_history.get_intake_history({"u_id": 7}, None, None, 50, 0))
    assert history["items"][0]["status"] == "pending_confirmation" and history["total"] == 1


def test_weekly_adherence_counts_pending_confirmation_as_not_taken(monkeypatch):
    class Conn(_Conn):
        async def fetch(self, query, *args):
            if 'FROM "user"' in query:
                return [{"id": 7, "name": "Pearl"}]
            if "FROM intake" in query:
                return [{"day": datetime.date(2026, 9, 28), "taken": 2, "missed": 0, "skipped": 0,
                         "awaiting": 2, "overdue": 0}]
            if "FROM emotion" in query:
                return []
            return [{"line_id": "U-amy"}]

    sent = []
    service = types.SimpleNamespace(send_weekly_summary=lambda *args: sent.append(args))
    monkeypatch.setattr(weekly_summary_job, "get_pool", lambda: _Pool(Conn()))
    monkeypatch.setattr(weekly_summary_job.LineService, "get_instance", classmethod(lambda cls: service))
    asyncio.run(weekly_summary_job.send_weekly_summaries())
    assert sent == [("U-amy", "Pearl", 50.0, "穩定")]
