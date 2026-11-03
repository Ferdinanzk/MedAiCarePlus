"""The LINE reminder switch's routes: the banner's state for any user, the admin's switch and the month's usage."""

import datetime
import logging
import sys
import types

import pytest
from fastapi.testclient import TestClient

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app import dependencies
from app.dependencies import get_consented_user
from app.main import app
from app.routers import api_line_admin
from app.services import line_pause, line_service
from app.services.line_service import LineService

ADMIN_ID, OTHER_ID = 7, 8


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeConn:
    def __init__(self):
        self.users = {ADMIN_ID: {"email": "Boss@Example.com", "name": "Boss"},
                      OTHER_ID: {"email": "someone@example.com", "name": "Other"}}
        self.log_rows = []

    def transaction(self):
        return _Tx()

    async def fetchval(self, query, *args):
        user = self.users.get(args[0])
        if query == 'SELECT email FROM "user" WHERE u_id = $1':
            return user["email"] if user else None
        if query == 'SELECT name FROM "user" WHERE u_id = $1':
            return user["name"] if user else None
        raise AssertionError(query)

    async def execute(self, query, *args):
        if "INSERT INTO line_switch_log" in query:
            self.log_rows.append(args)


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *args):
                return False
        return _Ctx()


class FakeResponse:
    def __init__(self, status=200, body=None, bad_json=False):
        self.status_code, self._body, self._bad = status, body, bad_json

    def json(self):
        if self._bad:
            raise ValueError("not json")
        return self._body


@pytest.fixture
def env(monkeypatch):
    conn = FakeConn()
    user = {"u_id": ADMIN_ID, "name": "Boss"}
    monkeypatch.setattr(dependencies, "get_pool", lambda: FakePool(conn))
    monkeypatch.setattr(api_line_admin, "get_pool", lambda: FakePool(conn))
    monkeypatch.setattr(dependencies, "ADMIN_EMAILS", frozenset({"boss@example.com"}))
    monkeypatch.setitem(line_pause._state, "paused", False)
    monkeypatch.setitem(line_pause._state, "since", None)
    monkeypatch.setitem(line_pause._state, "by", None)
    LineService.get_instance()   # its first construction sets _available from the (empty) token
    monkeypatch.setattr(LineService, "_available", True)
    monkeypatch.setattr(LineService, "_quota_cache", None)
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if url == line_service.QUOTA_URL:
            return FakeResponse(200, {"type": "limited", "value": 200})
        return FakeResponse(200, {"totalUsage": 57})
    monkeypatch.setattr(line_service.requests, "get", fake_get)
    monkeypatch.setitem(app.dependency_overrides, get_consented_user, lambda: user)
    yield types.SimpleNamespace(conn=conn, user=user, calls=calls, client=TestClient(app), monkeypatch=monkeypatch)
    app.dependency_overrides.pop(get_consented_user, None)


def test_banner_state_for_any_user(env):
    env.user["u_id"] = OTHER_ID
    assert env.client.get("/api/line/reminders").json() == {"paused": False, "since": None}
    line_pause._state.update(paused=True, since=datetime.datetime(2026, 11, 2, 9, 30), by=ADMIN_ID)
    assert env.client.get("/api/line/reminders").json() == {"paused": True, "since": "2026-11-02T09:30:00"}


def test_admin_can_pause_and_resume(env, caplog):
    with caplog.at_level(logging.INFO, logger="app.routers.api_line_admin"):
        paused = env.client.put("/api/admin/line/reminders", json={"paused": True})
        assert paused.status_code == 200
        body = paused.json()
        assert body["paused"] is True and body["since"] and body["changed_by"] == "Boss"
        assert body["quota"] == {"limit": 200, "used": 57, "error": None}
        assert env.client.get("/api/line/reminders").json()["paused"] is True
        resumed = env.client.put("/api/admin/line/reminders", json={"paused": False})
    assert f"LINE medication reminders paused by user {ADMIN_ID}" in caplog.text
    assert resumed.json()["paused"] is False and resumed.json()["since"] is None
    assert resumed.json()["changed_by"] is None
    assert f"LINE medication reminders resumed by user {ADMIN_ID}" in caplog.text
    assert [row[:2] for row in env.conn.log_rows] == [(True, ADMIN_ID), (False, ADMIN_ID)]
    assert env.client.get("/api/line/reminders").json() == {"paused": False, "since": None}


