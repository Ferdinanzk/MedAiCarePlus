"""Answers to family's buttons and verification codes go as free LINE replies, push as the fallback."""

import datetime
import os
import sys
import types

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

sys.path.insert(0, os.path.dirname(__file__))
from test_dose_due_rule import _incident_db, clock, run, taipei  # noqa: E402,F401

from app.services import dose_confirmation, line_pause, line_service  # noqa: E402
from app.services.line_service import LineService  # noqa: E402


class _Resp:
    def __init__(self, status, headers=None, text=""):
        self.status_code = status
        self.headers = headers or {}
        self.text = text


def _service(monkeypatch):
    service = LineService()
    monkeypatch.setattr(LineService, "_available", True)   # the constructor reads the (empty) test token
    return service


def test_reply_messages_posts_to_reply_endpoint(monkeypatch):
    service = _service(monkeypatch)
    seen = {}

    def post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, body=json)
        return _Resp(200, {"x-line-request-id": "r1"})

    monkeypatch.setattr(line_service.requests, "post", post)
    messages = [{"type": "text", "text": "hi"}]
    result = service.reply_messages("SECRETTOK", messages)
    assert seen["url"] == "https://api.line.me/v2/bot/message/reply"
    assert set(seen["body"]) == {"replyToken", "messages"} and seen["body"]["replyToken"] == "SECRETTOK"
    assert result["status"] == "accepted" and result["request_id"] == "r1"

    monkeypatch.setattr(line_service.requests, "post", lambda *a, **k: _Resp(400, text="Invalid reply token"))
    bad = service.reply_messages("SECRETTOK", messages)
    assert bad["status"] == "invalid_token" and "SECRETTOK" not in str(bad)
    monkeypatch.setattr(line_service.requests, "post", lambda *a, **k: _Resp(500))
    assert service.reply_messages("SECRETTOK", messages)["status"] == "failed"

    def boom(*a, **k):
        raise line_service.requests.ConnectionError("https://x?replyToken=SECRETTOK")

    monkeypatch.setattr(line_service.requests, "post", boom)
    failed = service.reply_messages("SECRETTOK", messages)
    assert failed["status"] == "failed" and "SECRETTOK" not in str(failed)
    assert service.reply_messages("SECRETTOK", [messages[0]] * 6)["status"] == "failed"   # LINE takes at most 5


def test_reply_text_falls_back_to_push_without_token(monkeypatch):
    service = _service(monkeypatch)
    pushed, replied = [], []
    monkeypatch.setattr(service, "send_text", lambda to, message, *, kind: pushed.append((to, message, kind))
                        or {"sent": True})
    monkeypatch.setattr(service, "reply_messages",
                        lambda token, messages: replied.append(token) or {"status": "invalid_token"})
    service.reply_text(None, "U1", "hello")
    assert replied == [] and pushed == [("U1", "hello", line_pause.OTHER)]
    service.reply_text("tok", "U1", "again")           # a rejected token falls back too
    assert replied == ["tok"] and len(pushed) == 2
    monkeypatch.setattr(service, "reply_messages", lambda token, messages: {"status": "accepted"})
    assert service.reply_text("tok", "U1", "third")["sent"] is True and len(pushed) == 2


def test_verification_success_text_is_the_sent_text(monkeypatch):
    service = _service(monkeypatch)
    sent = []
    monkeypatch.setattr(service, "send_text", lambda to, message, *, kind: sent.append(message) or {})
    service.send_verification_success("U1", "Pearl")
    assert sent == [line_service.verification_success_text("Pearl")]
    assert "Pearl" in sent[0]


