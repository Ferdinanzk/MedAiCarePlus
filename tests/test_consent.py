import asyncio
import datetime
import sys
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app import config
from app.dependencies import get_current_user
from app.routers import api_auth, api_consent
from app.services import consent_service as service, legal_service


class _Transaction:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        assert not self.conn.in_transaction
        self.conn.in_transaction = True
        self.snapshot = list(self.conn.rows)
        return self

    async def __aexit__(self, exc_type, *args):
        self.conn.in_transaction = False
        self.conn.committed = exc_type is None
        if exc_type is not None:
            self.conn.rows = self.snapshot
        return False


class _Connection:
    def __init__(self):
        self.rows = []
        self.statements = []
        self.fetches = 0
        self.in_transaction = False
        self.committed = False
        self.timestamp = datetime.datetime(2026, 9, 30, tzinfo=datetime.timezone.utc)

    def transaction(self):
        return _Transaction(self)

    async def fetchval(self, query, *args):
        assert 'INSERT INTO "user"' in query
        self.statements.append((query, args, self.in_transaction))
        return 7

    async def execute(self, query, *args):
        self.statements.append((query, args, self.in_transaction))
        if "INSERT INTO consent" in query:
            self.rows.append(dict(zip(
                ("u_id", "kind", "terms_version", "language", "document_sha256", "scope",
                 "granted", "source", "user_agent"), args),
                consent_id=len(self.rows) + 1, created_at=self.timestamp))
        else:
            # A core withdrawal also aborts open robot tasks in the same transaction.
            assert "INSERT INTO detail" in query or query.startswith("UPDATE reachy_task SET status = 'aborted'")

    async def fetch(self, query, *args):
        assert "SELECT DISTINCT ON (scope)" in query
        assert "ORDER BY scope, consent_id DESC" in query
        self.fetches += 1
        latest = {}
        for row in sorted(self.rows, key=lambda row: row["consent_id"], reverse=True):
            if row["u_id"] == args[0]:
                latest.setdefault(row["scope"], row)
        return list(latest.values())


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


@pytest.fixture(autouse=True)
def isolated_consent(monkeypatch):
    monkeypatch.setattr(service, "_cache", {})

    def document(kind, language):
        language = legal_service.normalise_language(language)
        doc = {"kind": kind, "version": config.TERMS_VERSION, "language": language}
        return legal_service.LegalDocument(
            kind, config.TERMS_VERSION, language, doc, legal_service.canonical_hash(doc), True)

    monkeypatch.setattr(legal_service, "get_document", document)


def _payload(kind="core", scopes=None, **changes):
    payload = {
        "kind": kind, "terms_version": config.TERMS_VERSION, "language": "en",
        "document_sha256": legal_service.get_document(kind, "en").sha256,
        "scopes": scopes if scopes is not None else {"core": True}, "source": "settings",
    }
    return {**payload, **changes}


def _record(conn, **changes):
    return service.record(conn, 7, **_payload(**changes), user_agent="test-agent")


def _client(monkeypatch, conn):
    for module in (api_auth, api_consent, service):
        monkeypatch.setattr(module, "get_pool", lambda: _Pool(conn))
    app = FastAPI()
    app.include_router(api_auth.router)
    app.include_router(api_consent.router)
    app.dependency_overrides[get_current_user] = lambda: {"u_id": 7, "name": "Test"}
    return TestClient(app)


@pytest.mark.parametrize(("changes", "code"), [
    ({"terms_version": "old"}, "stale_terms_version"),
    ({"document_sha256": "0" * 64}, "document_hash_mismatch"),
    ({"language": "zh-TW"}, "document_hash_mismatch"),
    ({"scopes": {"core": True, "robot_camera": True}}, "invalid_scope"),
    ({"kind": "unknown"}, "invalid_scope"),
    ({"scopes": {}}, "invalid_scope"),
    ({"scopes": {"core": "true"}}, "invalid_scope"),
    ({"scopes": {"core": False}, "source": "register"}, "core_required"),
])
def test_record_rejects_invalid_consent_before_writing(changes, code):
    conn = _Connection()
    with pytest.raises(service.ConsentError) as exc:
        asyncio.run(_record(conn, **changes))
    assert exc.value.code == code
    assert conn.statements == []


def test_record_inserts_scopes_in_dict_order_and_normalises_language():
    conn = _Connection()
    scopes = {"safety_alerts": False, "robot_camera": True, "cloud_voice": False}
    asyncio.run(_record(
        conn, kind="robot", scopes=scopes, language="zh",
        document_sha256=legal_service.get_document("robot", "zh-TW").sha256))
    assert [row["scope"] for row in conn.rows] == list(scopes)
    assert [row["granted"] for row in conn.rows] == list(scopes.values())
    assert all(row["language"] == "zh-TW" and row["user_agent"] == "test-agent" for row in conn.rows)


def test_fetch_state_uses_highest_consent_id_when_timestamps_tie():
    conn = _Connection()

    async def scenario():
        await _record(conn)
        await _record(conn, scopes={"core": False})
        state = await service.fetch_state(conn, 7)
        assert conn.rows[0]["created_at"] == conn.rows[1]["created_at"]
        assert state["core"]["consent_id"] == 2
        assert state["core"]["granted"] is False
        assert await service.fetch_state(conn, 8) == {}

    asyncio.run(scenario())


