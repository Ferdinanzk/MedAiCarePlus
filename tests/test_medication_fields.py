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
    assert result == {"id": 11, "name": "Metformin", "dose_form": "solid_oral", "units_per_dose": 1.0}
    query, args = next((c[1], c[2]) for c in conn.calls if "INSERT INTO medication" in c[1])
    assert "dose_form" in query and "units_per_dose" in query
    assert args[-2:] == ("solid_oral", Decimal("1"))


def test_create_stores_given_fields(monkeypatch):
    conn = _Conn(fetchval_result=12)
    monkeypatch.setattr(api_medications, "get_pool", lambda: _Pool(conn))
    result = asyncio.run(api_medications.create_medication(
        _payload(dose_form="liquid", units_per_dose="2.5"), {"u_id": 7}))
    assert result["dose_form"] == "liquid"
    assert result["units_per_dose"] == 2.5
    args = next(c[2] for c in conn.calls if "INSERT INTO medication" in c[1])
    assert args[-2:] == ("liquid", Decimal("2.5"))


@pytest.mark.parametrize("fields, expected", [
    ({}, (None, None)),
    ({"dose_form": "inhaler", "units_per_dose": 2}, ("inhaler", Decimal("2"))),
])
def test_update_keeps_existing_fields_unless_given(monkeypatch, fields, expected):
    conn = _Conn(fetchval_result=5, previous={"schedule_time": None, "use_before": None})
    monkeypatch.setattr(api_medications, "get_pool", lambda: _Pool(conn))
    assert asyncio.run(api_medications.update_medication(5, _payload(**fields), {"u_id": 7})) == {"id": 5}
    query, args = next((c[1], c[2]) for c in conn.calls if "UPDATE medication" in c[1])
    assert "COALESCE($14" in query and "COALESCE($15" in query
    assert args[13:15] == expected


def test_list_returns_dose_fields(monkeypatch):
    conn = _Conn(rows=[{"id": 1, "name": "Metformin", "dose_form": "solid_oral", "units_per_dose": Decimal("1.00")}])
    monkeypatch.setattr(api_medications, "get_pool", lambda: _Pool(conn))
    result = asyncio.run(api_medications.list_medications({"u_id": 7}))
    assert result == [{"id": 1, "name": "Metformin", "dose_form": "solid_oral", "units_per_dose": 1.0}]
    assert "dose_form" in conn.calls[0][1] and "units_per_dose" in conn.calls[0][1]


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
           "scheduled_time": stamp, "schedule_time": None, "use_before": None}
    conn = _Conn(rows=[row])
    monkeypatch.setattr(api_medications, "get_pool", lambda: _Pool(conn))
    monkeypatch.setattr(api_history, "get_pool", lambda: _Pool(conn))
    today = asyncio.run(api_medications.today_medications({"u_id": 7}, "2026-09-30"))
    assert today[0]["status"] == "pending_confirmation"
    history = asyncio.run(api_history.get_intake_history({"u_id": 7}))
    assert history[0]["status"] == "pending_confirmation"


def test_weekly_adherence_counts_pending_confirmation_as_not_taken(monkeypatch):
    class Conn(_Conn):
        async def fetch(self, query, *args):
            if 'FROM "user"' in query:
                return [{"id": 7, "name": "Pearl"}]
            if "FROM intake" in query:
                return [{"status": "taken"}, {"status": "pending_confirmation"},
                        {"status": "pending_confirmation"}, {"status": "taken"}]
            if "FROM emotion" in query:
                return []
            return [{"line_id": "U-amy"}]

    sent = []
    service = types.SimpleNamespace(send_weekly_summary=lambda *args: sent.append(args))
    monkeypatch.setattr(weekly_summary_job, "get_pool", lambda: _Pool(Conn()))
    monkeypatch.setattr(weekly_summary_job.LineService, "get_instance", classmethod(lambda cls: service))
    asyncio.run(weekly_summary_job.send_weekly_summaries())
    assert sent == [("U-amy", "Pearl", 50.0, "穩定")]
