"""Task 3 of docs/superpowers/plans/2026-11-08-line-medicine-photo.md: a photo sent to the LINE account is checked
(who sent it, consent, the hourly limit), read in the background and answered with the list and a Save all button."""

import asyncio
import base64
import datetime
import hashlib
import hmac
import json
import logging
import sys
import threading
import time
import types
import uuid

import pytest
from starlette.requests import Request

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app.routers import api_notify  # noqa: E402
from app.services import dose_confirmation, line_photo, outbox_dispatcher  # noqa: E402
from app.services.ocr_service import OCRServiceError  # noqa: E402

UTC = datetime.timezone.utc
TODAY = datetime.date(2026, 10, 4)
PATIENT = "U-patient-4711"
TOKEN = "RT-token-0815"
MESSAGE = "MSG-90210"
MAIN_THREAD = threading.get_ident()


# ── fakes ────────────────────────────────────────────────────────────────────

class FakeDB:
    def __init__(self):
        self.users = [{"u_id": 7, "line_id": PATIENT, "user_active": True}]
        self.contacts = []
        self.drafts = {}
        self.meds = []
        self.outbox = []
        self.consent = {7: True}
        self.transactions = 0

    async def fetch(self, query, *args):
        if 'FROM "user"' in query:
            assert "line_id = $1" in query and "user_active = TRUE" in query
            return [{"u_id": u["u_id"]} for u in self.users if u["line_id"] == args[0] and u["user_active"]]
        if "FROM medication" in query:
            assert "u_id = $1" in query and "is_active IS NOT FALSE" in query
            return [{"med_name": m["med_name"]} for m in self.meds
                    if m["u_id"] == args[0] and m.get("is_active") is not False]
        raise AssertionError(query)

    async def fetchval(self, query, *args):
        if "FROM family_contacts" in query:
            assert "relationship IS DISTINCT FROM 'user'" in query
            return 1 if any(c["line_id"] == args[0] and c["relationship"] != "user" for c in self.contacts) else None
        if "INSERT INTO line_photo_draft" in query:
            assert "ON CONFLICT (line_message_id) DO NOTHING" in query
            draft_id, u_id, message_id = args
            if any(d["line_message_id"] == message_id for d in self.drafts.values()):
                return None
            self.drafts[draft_id] = {"draft_id": draft_id, "u_id": u_id, "line_message_id": message_id,
                                     "status": "reading", "result": None, "error_code": None,
                                     "created_at": datetime.datetime.now(UTC)}
            return draft_id
        if "FROM line_photo_draft" in query:
            assert "created_at > NOW() - INTERVAL '1 hour'" in query
            since = datetime.datetime.now(UTC) - datetime.timedelta(hours=1)
            return sum(1 for d in self.drafts.values() if d["u_id"] == args[0] and d["created_at"] > since)
        if "INSERT INTO notification_outbox" in query:
            dedupe_key, u_id, contact_id, line_id, kind, priority, payload = args
            if any(r["dedupe_key"] == dedupe_key for r in self.outbox):
                return None
            self.outbox.append({"dedupe_key": dedupe_key, "u_id": u_id, "recipient_contact_id": contact_id,
                                "recipient_line_id": line_id, "kind": kind, "priority": priority,
                                "payload": json.loads(payload)})
            return len(self.outbox)
        raise AssertionError(query)

    async def execute(self, query, *args):
        assert "UPDATE line_photo_draft" in query and "WHERE draft_id = $1" in query, query
        draft = self.drafts[args[0]]
        if "result = $2::jsonb" in query:
            draft.update(status="pending", result=json.loads(args[1]))
        else:
            draft.update(status=args[1], error_code=args[2])

    def transaction(self):
        db = self

        class Transaction:
            async def __aenter__(self):
                db.transactions += 1

            async def __aexit__(self, *exc):
                return False

        return Transaction()


class FakePool:
    def __init__(self, db):
        self.db = db

    def acquire(self):
        db = self.db

        class Acquire:
            async def __aenter__(self):
                return db

            async def __aexit__(self, *exc):
                return False

        return Acquire()


class FakeOCR:
    gemini_api_key = "key"

    def __init__(self, result=None, error=None, delay=0.0):
        self.result, self.error, self.delay = result, error, delay
        self.calls = []

    def process_image(self, image_bytes):
        self.calls.append((image_bytes, threading.get_ident()))
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise self.error
        return self.result