def test_cache_hit_expiry_and_invalidate(monkeypatch):
    conn = _Connection()
    monkeypatch.setattr(service, "get_pool", lambda: _Pool(conn))
    clock = types.SimpleNamespace(monotonic=lambda: now[0])
    now = [100.0]
    monkeypatch.setattr(service, "time", clock)

    async def scenario():
        await _record(conn)
        state = await service.get_state(7)
        state["core"]["granted"] = False
        now[0] += 4.99
        assert service.is_current(await service.get_state(7), "core")
        assert conn.fetches == 1
        now[0] = 105.0
        await service.get_state(7)
        assert conn.fetches == 2
        service.invalidate(7)
        await service.get_state(7)
        assert conn.fetches == 3
        await _record(conn, scopes={"core": False})
        assert not service.is_current(await service.get_state(7), "core")
        assert conn.fetches == 4

    asyncio.run(scenario())


def test_invalidation_during_read_does_not_repopulate_cache(monkeypatch):
    conn = _Connection()
    monkeypatch.setattr(service, "get_pool", lambda: _Pool(conn))

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        reads = []

        async def fetch_state(connection, u_id):
            reads.append(u_id)
            started.set()
            await release.wait()
            return {}

        monkeypatch.setattr(service, "fetch_state", fetch_state)
        pending = asyncio.create_task(service.get_state(7))
        await started.wait()
        service.invalidate(7)
        release.set()
        await pending
        await service.get_state(7)
        assert reads == [7, 7]

    asyncio.run(scenario())


def test_status_requires_current_grants_for_core_and_all_robot_scopes():
    state = {scope: {"granted": True, "terms_version": config.TERMS_VERSION}
             for scope in legal_service.SCOPE_KIND}
    assert service.status_payload(state)["robot_current"] is True
    state["safety_alerts"]["granted"] = False
    state["core"]["terms_version"] = "old"
    status = service.status_payload(state)
    assert status["core_current"] is False
    assert status["robot_current"] is False
    assert status["scopes"]["robot_camera"]["current"] is True
    assert service.status_payload({})["scopes"]["core"]["granted"] is False


@pytest.mark.parametrize("consent", [None, {"core": False}, {}])
def test_register_requires_core_consent(monkeypatch, consent):
    conn = _Connection()
    body = {"name": "Test", "face_label": "test"}
    if consent is not None:
        body["consent"] = {key: value for key, value in _payload(scopes=consent).items()
                           if key not in ("kind", "source")}
    response = _client(monkeypatch, conn).post("/api/auth/register", json=body)
    assert response.status_code == 400
    assert response.json() == {"success": False, "error": "core_required"}
    assert conn.statements == []


def test_register_writes_user_details_and_consent_in_same_transaction(monkeypatch):
    conn = _Connection()
    consent = {key: value for key, value in _payload().items() if key not in ("kind", "source")}
    response = _client(monkeypatch, conn).post("/api/auth/register", json={
        "name": "Test", "face_label": "test", "consent": consent,
    }, headers={"user-agent": "registration-test"})
    assert response.status_code == 200
    assert response.json()["success"] is True
    assert response.json()["token"]
    assert conn.committed
    assert len(conn.statements) == 3
    assert all(in_transaction for _, _, in_transaction in conn.statements)
    assert 'INSERT INTO "user"' in conn.statements[0][0]
    assert "INSERT INTO consent" in conn.statements[2][0]
    assert conn.rows[0]["source"] == "register"
    assert conn.rows[0]["user_agent"] == "registration-test"
    assert 7 not in service._cache


def test_register_rolls_back_on_hash_mismatch(monkeypatch):
    conn = _Connection()
    consent = {key: value for key, value in _payload(document_sha256="wrong").items()
               if key not in ("kind", "source")}
    response = _client(monkeypatch, conn).post("/api/auth/register", json={
        "name": "Test", "face_label": "test", "consent": consent,
    })
    assert response.status_code == 409
    assert response.json() == {"success": False, "error": "document_hash_mismatch"}
    assert not conn.committed
    assert conn.rows == []


@pytest.mark.parametrize(("changes", "status", "code"), [
    ({"terms_version": "old"}, 409, "stale_terms_version"),
    ({"document_sha256": "wrong"}, 409, "document_hash_mismatch"),
    ({"scopes": {"robot_camera": True}}, 422, "invalid_scope"),
])
def test_consent_api_error_codes(monkeypatch, changes, status, code):
    conn = _Connection()
    response = _client(monkeypatch, conn).post("/api/consent", json=_payload(**changes))
    assert response.status_code == status
    assert response.json() == {"detail": code}
    assert conn.statements == []


def test_consent_api_grant_and_withdrawal_update_status(monkeypatch):
    conn = _Connection()
    client = _client(monkeypatch, conn)
    assert client.get("/api/consent/status").json()["core_current"] is False
    response = client.post("/api/consent", json=_payload(), headers={"user-agent": "consent-test"})
    assert response.status_code == 200
    assert response.json()["core_current"] is True
    assert conn.committed
    assert conn.rows[0]["user_agent"] == "consent-test"
    assert all(in_transaction for _, _, in_transaction in conn.statements)
    response = client.post("/api/consent", json=_payload(scopes={"core": False}))
    assert response.status_code == 200
    assert response.json()["core_current"] is False
    assert client.get("/api/consent/status").json()["core_current"] is False
