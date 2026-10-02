"""Device tokens, pairing, and the port/token isolation between device and user auth."""

import asyncio
import hashlib
import sys
import types
import uuid

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app import config
from app.config import DEVICE_PORT, PUBLIC_PORT
from app.dependencies import get_current_user
from app.routers import api_reachy
from app.services import consent_service, device_auth, reachy_tasks

CORE = {"granted": True, "terms_version": config.TERMS_VERSION}
ROBOT_CONSENT = {"core": CORE, "robot_camera": {"granted": True, "terms_version": config.TERMS_VERSION}}


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeConn:
    def __init__(self):
        self.devices = []            # rows of reachy_device
        self.executed = []

    def transaction(self):
        return _Transaction()

    async def fetchrow(self, query, *args):
        if "FROM reachy_device d JOIN \"user\" u" in query:
            for row in self.devices:
                if row["token_hash"] == args[0]:
                    return {**row, "face_label": "pearl", "name": "Pearl"}
            return None
        if "FROM reachy_device WHERE u_id = $1 AND revoked_at IS NULL" in query:
            for row in self.devices:
                if row["u_id"] == args[0] and row["revoked_at"] is None:
                    return {"device_id": row["device_id"], "label": "Reachy Mini", "auto_record": row["auto_record"],
                            "last_seen_at": None, "robot_reachable": None, "landmark_fps": None}
            return None
        if "FROM reachy_task WHERE u_id = $1" in query:
            return None
        raise AssertionError(query)

    async def fetchval(self, query, *args):
        if "FROM family_contacts" in query:
            return 0   # contacts that can receive family alerts
        raise AssertionError(query)

    async def execute(self, query, *args):
        self.executed.append((query, args))
        if "UPDATE reachy_device SET revoked_at = NOW()" in query:
            for row in self.devices:
                if row["u_id"] == args[0] and row["revoked_at"] is None:
                    row["revoked_at"] = "now"
        elif "INSERT INTO reachy_device" in query:
            self.devices.append({"device_id": uuid.UUID(args[0]), "u_id": args[1], "token_hash": args[2],
                                 "auto_record": False, "revoked_at": None})


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Acquire:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *args):
                return False
        return _Acquire()


@pytest.fixture
def env(monkeypatch):
    from app.main import app

    conn = FakeConn()
    consent = {7: dict(ROBOT_CONSENT)}

    async def get_state(u_id):
        return consent.get(u_id, {})

    async def current_task(u_id, device_id):
        return {"task_id": "t", "u_id": u_id, "device_id": device_id}

    monkeypatch.setattr(app, "dependency_overrides", {})
    monkeypatch.setattr(consent_service, "get_state", get_state)
    monkeypatch.setattr(device_auth, "get_pool", lambda: FakePool(conn))
    monkeypatch.setattr(api_reachy, "get_pool", lambda: FakePool(conn))
    monkeypatch.setattr(reachy_tasks, "current_task", current_task)
    return types.SimpleNamespace(app=app, conn=conn, consent=consent,
                                 device=TestClient(app, base_url=f"http://testserver:{DEVICE_PORT}"),
                                 public=TestClient(app, base_url=f"http://testserver:{PUBLIC_PORT}"))


def _store(conn, device_id, u_id, token, revoked=False):
    conn.devices.append({"device_id": uuid.UUID(device_id), "u_id": u_id, "token_hash": device_auth.hash_token(token),
                         "auto_record": False, "revoked_at": "then" if revoked else None})


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_token_format_and_hash():
    device_id = str(uuid.uuid4())
    token = device_auth.issue_token(device_id, 7)
    assert token.startswith("rdv1.") and device_auth.TOKEN_PREFIX == "rdv1."
    assert device_auth._signer.loads(token[5:]) == {"d": device_id, "u": 7}
    assert device_auth.hash_token(token) == hashlib.sha256(token.encode()).hexdigest()
    assert len(device_auth.hash_token(token)) == 64


def test_valid_token_authorises_device_route(env):
    device_id = str(uuid.uuid4())
    token = device_auth.issue_token(device_id, 7)
    _store(env.conn, device_id, 7, token)
    response = env.device.get("/api/device/tasks/current", headers=_auth(token))
    assert response.status_code == 200
    assert response.json() == {"task_id": "t", "u_id": 7, "device_id": device_id}


@pytest.mark.parametrize("case", ["missing", "garbage", "face_token", "unstored", "revoked", "claims_mismatch",
                                  "wrong_salt"])
def test_invalid_device_tokens_are_401(env, case):
    device_id = str(uuid.uuid4())
    token = device_auth.issue_token(device_id, 7)
    headers = _auth(token)
    if case == "missing":
        headers = {}
    elif case == "garbage":
        headers = _auth("rdv1.not-a-token")
    elif case == "face_token":
        from itsdangerous import URLSafeTimedSerializer
        headers = _auth(URLSafeTimedSerializer(config.SECRET_KEY).dumps({"u_id": 7, "name": "Pearl"}))
    elif case == "revoked":
        _store(env.conn, device_id, 7, token, revoked=True)
    elif case == "claims_mismatch":
        # A stored hash whose row belongs to another user must not authorise.
        _store(env.conn, device_id, 8, token)
    elif case == "wrong_salt":
        from itsdangerous import URLSafeTimedSerializer
        forged = "rdv1." + URLSafeTimedSerializer(config.SECRET_KEY).dumps({"d": device_id, "u": 7})
        _store(env.conn, device_id, 7, forged)
        headers = _auth(forged)
    response = env.device.get("/api/device/tasks/current", headers=headers)
    assert response.status_code == 401


