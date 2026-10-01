import asyncio
import base64
import hashlib
import hmac
import json
import sys
import types

import pytest
from starlette.requests import Request

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app.routers import api_notify


def _request(body, signature=None):
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    headers = [(b"x-line-signature", signature.encode("utf-8"))] if signature is not None else []
    return Request({"type": "http", "headers": headers}, receive)


@pytest.mark.parametrize("endpoint", [api_notify.line_webhook, api_notify.line_webhook_public])
@pytest.mark.parametrize("secret, signature, error", [
    ("", None, "LINE channel secret not configured"),
    ("", "any", "LINE channel secret not configured"),
    ("configured", None, "Invalid signature"),
    ("configured", "wrong", "Invalid signature"),
    ("configured", "é", "Invalid signature"),
])
def test_webhook_rejects_unsigned_before_parsing_or_db(monkeypatch, endpoint, secret, signature, error):
    monkeypatch.setattr(api_notify, "LINE_CHANNEL_SECRET", secret)

    def unexpected():
        raise AssertionError("Webhook must reject before accessing DB")

    monkeypatch.setattr(api_notify, "get_pool", unexpected)
    response = asyncio.run(endpoint(_request(b"not-json", signature)))
    assert response.status_code == 401
    assert json.loads(response.body) == {"error": error}


def test_valid_signature_processes_verification_code(monkeypatch):
    calls = []

    class Connection:
        async def fetchrow(self, query, *args):
            if "FROM family_contacts" in query:
                assert args == ("ABCD",)
                return {"id": 3, "u_id": 7, "name": "Family", "relationship": "child"}
            return {"name": "Pearl", "line_id": None}

        async def execute(self, query, *args):
            calls.append(("verified", args))

    class Acquire:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *args):
            return False

    pool = types.SimpleNamespace(acquire=lambda: Acquire())
    service = types.SimpleNamespace(send_verification_success=lambda *args: calls.append(("sent", args)))
    monkeypatch.setattr(api_notify, "get_pool", lambda: pool)
    monkeypatch.setattr(api_notify.LineService, "get_instance", classmethod(lambda cls: service))
    monkeypatch.setattr(api_notify, "LINE_CHANNEL_SECRET", "secret")
    body = json.dumps({"events": [{"type": "message", "message": {"type": "text", "text": "abcd"},
                                   "source": {"userId": "line-family"}}]}).encode()
    signature = base64.b64encode(hmac.new(b"secret", body, hashlib.sha256).digest()).decode()
    result = asyncio.run(api_notify.line_webhook(_request(body, signature)))
    assert result == {"status": "ok"}
    assert calls == [("verified", ("line-family", 3)), ("sent", ("line-family", "Pearl"))]