def test_postback_ack_carries_reply_token_others_do_not(clock):
    db = _incident_db()
    confirmation_id = "11111111-1111-4111-8111-111111111111"
    db.intakes[5]["intake_stats"] = "pending_confirmation"
    db.confirmations[confirmation_id] = {
        "confirmation_id": confirmation_id, "u_id": 7, "task_id": None, "intk_ids": [5],
        "previous_status": {"5": "pending"}, "source": "patient_claim", "evidence": None,
        "created_at": taipei(3, 0, 5).astimezone(datetime.timezone.utc), "reminded_at": None,
        "resolved_at": None, "resolution": None, "resolved_by": None}
    data = dose_confirmation.sign_postback(dose_confirmation.ACTION, confirmation_id, "taken", 1)
    received = datetime.datetime(2026, 10, 3, 0, 6, tzinfo=datetime.timezone.utc)
    run(dose_confirmation.handle_postback(db, data, "U-amy", "tok-1", received_at=received))
    (ack,) = [r for r in db.outbox if r["dedupe_key"].startswith("dose_confirm_ack:")]
    assert ack["payload"]["reply_token"] == "tok-1"
    assert ack["payload"]["reply_until"] == (received + datetime.timedelta(seconds=50)).isoformat()
    others = [r for r in db.outbox if r["dedupe_key"].startswith("dose_confirm_answered:")]
    assert others and all("reply_token" not in r["payload"] and "reply_until" not in r["payload"] for r in others)


# ── The reply token is used only in a one-to-one chat ────────────────────────

def _webhook(monkeypatch, events, *, row=None):
    import asyncio
    import base64
    import hashlib
    import hmac
    import json
    from starlette.requests import Request
    from app.routers import api_notify

    calls = []

    class Connection:
        async def fetchrow(self, query, *args):
            if "FROM family_contacts" in query:
                return row
            return {"name": "Pearl", "line_id": None}

        async def execute(self, query, *args):
            pass

    class Acquire:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *args):
            return False

    service = LineService()
    monkeypatch.setattr(LineService, "_available", True)
    monkeypatch.setattr(service, "reply_messages",
                        lambda token, messages: calls.append(("reply", token)) or {"status": "accepted"})
    monkeypatch.setattr(service, "send_text",
                        lambda to, message, *, kind: calls.append(("push", to)) or {"sent": True})

    async def handle_postback(conn, data, sender, reply_token=None, received_at=None):
        calls.append(("postback", reply_token))

    monkeypatch.setattr(api_notify, "get_pool", lambda: types.SimpleNamespace(acquire=lambda: Acquire()))
    monkeypatch.setattr(api_notify.LineService, "get_instance", classmethod(lambda cls: service))
    monkeypatch.setattr(api_notify.dose_confirmation, "handle_postback", handle_postback)
    monkeypatch.setattr(api_notify, "LINE_CHANNEL_SECRET", "secret")
    body = json.dumps({"events": events}).encode()
    signature = base64.b64encode(hmac.new(b"secret", body, hashlib.sha256).digest()).decode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request({"type": "http", "headers": [(b"x-line-signature", signature.encode())]}, receive)
    asyncio.run(api_notify.line_webhook(request))
    return calls


def _text(source_type):
    return {"type": "message", "message": {"type": "text", "text": "nope"}, "replyToken": "RT",
            "source": {"type": source_type, "userId": "U-x"}}


def test_text_in_a_group_is_answered_by_push_not_reply(monkeypatch):
    assert _webhook(monkeypatch, [_text("group")]) == [("push", "U-x")]


def test_postback_in_a_room_gets_no_reply_token(monkeypatch):
    event = {"type": "postback", "postback": {"data": "d"}, "replyToken": "RT",
             "source": {"type": "room", "userId": "U-x", "roomId": "R"}}
    assert _webhook(monkeypatch, [event]) == [("postback", None)]
    event["source"] = {"userId": "U-x"}                     # no source type at all
    assert _webhook(monkeypatch, [event]) == [("postback", None)]


def test_one_to_one_chat_still_uses_the_token(monkeypatch):
    assert _webhook(monkeypatch, [_text("user")]) == [("reply", "RT")]
    event = {"type": "postback", "postback": {"data": "d"}, "replyToken": "RT",
             "source": {"type": "user", "userId": "U-x"}}
    assert _webhook(monkeypatch, [event]) == [("postback", "RT")]