def test_changed_by_is_null_when_that_user_is_gone(env):
    line_pause._state.update(paused=True, since=datetime.datetime(2026, 11, 2), by=99)
    assert env.client.get("/api/admin/line").json()["changed_by"] is None


def test_non_admin_gets_403(env):
    env.user["u_id"] = OTHER_ID
    for response in (env.client.get("/api/admin/line"),
                     env.client.put("/api/admin/line/reminders", json={"paused": True})):
        assert response.status_code == 403 and response.json() == {"detail": "admin_only"}
    assert line_pause.state()["paused"] is False and env.conn.log_rows == []
    assert env.calls == []


def test_no_admin_configured_is_403(env):
    env.monkeypatch.setattr(dependencies, "ADMIN_EMAILS", frozenset())
    assert env.client.get("/api/admin/line").status_code == 403
    env.monkeypatch.setattr(dependencies, "ADMIN_EMAILS", frozenset({""}))
    env.conn.users[ADMIN_ID]["email"] = None
    response = env.client.put("/api/admin/line/reminders", json={"paused": True})
    assert response.status_code == 403 and response.json() == {"detail": "admin_only"}
    env.conn.users[ADMIN_ID]["email"] = ""
    assert env.client.get("/api/admin/line").status_code == 403


@pytest.mark.parametrize("value", ["true", 1, 0, None, "yes"])
def test_paused_must_be_boolean(env, value):
    assert env.client.put("/api/admin/line/reminders", json={"paused": value}).status_code == 422
    assert env.client.put("/api/admin/line/reminders", json={}).status_code == 422
    assert line_pause.state()["paused"] is False and env.conn.log_rows == []


def test_quota_failure_keeps_card(env):
    env.monkeypatch.setattr(LineService, "quota", lambda self: {"limit": None, "used": None, "error": "http_403"})
    response = env.client.get("/api/admin/line")
    assert response.status_code == 200
    assert response.json()["quota"] == {"limit": None, "used": None, "error": "http_403"}
    assert env.client.put("/api/admin/line/reminders", json={"paused": True}).status_code == 200


def test_quota_is_cached(env):
    assert env.client.get("/api/admin/line").json()["quota"]["used"] == 57
    assert env.client.get("/api/admin/line").json()["quota"]["used"] == 57
    assert len(env.calls) == 2


def test_quota_unlimited_plan(env):
    env.monkeypatch.setattr(line_service.requests, "get", lambda url, **kw: FakeResponse(
        200, {"type": "none"} if url == line_service.QUOTA_URL else {"totalUsage": 3}))
    assert LineService.get_instance().quota() == {"limit": None, "used": 3, "error": None}


def test_quota_failures_are_not_cached_and_leak_nothing(env):
    secret = "tok-secret-123"
    env.monkeypatch.setattr(line_service, "LINE_CHANNEL_ACCESS_TOKEN", secret)
    service = LineService.get_instance()

    env.monkeypatch.setattr(line_service.requests, "get", lambda url, **kw: FakeResponse(403, {"message": secret}))
    assert service.quota() == {"limit": None, "used": None, "error": "http_403"}

    def boom(url, **kw):
        raise RuntimeError(f"connection to {secret} failed")
    env.monkeypatch.setattr(line_service.requests, "get", boom)
    assert service.quota() == {"limit": None, "used": None, "error": "request_failed"}

    env.monkeypatch.setattr(line_service.requests, "get", lambda url, **kw: FakeResponse(200, bad_json=True))
    assert service.quota() == {"limit": None, "used": None, "error": "bad_response"}
    env.monkeypatch.setattr(line_service.requests, "get", lambda url, **kw: FakeResponse(200, {"type": "weird"}))
    assert service.quota()["error"] == "bad_response"
    assert LineService._quota_cache is None
    env.monkeypatch.setattr(line_service.requests, "get", lambda url, **kw: FakeResponse(
        200, {"type": "limited", "value": 10} if url == line_service.QUOTA_URL else {"totalUsage": 1}))
    assert service.quota() == {"limit": 10, "used": 1, "error": None}


def test_quota_when_line_is_not_configured(env):
    env.monkeypatch.setattr(LineService, "_available", False)
    assert LineService.get_instance().quota() == {"limit": None, "used": None, "error": "line_unavailable"}
    assert env.calls == [] and LineService._quota_cache is None