@pytest.mark.parametrize("state", [
    {}, {"core": CORE},
    {"core": CORE, "robot_camera": {"granted": False, "terms_version": config.TERMS_VERSION}},
    {"core": CORE, "robot_camera": {"granted": True, "terms_version": "old"}},
])
def test_device_route_requires_current_robot_camera_consent(env, state):
    device_id = str(uuid.uuid4())
    token = device_auth.issue_token(device_id, 7)
    _store(env.conn, device_id, 7, token)
    env.consent[7] = state
    response = env.device.get("/api/device/tasks/current", headers=_auth(token))
    assert response.status_code == 403 and response.json() == {"detail": "consent_required"}


def test_device_routes_are_404_on_public_port(env):
    device_id = str(uuid.uuid4())
    token = device_auth.issue_token(device_id, 7)
    _store(env.conn, device_id, 7, token)
    assert env.public.get("/api/device/tasks/current", headers=_auth(token)).status_code == 404
    assert env.public.post("/api/device/heartbeat", json={}).status_code == 404


def test_device_token_rejected_by_user_routes(env):
    device_id = str(uuid.uuid4())
    token = device_auth.issue_token(device_id, 7)
    _store(env.conn, device_id, 7, token)
    # Through the public listener (middleware) ...
    assert env.public.get("/api/reachy/status", headers=_auth(token)).status_code == 401
    # ... and by get_current_user itself, independent of the middleware.
    with pytest.raises(HTTPException) as exc:
        asyncio.run(get_current_user(HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)))
    assert exc.value.status_code == 401


def test_face_token_not_accepted_as_device_token():
    from itsdangerous import URLSafeTimedSerializer
    face = URLSafeTimedSerializer(config.SECRET_KEY).dumps({"u_id": 7, "name": "Pearl"})
    assert device_auth._claims(face) is None
    assert device_auth._claims("rdv1." + face) is None


# ── Pairing (public port, user auth) ─────────────────────────────────────────

def _as_user(env, u_id=7):
    env.app.dependency_overrides[get_current_user] = lambda: {"u_id": u_id, "name": "Pearl"}


def test_pairing_requires_robot_camera_consent(env):
    _as_user(env)
    env.consent[7] = {"core": CORE}
    response = env.public.post("/api/reachy/pairing")
    assert response.status_code == 403 and response.json() == {"detail": "robot_consent_required"}
    assert env.conn.devices == []


def test_pairing_requires_core_consent(env):
    _as_user(env)
    env.consent[7] = {}
    assert env.public.post("/api/reachy/pairing").json() == {"detail": "consent_required"}


def test_pairing_returns_token_once_stores_hash_and_replaces_active_device(env):
    _as_user(env)
    first = env.public.post("/api/reachy/pairing")
    assert first.status_code == 200
    body = first.json()
    assert set(body) == {"device_id", "token"} and body["token"].startswith("rdv1.")
    stored = env.conn.devices[0]
    assert stored["token_hash"] == device_auth.hash_token(body["token"]) and body["token"] not in str(stored)
    assert env.device.get("/api/device/tasks/current", headers=_auth(body["token"])).status_code == 200

    second = env.public.post("/api/reachy/pairing").json()
    assert second["device_id"] != body["device_id"]
    assert [row["revoked_at"] is None for row in env.conn.devices] == [False, True]
    assert env.device.get("/api/device/tasks/current", headers=_auth(body["token"])).status_code == 401
    assert env.device.get("/api/device/tasks/current", headers=_auth(second["token"])).status_code == 200
    # The token is never shown again.
    status = env.public.get("/api/reachy/status").json()
    assert status["paired"] is True and status["device_id"] == second["device_id"]
    assert "token" not in status


def test_pairing_routes_not_served_on_device_port(env):
    _as_user(env)
    assert env.device.post("/api/reachy/pairing").status_code == 404


def test_heartbeat_reports_stop_all_for_revoked_device_or_withdrawn_consent(env):
    device_id = str(uuid.uuid4())
    token = device_auth.issue_token(device_id, 7)
    _store(env.conn, device_id, 7, token, revoked=True)
    response = env.device.post("/api/device/heartbeat", json={}, headers=_auth(token))
    assert response.status_code == 200 and response.json()["stop_all"] is True

    other_id = str(uuid.uuid4())
    other = device_auth.issue_token(other_id, 7)
    _store(env.conn, other_id, 7, other)
    env.consent[7] = {"core": CORE}
    response = env.device.post("/api/device/heartbeat", json={}, headers=_auth(other))
    assert response.status_code == 200 and response.json()["stop_all"] is True
    assert not any("last_seen_at" in query for query, _ in env.conn.executed)