@pytest.fixture
def env(monkeypatch):
    db = FakeDB()
    calls = []
    spawned = []
    wakes = []
    content = {"answer": ("ok", b"jpeg-bytes")}

    def reply_text(token, to, text):
        calls.append(("reply_text", token, to, text, threading.get_ident()))
        return {"sent": True}

    def start_loading(chat_id, seconds=60):
        calls.append(("start_loading", chat_id, seconds, threading.get_ident()))
        return True

    def get_content(message_id, max_bytes):
        calls.append(("get_content", message_id, max_bytes, threading.get_ident()))
        return content["answer"]

    service = types.SimpleNamespace(reply_text=reply_text, start_loading=start_loading, get_content=get_content)

    async def get_state(u_id):
        return {"core": {"granted": db.consent.get(u_id, False), "terms_version": line_photo.consent_service.config
                         .TERMS_VERSION}}

    monkeypatch.setattr(line_photo, "get_pool", lambda: FakePool(db))
    monkeypatch.setattr(line_photo.LineService, "get_instance", classmethod(lambda cls: service))
    monkeypatch.setattr(line_photo.consent_service, "get_state", get_state)
    monkeypatch.setattr(line_photo, "_spawn", spawned.append)
    monkeypatch.setattr(line_photo.outbox, "wake", lambda: wakes.append(1))
    monkeypatch.setattr(line_photo.memory, "local_today", lambda: TODAY)
    ns = types.SimpleNamespace(db=db, calls=calls, spawned=spawned, wakes=wakes, content=content,
                               ocr=FakeOCR(result=_result(_med("PANADOL"))))
    monkeypatch.setattr(line_photo.OCRService, "get_instance", classmethod(lambda cls: ns.ocr))
    yield ns
    for coro in spawned:
        coro.close()


def _image(message_id=MESSAGE, source="user", provider="line", user=PATIENT):
    source_info = {"type": source, "userId": user}
    if source == "group":
        source_info["groupId"] = "G1"
    if source == "room":
        source_info["roomId"] = "R1"
    return {"type": "message", "message": {"type": "image", "id": message_id, "contentProvider": {"type": provider}},
            "source": source_info, "replyToken": TOKEN}


def _med(name, **changes):
    med = {"med_name": name, "dosage": "500mg", "quantity": "15 錠", "pill_count": 15, "unit": "錠",
           "frequency_text": "一天3次", "instructions": "一天3次", "times_per_day": 3, "max_per_day": None,
           "interval_hours": None, "days": 5, "amount_each_intake": "1錠", "units_per_dose": 1.0, "form": "tablet",
           "dose_form": "solid_oral", "as_needed": False,
           "schedule_time": {"morning": True, "noon": True, "night": True, "bedtime": False,
                             "before_meals": False, "after_meals": True},
           "schedule_source": "text", "stock": 15, "total_intake": "N/A", "intake_time_label": "N/A",
           "warning": "N/A", "pill_description": "N/A", "clinical_uses": "N/A", "manufacturer": "N/A"}
    med.update(changes)
    return med


def _result(*meds, **paper):
    result = {key: meds[0].get(key, "N/A") for key in ("med_name", "dosage", "quantity", "amount_each_intake")}
    result.update({"hospital": "N/A", "prescription_no": "N/A", "use_before": "N/A", "physician": "N/A",
                   "pharmacist": "N/A", "patient_name": "N/A", "date_dispensed": "N/A", "pharmacy": "N/A",
                   "visit_date": "N/A", "document_type": "medicine_bag", "medications": list(meds)})
    result.update(paper)
    return result


def _receive(event, received_at=None):
    return asyncio.run(line_photo.receive(event, received_at or datetime.datetime.now(UTC)))


def _read(env, received_at=None, event=None):
    """A photo through receive, then its background task run to the end."""
    received_at = received_at or datetime.datetime.now(UTC)
    assert _receive(event or _image(), received_at) == "reading"
    assert len(env.spawned) == 1
    return asyncio.run(env.spawned.pop())


def _only_draft(env):
    assert len(env.db.drafts) == 1
    return next(iter(env.db.drafts.values()))


def _texts(row):
    return [m["text"] for m in row["payload"]["messages"] if m["type"] == "text"]


# ── who sent it ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("source", ["group", "room"])
def test_group_and_room_photos_are_ignored(env, monkeypatch, source):
    def no_db():
        raise AssertionError("a group photo must not reach the database")

    monkeypatch.setattr(line_photo, "get_pool", no_db)
    assert _receive(_image(source=source)) == "ignored"
    assert env.calls == [] and env.spawned == [] and env.wakes == []


