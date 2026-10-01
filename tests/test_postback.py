import asyncio
import base64
import hashlib
import hmac
import json
import os
import sys
import types
import uuid

import pytest
from starlette.requests import Request

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

sys.path.insert(0, os.path.dirname(__file__))
from test_dose_confirmation import FakeDB, FakePool  # noqa: E402

from app.routers import api_notify  # noqa: E402
from app.services import dose_confirmation  # noqa: E402

SECRET = "channel-secret"


def _request(body, signature):
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({"type": "http", "headers": [(b"x-line-signature", signature.encode())]}, receive)


def _post(events, secret=SECRET):
    body = json.dumps({"events": events}).encode()
    signature = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    return asyncio.run(api_notify.line_webhook(_request(body, signature)))


def _postback(data, user_id):
    return {"type": "postback", "postback": {"data": data}, "source": {"type": "user", "userId": user_id},
            "replyToken": "r"}


@pytest.fixture
def db(monkeypatch):
    database = FakeDB()
    monkeypatch.setattr(api_notify, "LINE_CHANNEL_SECRET", SECRET)
    monkeypatch.setattr(api_notify, "get_pool", lambda: FakePool(database))
    sent = []
    service = types.SimpleNamespace(send_text=lambda *args: sent.append(args),
                                    send_verification_success=lambda *args: sent.append(args))
    monkeypatch.setattr(api_notify.LineService, "get_instance", classmethod(lambda cls: service))
    database.direct_sends = sent
    return database


def _confirmation(db):
    return asyncio.run(dose_confirmation.create(db, u_id=7, task_id=None, intk_ids=[100, 101],
                                                source="degraded", evidence=None))


def _button(db, confirmation_id, contact_id, answer):
    entry = next(r for r in db.outbox if r["dedupe_key"] == f"dose_confirm:{confirmation_id}:{contact_id}")
    template = next(m for m in entry["payload"]["messages"] if m["type"] == "template")
    index = 0 if answer == "taken" else 1
    return template["template"]["actions"][index]["data"]


def _snapshot(db):
    return json.dumps({"intakes": db.intakes, "meds": db.meds, "confirmations": db.confirmations,
                       "outbox": db.outbox}, default=str, sort_keys=True)


def test_valid_taken_postback_records_dose_and_replies_through_outbox(db):
    confirmation_id = _confirmation(db)
    before = len(db.outbox)

    assert _post([_postback(_button(db, confirmation_id, 1, "taken"), "U-amy")]) == {"status": "ok"}

    assert db.intakes[100]["intake_stats"] == "taken"
    assert db.intakes[100]["detection_method"] == "caregiver_confirmed"
    assert db.meds[40]["pills_remaining"] == 4
    assert db.confirmations[confirmation_id]["resolution"] == "confirmed"
    assert db.confirmations[confirmation_id]["resolved_by"] == 1
    replies = {r["dedupe_key"]: r for r in db.outbox[before:]}
    assert set(replies) == {f"dose_confirm_ack:{confirmation_id}:1", f"dose_confirm_answered:{confirmation_id}:2"}
    ack = replies[f"dose_confirm_ack:{confirmation_id}:1"]
    assert ack["recipient_line_id"] == "U-amy"
    assert "Recorded" in ack["payload"]["messages"][0]["text"]
    notice = replies[f"dose_confirm_answered:{confirmation_id}:2"]
    assert notice["recipient_line_id"] == "U-ben"
    assert "Amy answered: taken" in notice["payload"]["messages"][0]["text"]
    assert db.direct_sends == []     # nothing bypasses the outbox


def test_valid_not_taken_postback_restores_previous_status(db):
    confirmation_id = _confirmation(db)
    _post([_postback(_button(db, confirmation_id, 2, "not_taken"), "U-ben")])
    assert db.intakes[100]["intake_stats"] == "pending"
    assert db.intakes[101]["intake_stats"] == "missed"
    assert db.confirmations[confirmation_id]["resolution"] == "denied"
    assert any("Ben answered: not taken" in r["payload"]["messages"][0]["text"] for r in db.outbox
               if r["dedupe_key"] == f"dose_confirm_answered:{confirmation_id}:1")


def _forged_cases(confirmation_id):
    good = dose_confirmation.sign_postback("dose_confirm", confirmation_id, "taken", 1)
    return {
        "bad_hmac": (good[:-4] + "0000", "U-amy"),
        "answer_swapped": (good.replace("a=taken", "a=not_taken"), "U-amy"),
        "contact_swapped_to_ben_from_amy_line": (good.replace("c=1&", "c=2&"), "U-amy"),
        "other_secret": (None, "U-amy"),
        "sender_not_contact": (good, "U-stranger"),
        "sender_is_other_verified_contact": (good, "U-ben"),
        "no_sender": (good, ""),
        "unverified_contact": (dose_confirmation.sign_postback("dose_confirm", confirmation_id, "taken", 4), "U-unv"),
        "patient_self": (dose_confirmation.sign_postback("dose_confirm", confirmation_id, "taken", 5), "U-pearl"),
        "contact_of_other_patient": (
            dose_confirmation.sign_postback("dose_confirm", confirmation_id, "taken", 6), "U-stranger"),
        "unknown_confirmation": (
            dose_confirmation.sign_postback("dose_confirm", str(uuid.uuid4()), "taken", 1), "U-amy"),
        "unknown_contact": (dose_confirmation.sign_postback("dose_confirm", confirmation_id, "taken", 99), "U-amy"),
        "garbage": ("hello", "U-amy"),
    }


