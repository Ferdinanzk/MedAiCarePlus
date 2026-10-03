import sys
import types

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dependencies import get_current_user
from app.routers import api_consent
from app.services import consent_service, deletion_ledger


class _Conn:
    def __init__(self):
        self.executed = []

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, *args):
        self.executed.append((" ".join(sql.split()), args))


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return self.conn


def _client(monkeypatch):
    conn = _Conn()
    monkeypatch.setattr(api_consent, "get_pool", lambda: _Pool(conn))
    monkeypatch.setattr(deletion_ledger, "append_host_file", lambda *entry: None)   # never the real ledger

    async def record(*args, **kwargs):
        return None

    async def state(u_id):
        return {}

    monkeypatch.setattr(consent_service, "record", record)
    monkeypatch.setattr(consent_service, "get_state", state)
    app = FastAPI()
    app.include_router(api_consent.router)
    app.dependency_overrides[get_current_user] = lambda: {"u_id": 7, "name": "P"}
    return TestClient(app), conn


def _post(client, kind, scopes):
    return client.post("/api/consent", json={
        "kind": kind, "terms_version": "2026-10", "language": "en", "document_sha256": "a" * 64,
        "scopes": scopes, "source": "settings"})


def test_withdrawing_robot_camera_revokes_device_and_aborts_tasks_in_same_transaction(monkeypatch):
    client, conn = _client(monkeypatch)
    assert _post(client, "robot", {"robot_camera": False}).status_code == 200
    statements = [sql for sql, _ in conn.executed]
    assert any(sql.startswith("UPDATE reachy_device SET revoked_at = NOW()") for sql in statements)
    aborts = [(sql, args) for sql, args in conn.executed if sql.startswith("UPDATE reachy_task SET status = 'aborted'")]
    assert aborts and aborts[0][1] == (7, "consent_withdrawn")


def test_withdrawing_core_aborts_tasks_but_keeps_pairing(monkeypatch):
    client, conn = _client(monkeypatch)
    assert _post(client, "core", {"core": False}).status_code == 200
    statements = [sql for sql, _ in conn.executed]
    assert not any("reachy_device" in sql for sql in statements)
    assert any(sql.startswith("UPDATE reachy_task SET status = 'aborted'") for sql in statements)


def test_granting_consent_touches_no_robot_state(monkeypatch):
    client, conn = _client(monkeypatch)
    assert _post(client, "robot", {"robot_camera": True}).status_code == 200
    assert _post(client, "core", {"core": True}).status_code == 200
    assert conn.executed == []
