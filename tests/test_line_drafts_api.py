import asyncio
import datetime
import json
import sys
import types
import uuid

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app.dependencies import get_consented_user, get_current_user
from app.routers import api_line_drafts
from app.services import consent_service

NOW = datetime.datetime(2026, 11, 8, 12, 0, tzinfo=datetime.timezone.utc)


def _row(u_id=7, status="pending", expires_in=3600, age=0, meds=2):
    return {
        "draft_id": uuid.uuid4(), "u_id": u_id, "status": status,
        "created_at": NOW - datetime.timedelta(seconds=age),
        "expires_at": NOW + datetime.timedelta(seconds=expires_in), "saved_at": None,
        # asyncpg returns JSONB as text
        "result": json.dumps({"medications": [{"med_name": f"M{i}"} for i in range(meds)], "hospital": "H"}),
    }


class _Conn:
    """Applies the WHERE the routes ask for to an in-memory list: u_id, pending, not expired."""

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def _mine(self, u_id, draft_id=None):
        return [r for r in self.rows if r["u_id"] == u_id and r["status"] == "pending" and r["expires_at"] > NOW
                and (draft_id is None or r["draft_id"] == draft_id)]

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        assert "status = 'pending'" in query and "expires_at > NOW()" in query and "u_id = $1" in query
        return sorted(self._mine(args[0]), key=lambda r: r["created_at"], reverse=True)

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        assert "status = 'pending'" in query and "expires_at > NOW()" in query
        found = self._mine(args[0], args[1])
        if not found:
            return None
        if query.lstrip().startswith("UPDATE"):
            found[0]["status"] = "saved"
            found[0]["saved_at"] = NOW
        return found[0]


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *a):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


@pytest.fixture
def conn(monkeypatch):
    c = _Conn([])
    monkeypatch.setattr(api_line_drafts, "get_pool", lambda: _Pool(c))
    return c


USER = {"u_id": 7, "name": "x"}


def run(coro):
    return asyncio.run(coro)


def test_lists_only_my_pending_unexpired_drafts(conn):
    mine_new = _row(age=10, meds=3)
    mine_old = _row(age=500, meds=1)
    conn.rows += [mine_old, mine_new, _row(u_id=8), _row(status="saved"), _row(expires_in=-5), _row(status="reading")]
    out = run(api_line_drafts.list_drafts(USER))
    assert [d["id"] for d in out] == [str(mine_new["draft_id"]), str(mine_old["draft_id"])]   # newest first
    assert [d["count"] for d in out] == [3, 1]
    assert out[0]["created_at"] == mine_new["created_at"].isoformat()


def test_open_returns_parsed_result_and_someone_elses_draft_is_404(conn):
    mine = _row()
    theirs = _row(u_id=8)
    conn.rows += [mine, theirs]
    got = run(api_line_drafts.get_draft(mine["draft_id"], USER))
    assert got["id"] == str(mine["draft_id"]) and got["result"]["hospital"] == "H"
    with pytest.raises(HTTPException) as err:
        run(api_line_drafts.get_draft(theirs["draft_id"], USER))
    assert err.value.status_code == 404 and err.value.detail == "not_found"


def test_open_someone_elses_draft_is_404(conn):
    theirs = _row(u_id=8)
    conn.rows.append(theirs)
    with pytest.raises(HTTPException) as err:
        run(api_line_drafts.get_draft(theirs["draft_id"], USER))
    assert err.value.status_code == 404
    with pytest.raises(HTTPException) as err:
        run(api_line_drafts.mark_saved(theirs["draft_id"], USER))
    assert err.value.status_code == 404 and theirs["status"] == "pending"


def test_mark_saved(conn):
    mine = _row()
    conn.rows.append(mine)
    assert run(api_line_drafts.mark_saved(mine["draft_id"], USER)) == {"saved": True}
    assert mine["status"] == "saved" and mine["saved_at"] is not None
    with pytest.raises(HTTPException) as err:   # no longer pending
        run(api_line_drafts.mark_saved(mine["draft_id"], USER))
    assert err.value.status_code == 404


def test_routes_need_consent(monkeypatch):
    from app.main import app

    monkeypatch.setattr(app, "dependency_overrides", {})

    def calls(dep):
        out = {dep.call}
        for child in dep.dependencies:
            out |= calls(child)
        return out

    found = {(m, r.path) for r in app.routes if isinstance(r, APIRoute) and r.path.startswith("/api/line-drafts")
             for m in r.methods}
    assert found == {("GET", "/api/line-drafts"), ("GET", "/api/line-drafts/{draft_id}"),
                     ("POST", "/api/line-drafts/{draft_id}/saved")}
    for r in app.routes:
        if isinstance(r, APIRoute) and r.path.startswith("/api/line-drafts"):
            assert get_consented_user in calls(r.dependant)

    async def no_consent(u_id):
        return None

    monkeypatch.setattr(consent_service, "get_state", no_consent)
    monkeypatch.setattr(consent_service, "is_current", lambda state, kind: False)
    app.dependency_overrides[get_current_user] = lambda: USER
    r = TestClient(app).get("/api/line-drafts")
    assert r.status_code == 403 and r.json()["detail"] == "consent_required"