def test_unlinked_sender_is_told_to_link(env):
    env.db.contacts.append({"line_id": "U-stranger", "relationship": "user"})   # a patient row's own contact only
    assert _receive(_image(user="U-stranger")) == "not_linked"
    (call,) = env.calls
    assert call[:4] == ("reply_text", TOKEN, "U-stranger", line_photo.NOT_LINKED_TEXT)
    assert call[4] != MAIN_THREAD
    assert env.db.drafts == {} and env.db.outbox == [] and env.spawned == []


def test_two_patients_on_one_line_id_count_as_unlinked(env):
    env.db.users.append({"u_id": 8, "line_id": PATIENT, "user_active": True})
    assert _receive(_image()) == "not_linked"
    assert [c[3] for c in env.calls] == [line_photo.NOT_LINKED_TEXT]
    assert env.db.drafts == {} and env.spawned == []


def test_an_inactive_second_account_does_not_hide_the_patient(env):
    env.db.users.append({"u_id": 8, "line_id": PATIENT, "user_active": False})
    assert _receive(_image()) == "reading"
    assert _only_draft(env)["u_id"] == 7


def test_family_sender_is_refused(env):
    env.db.contacts.append({"line_id": "U-family", "relationship": "child"})
    assert _receive(_image(user="U-family")) == "family"
    assert [c[:4] for c in env.calls] == [("reply_text", TOKEN, "U-family", line_photo.FAMILY_TEXT)]
    assert env.db.drafts == {} and env.db.outbox == [] and env.spawned == []


def test_patient_without_current_consent_is_refused_without_download(env):
    env.db.consent[7] = False
    received = datetime.datetime.now(UTC)
    assert _receive(_image(), received) == "consent"
    (row,) = env.db.outbox
    assert row["dedupe_key"] == f"line_photo:msg:{MESSAGE}:consent"
    assert (row["kind"], row["priority"], row["u_id"], row["recipient_line_id"]) == ("line_photo", 1, 7, PATIENT)
    assert _texts(row) == [line_photo.CONSENT_TEXT]
    assert row["payload"]["reply_token"] == TOKEN
    assert row["payload"]["reply_until"] == (received + datetime.timedelta(seconds=50)).isoformat()
    assert env.wakes == [1]
    assert env.calls == [] and env.db.drafts == {} and env.spawned == []


# ── one row per photo, the hourly limit ──────────────────────────────────────

def test_redelivered_photo_is_read_once(env):
    assert _receive(_image()) == "reading"
    assert _receive(_image()) == "duplicate"
    assert len(env.db.drafts) == 1 and len(env.spawned) == 1
    assert env.db.outbox == [] and env.calls == []


def test_eleventh_photo_in_an_hour_is_refused(env):
    for n in range(10):
        assert _receive(_image(message_id=f"m{n}")) == "reading"
    assert _receive(_image(message_id="m10")) == "too_many"
    refused = next(d for d in env.db.drafts.values() if d["line_message_id"] == "m10")
    assert (refused["status"], refused["error_code"]) == ("refused", "too_many")
    (row,) = env.db.outbox
    assert row["dedupe_key"] == f"line_photo:{refused['draft_id']}:too_many"
    assert _texts(row) == [line_photo.TOO_MANY_TEXT] and row["payload"]["reply_token"] == TOKEN
    assert len(env.spawned) == 10

    # An hour later the ten photos no longer count; the refused one still does.
    for draft in env.db.drafts.values():
        if draft["status"] == "reading":
            draft["created_at"] -= datetime.timedelta(hours=2)
    for n in range(9):
        assert _receive(_image(message_id=f"later{n}")) == "reading"
    assert _receive(_image(message_id="later9")) == "too_many"


def test_external_photo_is_not_downloaded(env):
    assert _receive(_image(provider="external")) == "external"
    draft = _only_draft(env)
    assert (draft["status"], draft["error_code"]) == ("failed", "external")
    (row,) = env.db.outbox
    assert row["dedupe_key"] == f"line_photo:{draft['draft_id']}:external"
    assert _texts(row) == [line_photo.UNREADABLE_TEXT]
    assert env.calls == [] and env.spawned == []


# ── reading it ───────────────────────────────────────────────────────────────