@pytest.mark.parametrize("case", sorted(_forged_cases(str(uuid.UUID(int=0)))))
def test_forged_or_unauthorised_postback_changes_nothing(db, monkeypatch, case):
    """Review focus 4: bad HMAC or a sender who isn't that patient's verified contact → no state change."""
    confirmation_id = _confirmation(db)
    data, sender = _forged_cases(confirmation_id)[case]
    if data is None:
        message = f"dose_confirm|{confirmation_id}|taken|1".encode()
        sig = hmac.new(b"attacker-secret", message, hashlib.sha256).hexdigest()[:16]
        data = f"action=dose_confirm&id={confirmation_id}&a=taken&c=1&s={sig}"
    before = _snapshot(db)

    assert _post([_postback(data, sender)]) == {"status": "ok"}

    assert _snapshot(db) == before
    assert db.direct_sends == []


def test_second_answer_and_replay_change_nothing_and_get_already_answered(db):
    """Review focus 4: the first valid answer wins; a second answer or a replay only gets a notice."""
    confirmation_id = _confirmation(db)
    first = _button(db, confirmation_id, 1, "taken")
    _post([_postback(first, "U-amy")])
    state = json.dumps({"intakes": db.intakes, "meds": db.meds, "confirmations": db.confirmations},
                       default=str, sort_keys=True)

    _post([_postback(_button(db, confirmation_id, 2, "not_taken"), "U-ben")])
    _post([_postback(first, "U-amy")])      # replay of the accepted answer
    _post([_postback(_button(db, confirmation_id, 2, "not_taken"), "U-ben")])

    assert json.dumps({"intakes": db.intakes, "meds": db.meds, "confirmations": db.confirmations},
                      default=str, sort_keys=True) == state
    assert db.meds[40]["pills_remaining"] == 4
    late = {r["dedupe_key"]: r for r in db.outbox if r["dedupe_key"].startswith("dose_confirm_late:")}
    assert set(late) == {f"dose_confirm_late:{confirmation_id}:2", f"dose_confirm_late:{confirmation_id}:1"}
    assert "Already answered by Amy" in late[f"dose_confirm_late:{confirmation_id}:2"]["payload"]["messages"][0]["text"]


def test_postback_to_expired_confirmation_reports_expiry(db):
    confirmation_id = _confirmation(db)
    db.confirmations[confirmation_id]["resolution"] = "expired"
    _post([_postback(_button(db, confirmation_id, 1, "taken"), "U-amy")])
    assert db.intakes[100]["intake_stats"] == "pending_confirmation"
    late = next(r for r in db.outbox if r["dedupe_key"] == f"dose_confirm_late:{confirmation_id}:1")
    assert "expired" in late["payload"]["messages"][0]["text"]


def test_postback_and_message_events_in_one_delivery_are_all_processed(db, monkeypatch):
    confirmation_id = _confirmation(db)
    original_fetchrow = db.fetchrow

    async def fetchrow(query, *args):
        if "WHERE verification_code" in query:
            return None
        return await original_fetchrow(query, *args)

    monkeypatch.setattr(db, "fetchrow", fetchrow)
    events = [_postback("forged", "U-amy"),
              _postback(_button(db, confirmation_id, 1, "taken"), "U-amy"),
              {"type": "message", "message": {"type": "text", "text": "nope"}, "source": {"userId": "U-x"}}]
    assert _post(events) == {"status": "ok"}
    assert db.confirmations[confirmation_id]["resolution"] == "confirmed"
    assert db.direct_sends == [("U-x", "驗證碼無效，請確認後重新輸入。/ Invalid verification code.")]


def test_postback_handler_error_does_not_break_other_events(db, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(dose_confirmation, "handle_postback", boom)
    confirmation_id = _confirmation(db)
    assert _post([_postback(_button(db, confirmation_id, 1, "taken"), "U-amy")]) == {"status": "ok"}
    assert db.confirmations[confirmation_id]["resolution"] is None


def test_unsigned_postback_is_rejected_before_db(monkeypatch):
    monkeypatch.setattr(api_notify, "LINE_CHANNEL_SECRET", SECRET)

    def unexpected():
        raise AssertionError("must not touch the DB")

    monkeypatch.setattr(api_notify, "get_pool", unexpected)
    body = json.dumps({"events": [_postback("x", "U-amy")]}).encode()
    response = asyncio.run(api_notify.line_webhook(_request(body, "bad")))
    assert response.status_code == 401
