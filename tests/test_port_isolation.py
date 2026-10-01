import sys
import types

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from fastapi import APIRouter
from fastapi.testclient import TestClient

from app.config import DEVICE_PORT, PUBLIC_PORT
from app.main import app

_probe = APIRouter()


@_probe.get("/api/device/__probe")
async def device_probe():
    return {"ok": True}


@_probe.get("/api/__public_probe")
async def public_probe():
    return {"ok": True}


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
