import sys
import types

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app import config
from app.dependencies import get_current_user, get_consented_user
from app.routers import api_auth, api_history
from app.services import consent_service


EXEMPT_PATHS = {
    "/api/auth/onboarding-status",
    "/api/consent/status", "/api/consent",
    "/api/legal/current",
    "/api/account/export", "/api/account/delete",
    "/api/auth/face-login", "/api/auth/email-login", "/api/auth/register", "/api/auth/logout",
    "/api/face/login", "/api/face/identify", "/api/face/identify-bytes",
    "/api/notify/webhook/line", "/line/webhook",
}
# Limited mode for one method only: the same path is consent-gated for the other methods.
EXEMPT_ROUTES = {("GET", "/api/memory"), ("DELETE", "/api/memory"), ("DELETE", "/api/memory/fact")}


def _dependency_calls(dependant):
    calls = {dependant.call}
    for child in dependant.dependencies:
        calls.update(_dependency_calls(child))
    return calls


@pytest.fixture
def app(monkeypatch):
    # OpenVINO and ONNX imports are lazy in the actual services. Import the
    # real application, but don't enter its model/DB/scheduler lifespan.
    from app.main import app

    monkeypatch.setattr(app, "dependency_overrides", {})
    return app


def test_all_authenticated_routes_enforce_consent_except_explicit_exemptions(app):
    protected = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        calls = _dependency_calls(route.dependant)
        exempt = route.path in EXEMPT_PATHS or any((method, route.path) in EXEMPT_ROUTES for method in route.methods)
        if exempt:
            assert get_consented_user not in calls, (route.methods, route.path)
        elif get_current_user in calls:
            assert get_consented_user in calls, (route.methods, route.path)
            protected.add(route.path)

    assert {
        "/api/emotion/log", "/api/face/enroll", "/api/face/enrollment-status",
        "/api/family/contacts", "/api/history/intakes", "/api/intake/record",
        "/api/medications", "/api/ocr/parse", "/api/notify/settings",
        "/api/auth/link-account", "/api/auth/onboarding-complete",
        "/api/intake/monitor/start", "/api/intake/monitor/landmarks",
        "/api/intake/monitor/vision", "/api/intake/monitor/outcome",
        "/api/intake/monitor/end", "/api/intake/monitor/recent", "/api/intake/monitor/undo",
        "/api/memory", "/api/memory/fact",
    } <= protected


class _Connection:
    def __init__(self):
        self.history_reads = 0

    async def fetch(self, query, *args):
        assert "FROM intake i" in query
        assert args[0] == 7
        self.history_reads += 1
        return [{"id": 11, "medication_name": "Test medication", "status": "taken", "total": 1}]

    async def fetchrow(self, query, *args):
        assert 'FROM "user"' in query
        assert args == (7,)
        return {"u_id": 7, "face_enrolled": False, "onboarding_complete": False}


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


@pytest.mark.parametrize("initial_state", [
    {},
    {"core": {"granted": False, "terms_version": config.TERMS_VERSION}},
    {"core": {"granted": True, "terms_version": "old"}},
])
def test_history_blocks_without_current_consent_then_allows_current_consent(app, monkeypatch, initial_state):
    state = initial_state
    conn = _Connection()

    async def get_state(u_id):
        assert u_id == 7
        return state

    monkeypatch.setattr(consent_service, "get_state", get_state)
    monkeypatch.setattr(api_history, "get_pool", lambda: _Pool(conn))
    app.dependency_overrides[get_current_user] = lambda: {"u_id": 7, "name": "Test"}
    client = TestClient(app)
    response = client.get("/api/history/intakes")
    assert response.status_code == 403
    assert response.json() == {"detail": "consent_required"}
    assert conn.history_reads == 0

    state = {"core": {"granted": True, "terms_version": config.TERMS_VERSION}}
    response = client.get("/api/history/intakes")
    assert response.status_code == 200
    assert response.json()["items"] == [{"id": 11, "medication_name": "Test medication", "status": "taken"}]
    assert conn.history_reads == 1
    client.close()


def test_onboarding_status_and_consent_status_work_in_limited_mode(app, monkeypatch):
    checked = []

    async def get_state(u_id):
        checked.append(u_id)
        return {}

    monkeypatch.setattr(consent_service, "get_state", get_state)
    monkeypatch.setattr(api_auth, "get_pool", lambda: _Pool(_Connection()))
    app.dependency_overrides[get_current_user] = lambda: {"u_id": 7, "name": "Test"}
    client = TestClient(app)
    response = client.get("/api/auth/onboarding-status")
    assert response.status_code == 200
    assert response.json() == {"onboarding_complete": False, "face_enrolled": False}
    assert checked == []
    response = client.get("/api/consent/status")
    assert response.status_code == 200
    assert response.json()["core_current"] is False
    assert checked == [7]
    client.close()


def test_unauthenticated_history_rejected_before_consent_lookup(app, monkeypatch):
    async def get_state(u_id):
        pytest.fail("Unauthenticated requests must not query consent")

    monkeypatch.setattr(consent_service, "get_state", get_state)
    client = TestClient(app)
    assert client.get("/api/history/intakes").status_code == 401
    client.close()
