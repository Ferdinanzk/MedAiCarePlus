import asyncio
import datetime
import sys
import types

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app.routers import api_medications


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _Connection:
    def __init__(self, due=None, recent_taken=None):
        self.due = due
        self.recent_taken = recent_taken
        self.inserted = None
        self.due_query = None
        self.recent_query = None

    def transaction(self):
        return _Transaction()

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


def test_intake_now_reuses_due_row_without_inserting(monkeypatch):
    due = {
        "id": 12, "med_id": 4, "name": "Rescue pill", "dosage": "1 tab",
        "scheduled_time": datetime.datetime.now(datetime.timezone.utc),
        "status": "missed", "pills_remaining": 3, "warning": None,
    }
    conn = _Connection(due=due)
    monkeypatch.setattr(api_medications, "get_pool", lambda: _Pool(conn))

    result = asyncio.run(api_medications.intake_now(4, {"u_id": 7}))

    assert result == due
    assert conn.inserted is None
    assert conn.due_query is not None
    due_query, due_args = conn.due_query
    assert "intake_time_stamp >= $3" in due_query
    assert "intake_time_stamp <= $4" in due_query
    assert due_args[2].date() == due_args[3].date()


def test_intake_now_uses_typed_python_cutoff_for_recent_duplicate(monkeypatch):
    conn = _Connection(recent_taken=1)
    monkeypatch.setattr(api_medications, "get_pool", lambda: _Pool(conn))

    response = asyncio.run(api_medications.intake_now(4, {"u_id": 7}))

    assert response.status_code == 409
    assert conn.recent_query is not None
    recent_query, recent_args = conn.recent_query
    assert "- INTERVAL" not in recent_query
    assert isinstance(recent_args[2], datetime.datetime)
