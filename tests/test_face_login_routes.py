"""Every face login route takes up to 5 frames and answers with a reason code (Task 4 of the face login plan).

The face service is a fake whose login() answers from a script; the database is a fake pool. The routes run in
a real FastAPI app through TestClient, so the multipart field name and the status codes are the real ones.
"""
import io
import logging
import sys
import time
import types

import cv2
import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner, URLSafeTimedSerializer

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app.config import SECRET_KEY
from app.routers import api_auth, api_face, auth
from app.services.face_login import LoginDecision
from app.services.face_recognition_service import FaceRecognitionService

USERS = {"pearl": {"u_id": 7, "name": "Pearl", "line_id": None}}

ROUTES = ["/api/face/login", "/api/face/identify", "/api/auth/face-login", "/auth/face-frame"]

MATCH = LoginDecision(True, "pearl", 0.12, "match", 5, face_count=1)
NOBODY = LoginDecision(False, None, None, "unknown", 5, face_count=1)


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *args):
        return False


class _Connection:
    def __init__(self):
        self.lookups = []
        self.executed = []

    async def fetchrow(self, query, *args):
        assert 'FROM "user"' in query and "face_label" in query and "user_active" in query
        self.lookups.append(args[0])
        return USERS.get(args[0])

    async def execute(self, query, *args):
        self.executed.append((query, args))


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


class _FakeFaces:
    def __init__(self, decision):
        self.decision = decision
        self.calls = []

    def login(self, frames):
        self.calls.append(list(frames))
        return self.decision


@pytest.fixture
def env(monkeypatch):
    conn = _Connection()
    for module in (api_face, api_auth, auth):
        monkeypatch.setattr(module, "get_pool", lambda: _Pool(conn))
    faces = _FakeFaces(MATCH)
    monkeypatch.setattr(FaceRecognitionService, "get_instance", classmethod(lambda cls: faces))
    monkeypatch.setattr(FaceRecognitionService, "_available", True)
    app = FastAPI()
    app.include_router(api_face.router)
    app.include_router(api_auth.router)
    app.include_router(auth.router, prefix="/auth")
    return types.SimpleNamespace(client=TestClient(app), faces=faces, conn=conn)


def _jpeg(seed=0):
    image = np.full((48, 64, 3), seed * 30, np.uint8)
    return cv2.imencode(".jpg", image)[1].tobytes()


def _files(blobs):
    return [("files", (f"frame-{i}.jpg", io.BytesIO(blob), "image/jpeg")) for i, blob in enumerate(blobs)]


def _post(env, route, blobs):
    return env.client.post(route, files=_files(blobs))


def test_login_with_five_frames_issues_a_token(env):
    resp = _post(env, "/api/face/login", [_jpeg(i) for i in range(5)])
    assert resp.status_code == 200
    body = resp.json()
    assert body["identified"] is True and body["u_id"] == 7 and body["token"]
    assert body["reason"] == "match" and body["frames"] == 5
    assert body["name"] == "pearl" and body["distance"] == 0.12 and body["face_count"] == 1
    assert body["error"] is None
    assert len(env.faces.calls) == 1 and len(env.faces.calls[0]) == 5

    resp = _post(env, "/api/auth/face-login", [_jpeg(i) for i in range(5)])
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True and body["token"] and body["user"]["id"] == 7
    assert body["reason"] == "match" and body["frames"] == 5


def test_reason_is_returned_when_not_identified(env):
    env.faces.decision = LoginDecision(False, None, None, "two_faces", 5, face_count=2)
    resp = _post(env, "/api/face/login", [_jpeg(i) for i in range(5)])
    assert resp.status_code == 200
    body = resp.json()
    assert body["identified"] is False and body["reason"] == "two_faces"
    assert body["face_count"] == 2 and body["frames"] == 5
    assert "token" not in body and "u_id" not in body

    for route in ("/api/face/identify", "/auth/face-frame"):
        body = _post(env, route, [_jpeg(i) for i in range(5)]).json()
        assert body["identified"] is False and body["reason"] == "two_faces"
        assert "token" not in body and "login_ticket" not in body

    resp = _post(env, "/api/auth/face-login", [_jpeg(i) for i in range(5)])
    assert resp.status_code == 401
    body = resp.json()
    assert body["success"] is False and body["reason"] == "two_faces" and "token" not in body
    assert env.conn.lookups == []   # nobody is looked up for a face that was not identified


def test_six_frames_is_422(env):
    for route in ROUTES:
        resp = _post(env, route, [_jpeg(i) for i in range(6)])
        assert resp.status_code == 422, route
        assert resp.json()["error"] == "too_many_frames"
    assert env.faces.calls == []