def _failed_with(env, outcome, code, text):
    draft = _only_draft(env)
    assert (draft["status"], draft["error_code"], draft["result"]) == ("failed", code, None)
    (row,) = env.db.outbox
    assert row["dedupe_key"] == f"line_photo:{draft['draft_id']}:{outcome}"
    assert (row["kind"], row["priority"], row["recipient_line_id"]) == ("line_photo", 1, PATIENT)
    assert _texts(row) == [text] and row["payload"]["reply_token"] == TOKEN
    assert env.wakes == [1]


def test_too_big_photo(env):
    env.content["answer"] = ("too_large", None)
    assert _read(env) == "too_large"
    _failed_with(env, "too_large", "too_large", line_photo.TOO_BIG_TEXT)
    assert env.ocr.calls == []
    assert [c[:3] for c in env.calls if c[0] == "get_content"] == [("get_content", MESSAGE, 10 * 1024 * 1024)]


def test_download_failure_is_busy(env):
    env.content["answer"] = ("failed", None)
    assert _read(env) == "download_failed"
    _failed_with(env, "download_failed", "download_failed", line_photo.BUSY_TEXT)
    assert env.ocr.calls == []


@pytest.mark.parametrize("code", ["invalid_image", "ocr_no_prescription_text", "ocr_incomplete_schedule"])
def test_unreadable_photo_codes(env, code):
    env.ocr = FakeOCR(error=OCRServiceError(code, "message", 422))
    assert _read(env) == "unreadable"
    _failed_with(env, "unreadable", code, line_photo.UNREADABLE_TEXT)


@pytest.mark.parametrize("error, code", [
    (OCRServiceError("ocr_provider_busy", "busy", 503), "ocr_provider_busy"),
    (OCRServiceError("ocr_unavailable", "off", 503), "ocr_unavailable"),
    (OCRServiceError("ocr_provider_timeout", "late", 504), "ocr_provider_timeout"),
    (RuntimeError("boom"), "ocr_error"),
    (None, "ocr_timeout"),
])
def test_reader_errors_and_timeout_are_busy(env, monkeypatch, error, code):
    if error is None:
        monkeypatch.setattr(line_photo, "OCR_GEMINI_IMAGE_BUDGET", 0.05)
        env.ocr = FakeOCR(result=_result(_med("PANADOL")), delay=0.3)
    else:
        env.ocr = FakeOCR(error=error)
    assert _read(env) == "busy"
    _failed_with(env, "busy", code, line_photo.BUSY_TEXT)


def test_good_read_stores_the_result_and_answers_with_the_list_and_button(env):
    result = _result(_med("PANADOL"), _med("IBU", amount_each_intake="N/A", units_per_dose=2.0, as_needed=True,
                                           days=None, times_per_day=None, max_per_day=4,
                                           schedule_time={s: False for s in line_photo.scan_rules.SLOTS}))
    env.ocr = FakeOCR(result=result)
    env.db.meds.append({"u_id": 7, "med_name": "IBU", "is_active": False})   # stopped: not a duplicate
    env.db.meds.append({"u_id": 8, "med_name": "PANADOL"})                    # another patient's
    received = datetime.datetime.now(UTC)
    assert _read(env, received) == "read"

    draft = _only_draft(env)
    assert (draft["status"], draft["result"]) == ("pending", result)
    assert env.ocr.calls[0][0] == b"jpeg-bytes"
    (row,) = env.db.outbox
    assert row["dedupe_key"] == f"line_photo:{draft['draft_id']}:read"
    assert (row["kind"], row["priority"], row["u_id"], row["recipient_line_id"], row["recipient_contact_id"]) == \
        ("line_photo", 1, 7, PATIENT, None)
    assert row["payload"]["reply_token"] == TOKEN
    assert row["payload"]["reply_until"] == (received + datetime.timedelta(seconds=50)).isoformat()
    text, template = row["payload"]["messages"]
    assert text == {"type": "text", "text":
                    "我讀到 2 種藥：/ I read 2 medicines:\n"
                    "1. PANADOL：1錠，一天 3 次（早上、中午、晚上），5 天\n"
                    "2. IBU：每次 2，需要時服用\n"
                    + line_photo.FOOTER_TEXT}
    assert template["type"] == "template" and template["altText"] == line_photo.BUTTON_TEXT
    assert template["template"]["type"] == "buttons" and template["template"]["text"] == line_photo.BUTTON_TEXT
    (action,) = template["template"]["actions"]
    assert action == {"type": "postback", "label": line_photo.SAVE_LABEL, "displayText": line_photo.SAVE_LABEL,
                      "data": line_photo.sign_save(draft["draft_id"])}
    assert line_photo.verify_save(action["data"]) == draft["draft_id"]
    assert env.wakes == [1]
    # Every LINE call and the reader ran off the event loop; the loading animation came first.
    assert [c[0] for c in env.calls] == ["start_loading", "get_content"]
    assert env.calls[0][1:3] == (PATIENT, 60)
    assert all(c[-1] != MAIN_THREAD for c in env.calls) and env.ocr.calls[0][1] != MAIN_THREAD


