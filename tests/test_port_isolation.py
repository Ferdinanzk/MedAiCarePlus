import re
import sys
import types

import pytest

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from fastapi import APIRouter, WebSocket
from fastapi.testclient import TestClient
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocketDisconnect

from app.config import DEVICE_PORT, PUBLIC_PORT
from app.main import app

_probe = APIRouter()


@_probe.get("/api/device/__probe")
async def device_probe():
    return {"ok": True}


@_probe.get("/api/__public_probe")
async def public_probe():
    return {"ok": True}


@_probe.websocket("/api/device/__ws_probe")
async def device_ws_probe(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_json({"port": websocket.scope["server"][1]})
    await websocket.close()


@_probe.websocket("/api/__public_ws_probe")
async def public_ws_probe(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_json({"port": websocket.scope["server"][1]})
    await websocket.close()


# Ahead of the SPA catch-all route, which would otherwise match first.
for _route in reversed(_probe.routes):
    app.router.routes.insert(0, _route)


def _client(port):
    # TestClient derives scope["server"] from base_url, which is what the middleware reads.
    return TestClient(app, base_url=f"http://testserver:{port}")


def test_device_routes_are_hidden_on_public_port():
    assert _client(PUBLIC_PORT).get("/api/device/__probe").status_code == 404


def test_device_routes_are_served_on_device_port():
    response = _client(DEVICE_PORT).get("/api/device/__probe")
    assert response.status_code == 200 and response.json() == {"ok": True}


def test_public_routes_are_not_served_on_device_port():
    assert _client(DEVICE_PORT).get("/api/__public_probe").status_code == 404
    assert _client(DEVICE_PORT).get("/api/legal/current").status_code == 404


def test_device_token_rejected_on_public_port_even_for_public_routes():
    response = _client(PUBLIC_PORT).get("/api/__public_probe", headers={"Authorization": "Bearer rdv1.abc"})
    assert response.status_code == 401


def test_face_token_style_header_passes_public_port():
    response = _client(PUBLIC_PORT).get("/api/__public_probe", headers={"Authorization": "Bearer eyJ1X2lk.x.y"})
    assert response.status_code == 200


# ── WebSockets (spec 2026-10-12-live-voice-2a-design.md A2; the http-only middleware never saw them, flaw F22) ──

def _ws(port, path, headers=None):
    # websocket_connect ignores base_url, so the port goes in the URL (TestClient puts it in scope["server"]).
    return TestClient(app).websocket_connect(f"ws://testserver:{port}{path}", headers=headers or {})


def _refused(port, path, headers=None) -> int:
    """The close code of a WebSocket the app closed before accepting it (uvicorn answers that handshake with 403)."""
    with pytest.raises(WebSocketDisconnect) as refused:
        with _ws(port, path, headers):
            pass
    return refused.value.code


def test_device_websockets_are_refused_on_public_port():
    assert _refused(PUBLIC_PORT, "/api/device/__ws_probe") == 1008


def test_device_websockets_are_served_on_device_port():
    with _ws(DEVICE_PORT, "/api/device/__ws_probe") as websocket:
        assert websocket.receive_json() == {"port": DEVICE_PORT}


def test_public_websockets_are_not_served_on_device_port():
    assert _refused(DEVICE_PORT, "/api/__public_ws_probe") == 1008


def test_device_token_websocket_rejected_on_public_port():
    assert _refused(PUBLIC_PORT, "/api/__public_ws_probe", {"Authorization": "Bearer rdv1.abc"}) == 1008
    with _ws(PUBLIC_PORT, "/api/__public_ws_probe", {"Authorization": "Bearer eyJ1X2lk.x.y"}) as websocket:
        assert websocket.receive_json() == {"port": PUBLIC_PORT}


def test_every_websocket_route_is_refused_on_the_wrong_port():
    """Walks every WebSocket route of the app: a device route never opens on the public port, with or without a
    device token, and every other WebSocket never opens on the device port."""
    paths = {re.sub(r"\{[^}]*\}", "x", route.path) for route in app.routes if isinstance(route, WebSocketRoute)}
    assert {"/api/device/conversations/live", "/emotion/ws/emotion"} <= paths
    for path in sorted(paths):
        if path.startswith("/api/device/"):
            assert _refused(PUBLIC_PORT, path) == 1008, path
            assert _refused(PUBLIC_PORT, path, {"Authorization": "Bearer rdv1.abc"}) == 1008, path
        else:
            assert _refused(DEVICE_PORT, path) == 1008, path
