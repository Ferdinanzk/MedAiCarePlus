"""POST /api/notify/generate-code takes the JSON body the Family page sends."""

import sys
import types

from fastapi.testclient import TestClient

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app import config
from app.dependencies import get_current_user
from app.routers import api_notify
from app.services import consent_service


class Conn:
    def __init__(self, owned):
        self.owned, self.calls = owned, []

    async def fetchval(self, query, *args):
        self.calls.append(args)
        code, contact_id, u_id = args
        return contact_id if (contact_id, u_id) in self.owned else None


class Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False
        return Ctx()


def _client(monkeypatch, conn):
    from app.main import app

    async def get_state(u_id):
        return {"core": {"granted": True, "terms_version": config.TERMS_VERSION}}

    monkeypatch.setattr(consent_service, "get_state", get_state)
    monkeypatch.setattr(api_notify, "get_pool", lambda: Pool(conn))
    monkeypatch.setattr(app, "dependency_overrides", {get_current_user: lambda: {"u_id": 1, "name": "A"}})
    return TestClient(app)


def test_json_body_from_the_family_page_returns_a_code(monkeypatch):
    conn = Conn(owned={(5, 1)})
    response = _client(monkeypatch, conn).post("/api/notify/generate-code", json={"contact_id": 5})
    assert response.status_code == 200
    code = response.json()["code"]
    assert len(code) == 8 and conn.calls == [(code, 5, 1)]


def test_someone_elses_or_missing_contact_gets_404(monkeypatch):
    client = _client(monkeypatch, Conn(owned={(5, 2)}))
    assert client.post("/api/notify/generate-code", json={"contact_id": 5}).status_code == 404
    assert client.post("/api/notify/generate-code", json={}).status_code == 422