def test_late_read_goes_by_push(env):
    received = datetime.datetime.now(UTC) - datetime.timedelta(minutes=3)
    assert _read(env, received) == "read"
    (row,) = env.db.outbox
    assert row["payload"]["reply_until"] == (received + datetime.timedelta(seconds=50)).isoformat()
    assert not outbox_dispatcher._reply_is_fresh(row["payload"], datetime.datetime.now(UTC))


def test_duplicates_and_problems_are_marked_in_the_list(env):
    env.ocr = FakeOCR(result=_result(_med("Panadol"), _med("NO STOCK", stock=0), _med("GOOD")))
    env.db.meds.append({"u_id": 7, "med_name": "PANA DOL", "is_active": True})
    assert _read(env) == "read"
    (row,) = env.db.outbox
    text, template = row["payload"]["messages"]
    lines = text["text"].split("\n")
    assert lines[1].startswith("1. Panadol：") and lines[1].endswith("（已在清單中）")
    assert lines[2].startswith("2. NO STOCK：") and lines[2].endswith("（請在 App 檢查）")
    assert lines[3] == "3. GOOD：1錠，一天 3 次（早上、中午、晚上），5 天"
    assert template["type"] == "template"


def test_a_course_that_already_ended_is_marked_for_checking():
    result = _result(_med("OLD"), date_dispensed="2026-01-01")
    (text, nothing) = line_photo.list_messages(result, TODAY.isoformat(), [], str(uuid.uuid4()))
    assert text["text"].split("\n")[1].endswith("（請在 App 檢查）")
    assert nothing == {"type": "text", "text": line_photo.NOTHING_TEXT}


def test_nothing_savable_has_no_button(env):
    env.ocr = FakeOCR(result=_result(_med("PANADOL"), _med("NO STOCK", stock=0)))
    env.db.meds.append({"u_id": 7, "med_name": "PANADOL"})
    assert _read(env) == "read"
    (row,) = env.db.outbox
    messages = row["payload"]["messages"]
    assert [m["type"] for m in messages] == ["text", "text"]
    assert messages[1]["text"] == line_photo.NOTHING_TEXT
    assert _only_draft(env)["status"] == "pending"


def test_custom_times_and_bedtime_are_listed():
    med = _med("Q4H", schedule_time={"morning": True, "noon": True, "night": True, "bedtime": True,
                                     "before_meals": False, "after_meals": False, "custom_times": ["16:00", "06:00"]},
               times_per_day=6, days=None)
    (text, _) = line_photo.list_messages(_result(med), TODAY.isoformat(), [], str(uuid.uuid4()))
    assert text["text"].split("\n")[1] == "1. Q4H：1錠，一天 6 次（早上、中午、晚上、睡前、16:00、06:00）"


# ── the Save all postback data ───────────────────────────────────────────────

def test_sign_and_verify_save():
    draft_id = str(uuid.uuid4())
    data = line_photo.sign_save(draft_id)
    assert data.startswith(f"action=line_photo_save&id={draft_id}&s=") and len(data.split("&s=")[1]) == 16
    assert line_photo.verify_save(data) == draft_id

    other = str(uuid.uuid4())
    signature = data.split("&s=")[1]
    flipped = signature[:-1] + ("0" if signature[-1] != "0" else "1")
    assert line_photo.verify_save(data[:-1] + flipped[-1]) is None                         # tampered signature
    assert line_photo.verify_save(f"action=line_photo_save&id={other}&s={signature}") is None   # another draft
    assert line_photo.verify_save(data + "&x=" + "a" * 300) is None                         # too long
    assert line_photo.verify_save(data + "&x=1") is None                                   # an extra key
    assert line_photo.verify_save(data + "&id=" + draft_id) is None                        # a repeated key
    assert line_photo.verify_save(data.replace("line_photo_save", "dose_confirm", 1)) is None   # wrong action
    upper = draft_id.upper()
    assert line_photo.verify_save(line_photo.sign_save(upper)) is None                     # not a canonical UUID
    assert line_photo.verify_save(dose_confirmation.sign_postback("dose_confirm", draft_id, "taken", 3)) is None
    assert line_photo.verify_save(None) is None and line_photo.verify_save("") is None
    assert len(line_photo.sign_save(draft_id)) <= 300