def test_frame_over_two_megabytes_is_413(env):
    big = _jpeg() + b"\0" * (2 * 1024 * 1024)
    for route in ROUTES:
        resp = _post(env, route, [_jpeg(), big])
        assert resp.status_code == 413, route
    assert env.faces.calls == []


def test_one_bad_frame_does_not_spoil_the_login(env):
    blobs = [_jpeg(0), _jpeg(1), b"not an image", _jpeg(2), _jpeg(3)]
    resp = _post(env, "/api/face/login", blobs)
    assert resp.status_code == 200 and resp.json()["identified"] is True
    frames = env.faces.calls[0]
    assert len(frames) == 5
    assert frames[2] is None   # the bad upload is passed on as a frame without a face
    assert all(isinstance(f, np.ndarray) for i, f in enumerate(frames) if i != 2)

    for route in ROUTES:
        resp = _post(env, route, [b"garbage"] * 4 + [b""])
        assert resp.status_code == 400, route
        assert resp.json()["error"] == "Invalid image"
    assert len(env.faces.calls) == 1   # five bad uploads never reach the face service


def test_every_face_route_uses_login(env):
    assert not hasattr(FaceRecognitionService, "identify_frame")
    for n, route in enumerate(ROUTES, start=1):
        resp = _post(env, route, [_jpeg(i) for i in range(5)])
        assert resp.status_code == 200, route
        assert resp.json()["reason"] == "match"
        assert len(env.faces.calls) == n and len(env.faces.calls[-1]) == 5

    # One frame is still accepted (1 to FACE_LOGIN_FRAMES).
    assert _post(env, "/api/face/identify", [_jpeg()]).status_code == 200


def test_model_unavailable_says_so(env, monkeypatch):
    monkeypatch.setattr(FaceRecognitionService, "_available", False)
    env.faces.decision = LoginDecision(False, None, None, "no_face", 5)
    for route in ("/api/face/login", "/api/face/identify", "/auth/face-frame"):
        body = _post(env, route, [_jpeg(i) for i in range(5)]).json()
        assert body["identified"] is False and body["reason"] == "no_face"
        assert body["error"] == "Model not loaded"
    body = _post(env, "/api/auth/face-login", [_jpeg(i) for i in range(5)]).json()
    assert body["error"] == "Model not loaded"


def test_login_log_has_the_account_not_the_name(env, caplog):
    with caplog.at_level(logging.INFO):
        _post(env, "/api/face/login", [_jpeg(i) for i in range(5)])
        _post(env, "/api/auth/face-login", [_jpeg(i) for i in range(5)])
        env.faces.decision = NOBODY
        _post(env, "/api/face/login", [_jpeg(i) for i in range(5)])
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("face login: account=")]
    assert lines == ["face login: account=7 reason=match", "face login: account=7 reason=match",
                     "face login: account=- reason=unknown"]
    assert not any("pearl" in r.getMessage().lower() for r in caplog.records)


def _confirm(env, data):
    return env.client.post("/auth/confirm-login", data=data, follow_redirects=False)


def test_confirm_login_needs_a_ticket(env, monkeypatch):
    # The old form posted a name; a name alone is no longer a login.
    assert _confirm(env, {"name": "pearl"}).status_code == 400
    assert _confirm(env, {}).status_code == 400
    assert _confirm(env, {"ticket": "not-a-ticket"}).status_code == 400
    # A session token is signed with another salt, so it is not a ticket.
    session = URLSafeTimedSerializer(SECRET_KEY).dumps("pearl")
    assert _confirm(env, {"ticket": session}).status_code == 400
    # A ticket older than 60 s is refused.
    signer = URLSafeTimedSerializer(SECRET_KEY, salt="face-login-ticket")
    real = TimestampSigner.get_timestamp
    monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda self: int(time.time()) - 61)
    old = signer.dumps("pearl")
    monkeypatch.setattr(TimestampSigner, "get_timestamp", real)
    resp = _confirm(env, {"ticket": old})
    assert resp.status_code == 400 and "location" not in resp.headers
    assert env.conn.lookups == []

    # /auth/face-frame hands out the ticket when identified; confirm-login turns it into a session.
    body = _post(env, "/auth/face-frame", [_jpeg(i) for i in range(5)]).json()
    assert body["identified"] is True and body["login_ticket"]
    resp = _confirm(env, {"ticket": body["login_ticket"]})
    assert resp.status_code == 303 and resp.headers["location"] == "/display/"
    assert auth.SESSION_COOKIE in resp.cookies
    assert env.conn.lookups == ["pearl"]

    # A recognised face with no account still goes to registration, from the ticket's label.
    resp = _confirm(env, {"ticket": signer.dumps("newface")})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/register?face_label=newface&hint=1"