def test_dose_confirmation_ignores_save_data():
    assert dose_confirmation.verify_postback(line_photo.sign_save(str(uuid.uuid4()))) is None


# ── logs ─────────────────────────────────────────────────────────────────────

def test_photo_logs_carry_no_ids_or_names(env, caplog):
    caplog.set_level(logging.DEBUG)
    env.ocr = FakeOCR(result=_result(_med("SECRETMEDNAME")))
    assert _read(env) == "read"
    env.db.consent[7] = False
    assert _receive(_image(message_id="MSG-2")) == "consent"
    env.db.contacts.append({"line_id": "U-family-1", "relationship": "child"})
    assert _receive(_image(user="U-family-1")) == "family"
    text = "\n".join(r.getMessage() for r in caplog.records)
    draft_id = _only_draft(env)["draft_id"]
    for secret in (PATIENT, TOKEN, MESSAGE, "MSG-2", "U-family-1", "SECRETMEDNAME", draft_id):
        assert secret not in text
    photo_lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[LINE] photo")]
    assert photo_lines == ["[LINE] photo: read", "[LINE] photo: consent", "[LINE] photo: family"]


# ── the webhook ──────────────────────────────────────────────────────────────

def _signed(events):
    body = json.dumps({"events": events}).encode()
    signature = base64.b64encode(hmac.new(b"secret", body, hashlib.sha256).digest()).decode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({"type": "http", "headers": [(b"x-line-signature", signature.encode())]}, receive)


def test_webhook_routes_images_before_the_text_skip(monkeypatch, caplog):
    seen = []

    async def receive(event, received_at):
        seen.append((event["message"]["id"], received_at))
        if event["message"]["id"] == "bad":
            raise RuntimeError("secret detail U-patient")
        return "reading"

    class Connection:
        async def fetchrow(self, query, *args):
            assert "FROM family_contacts" in query
            seen.append(("code", args))
            return None

    class Acquire:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *exc):
            return False

    replies = []
    service = types.SimpleNamespace(reply_text=lambda token, to, text: replies.append(text))
    monkeypatch.setattr(api_notify, "LINE_CHANNEL_SECRET", "secret")
    monkeypatch.setattr(api_notify, "get_pool", lambda: types.SimpleNamespace(acquire=lambda: Acquire()))
    monkeypatch.setattr(api_notify.LineService, "get_instance", classmethod(lambda cls: service))
    monkeypatch.setattr(api_notify.line_photo, "receive", receive)
    caplog.set_level(logging.INFO)

    events = [_image(message_id="bad"), _image(message_id="good"),
              {"type": "message", "message": {"type": "text", "text": "abcd"}, "replyToken": TOKEN,
               "source": {"type": "user", "userId": PATIENT}},
              {"type": "message", "message": {"type": "sticker"}, "replyToken": TOKEN,
               "source": {"type": "user", "userId": PATIENT}}]
    before = datetime.datetime.now(UTC)
    assert asyncio.run(api_notify.line_webhook(_signed(events))) == {"status": "ok"}

    assert [s[0] for s in seen] == ["bad", "good", "code"]
    assert seen[0][1] == seen[1][1] and seen[0][1] >= before        # the webhook's own receive time
    assert seen[2][1] == ("ABCD",)
    assert replies == ["驗證碼無效，請確認後重新輸入。/ Invalid verification code."]
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "[LINE] photo handling failed: RuntimeError" in logged and "secret detail" not in logged


# ── the background task holder ───────────────────────────────────────────────

def test_spawn_keeps_the_task_until_it_ends():
    async def main():
        done = asyncio.Event()

        async def job():
            await done.wait()
            return "x"

        task = line_photo._spawn(job())
        assert task in line_photo._tasks
        done.set()
        assert await task == "x"
        await asyncio.sleep(0)
        assert task not in line_photo._tasks

    asyncio.run(main())
