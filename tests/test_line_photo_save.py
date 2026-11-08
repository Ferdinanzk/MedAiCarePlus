"""Task 4 of docs/superpowers/plans/2026-11-08-line-medicine-photo.md: the patient's Save all press saves exactly what
the Scan page would save, once, in one transaction."""

import asyncio
import base64
import copy
import datetime
import hashlib
import hmac
import json
import logging
import sys
import types
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from starlette.requests import Request  # noqa: E402

from app.routers import api_medications, api_notify  # noqa: E402
from app.services import dose_confirmation, line_photo, memory  # noqa: E402

UTC = datetime.timezone.utc
PATIENT = "U-patient-4711"
OTHER = "U-someone-else-0042"
TOKEN = "RT-token-0815"
GOLDEN = {case["name"]: case for case in json.loads(
    (Path(__file__).parent / "scan_parity.json").read_text(encoding="utf-8"))["cases"]}
RECEIPT = GOLDEN["receipt dispensed 3 Oct: every course runs"]
REAL_LOCAL_TODAY = memory.local_today


# ── fakes ────────────────────────────────────────────────────────────────────

class FakeDB:
    """One database shared by every connection: a row lock on the draft and a rollback on an exception."""

    def __init__(self):
        self.users = {7: {"line_id": PATIENT, "user_active": True}}
        self.drafts = {}
        self.meds = []          # {"u_id", "med_name", "is_active"}
        self.inserts = []       # the INSERT INTO medication args
        self.intakes = []
        self.outbox = []
        self.consent = {7: True}
        self.fail_insert_at = None
        self.lock = None

    def state(self):
        return copy.deepcopy((self.drafts, self.meds, self.inserts, self.intakes, self.outbox))

    def restore(self, state):
        self.drafts, self.meds, self.inserts, self.intakes, self.outbox = state


class FakeConn:
    def __init__(self, db):
        self.db = db
        self.holding = False
        self.in_transaction = False

    def transaction(self):
        conn = self

        class Transaction:
            async def __aenter__(self):
                conn.in_transaction = True
                self.saved = conn.db.state()

            async def __aexit__(self, exc_type, exc, tb):
                if exc_type is not None:
                    conn.db.restore(self.saved)
                conn.in_transaction = False
                if conn.holding:
                    conn.holding = False
                    conn.db.lock.release()
                return False

        return Transaction()

    async def fetchrow(self, query, *args):
        assert "FROM line_photo_draft d" in query and 'JOIN "user" u USING (u_id)' in query, query
        assert "d.draft_id = $1" in query and query.rstrip().endswith("FOR UPDATE OF d"), query
        assert self.in_transaction
        if self.db.lock is None:
            self.db.lock = asyncio.Lock()
        await self.db.lock.acquire()
        self.holding = True
        draft = self.db.drafts.get(args[0])
        if draft is None:
            return None
        user = self.db.users[draft["u_id"]]
        return {**draft, "line_id": user["line_id"], "user_active": user["user_active"]}

    async def fetch(self, query, *args):
        if "FROM medication" in query:
            assert "u_id = $1" in query and "is_active IS NOT FALSE" in query
            return [{"med_name": m["med_name"]} for m in self.db.meds
                    if m["u_id"] == args[0] and m.get("is_active") is not False]
        raise AssertionError(query)

    async def fetchval(self, query, *args):
        if "INSERT INTO medication" in query:
            assert self.in_transaction
            await asyncio.sleep(0)   # let a second press reach the row lock meanwhile
            if self.db.fail_insert_at is not None and len(self.db.inserts) + 1 == self.db.fail_insert_at:
                raise RuntimeError("the database went away")
            self.db.inserts.append(args)
            self.db.meds.append({"u_id": args[0], "med_name": args[1], "is_active": args[9]})
            return 100 + len(self.db.inserts)
        if "INSERT INTO notification_outbox" in query:
            dedupe_key, u_id, contact_id, line_id, kind, priority, payload = args
            if any(r["dedupe_key"] == dedupe_key for r in self.db.outbox):
                return None
            self.db.outbox.append({"dedupe_key": dedupe_key, "u_id": u_id, "recipient_line_id": line_id,
                                   "kind": kind, "priority": priority, "payload": json.loads(payload)})
            return len(self.db.outbox)
        raise AssertionError(query)

    async def execute(self, query, *args):
        assert "UPDATE line_photo_draft SET status = 'saved', saved_at = NOW()" in query, query
        assert "WHERE draft_id = $1" in query and self.in_transaction
        self.db.drafts[args[0]].update(status="saved", saved_at=datetime.datetime.now(UTC))

    async def executemany(self, query, rows):
        assert "INSERT INTO intake" in query and self.in_transaction
        self.db.intakes.extend(rows)


class FakePool:
    def __init__(self, db):
        self.db = db

    def acquire(self):
        conn = FakeConn(self.db)

        class Acquire:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return Acquire()


@pytest.fixture
def env(monkeypatch):
    db = FakeDB()
    replies = []
    wakes = []
    consent_reads = []

    service = types.SimpleNamespace(reply_text=lambda token, to, text: replies.append((token, to, text)))

    async def fetch_state(conn, u_id):
        consent_reads.append((conn.in_transaction, u_id))
        return {"core": {"granted": db.consent.get(u_id, False),
                         "terms_version": line_photo.consent_service.config.TERMS_VERSION}}

    async def no_cached_state(u_id):
        raise AssertionError("Save all reads consent inside its transaction, never from the cache")

    monkeypatch.setattr(line_photo, "get_pool", lambda: FakePool(db))
    monkeypatch.setattr(line_photo.LineService, "get_instance", classmethod(lambda cls: service))
    monkeypatch.setattr(line_photo.consent_service, "fetch_state", fetch_state)
    monkeypatch.setattr(line_photo.consent_service, "get_state", no_cached_state)
    monkeypatch.setattr(line_photo.outbox, "wake", lambda: wakes.append(1))
    monkeypatch.setattr(line_photo.memory, "local_today", lambda: datetime.date(2026, 10, 4))
    return types.SimpleNamespace(db=db, replies=replies, wakes=wakes, consent_reads=consent_reads)


def _draft(db, result, status="pending", hours_left=20.0, u_id=7):
    draft_id = str(uuid.uuid4())
    now = datetime.datetime.now(UTC)
    db.drafts[draft_id] = {"draft_id": draft_id, "u_id": u_id, "line_message_id": f"MSG-{draft_id[:8]}",
                           "status": status, "result": json.dumps(result, ensure_ascii=False) if result else None,
                           "error_code": None, "created_at": now - datetime.timedelta(hours=1),
                           "expires_at": now + datetime.timedelta(hours=hours_left), "saved_at": None}
    return draft_id


def _press(db, draft_id, sender=PATIENT, at=None, token=TOKEN):
    received_at = at or datetime.datetime(2026, 10, 4, 3, 0, tzinfo=UTC)

    async def run():
        async with FakePool(db).acquire() as conn:
            return await line_photo.handle_save(conn, line_photo.sign_save(draft_id), sender, token, received_at)

    return asyncio.run(run())


def _texts(row):
    return [message["text"] for message in row["payload"]["messages"]]


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


def _result(*meds, dispensed="2026-10-03"):
    return {"date_dispensed": dispensed, "visit_date": dispensed, "use_before": "N/A", "medications": list(meds)}


def _expected_args(payload):
    """What POST /api/medications stores for the page's payload (its INSERT, in its argument order)."""
    return (7, payload["name"].strip(), payload["dosage"], payload["total_pills"],
            Decimal(str(payload["pills_remaining"])), payload["instructions"], payload["warning"],
            payload["pill_description"], payload["use_before"], payload["is_active"],
            payload["schedule_time"], payload["prescription_meta"], payload["dose_form"],
            Decimal(str(payload["units_per_dose"])), payload.get("min_interval_minutes"),
            payload.get("max_daily_doses"))


def _decoded(args):
    args = list(args)
    args[10] = json.loads(args[10]) if args[10] is not None else None
    args[11] = json.loads(args[11]) if args[11] is not None else None
    return tuple(args)


# ── the insert, lifted out of POST /api/medications ─────────────────────────

def test_create_medication_still_inserts_the_same_row(monkeypatch):
    payload = api_medications.MedicationPayload(**RECEIPT["expected"]["payloads"][0])
    created, lifted = FakeDB(), FakeDB()
    route_conn, direct_conn = FakeConn(created), FakeConn(lifted)
    route_conn.in_transaction = direct_conn.in_transaction = True   # the fake insists; the route itself has none
    monkeypatch.setattr(api_medications, "get_pool",
                        lambda: types.SimpleNamespace(acquire=lambda: _Same(route_conn)))

    from_route = asyncio.run(api_medications.create_medication(payload, {"u_id": 7}))
    from_helper = asyncio.run(api_medications.insert_medication(direct_conn, 7, payload))

    assert from_route == from_helper == {
        "id": 101, "name": "VITACOBAL CAPSULES 0", "dose_form": "solid_oral", "units_per_dose": 1.0,
        "min_interval_minutes": None, "max_daily_doses": None}
    assert created.inserts == lifted.inserts
    assert _decoded(lifted.inserts[0]) == _expected_args(RECEIPT["expected"]["payloads"][0])


class _Same:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


# ── Save all ─────────────────────────────────────────────────────────────────

def test_save_all_by_the_owner_saves_every_savable_medicine(env, caplog):
    caplog.set_level(logging.INFO)
    draft_id = _draft(env.db, RECEIPT["result"])
    at = datetime.datetime(2026, 10, 4, 3, 0, tzinfo=UTC)
    assert _press(env.db, draft_id, at=at) == "saved"

    payloads = RECEIPT["expected"]["payloads"]
    assert [_decoded(args) for args in env.db.inserts] == [_expected_args(p) for p in payloads]
    assert env.db.drafts[draft_id]["status"] == "saved" and env.db.drafts[draft_id]["saved_at"] is not None
    [row] = env.db.outbox
    assert row["dedupe_key"] == f"line_photo:{draft_id}:save:{at.isoformat()}"
    assert (row["u_id"], row["recipient_line_id"], row["kind"], row["priority"]) == (7, PATIENT, "line_photo", 1)
    assert row["payload"]["reply_token"] == TOKEN
    assert _texts(row) == ["已儲存 3 種藥。/ Saved 3 medicines."]
    assert env.wakes and env.replies == []
    assert env.consent_reads == [(True, 7)]

    lines = [r.getMessage() for r in caplog.records if "[LINE] photo save" in r.getMessage()]
    assert lines == ["[LINE] photo save: saved"]
    logged = caplog.text
    for secret in (PATIENT, TOKEN, draft_id, "VITACOBAL", "GASTRO", "OTOMYX"):
        assert secret not in logged


@pytest.mark.parametrize("case", ["someone_else", "no_row", "inactive"])
def test_save_all_by_someone_else_saves_nothing(env, case):
    draft_id = _draft(env.db, RECEIPT["result"])
    sender = PATIENT
    if case == "someone_else":
        sender = OTHER
    elif case == "no_row":
        draft_id = str(uuid.uuid4())
    else:
        env.db.users[7]["user_active"] = False
    _press(env.db, draft_id, sender=sender)
    assert env.db.inserts == [] and env.db.intakes == [] and env.db.outbox == []
    assert env.replies == [(TOKEN, sender, line_photo.EXPIRED_TEXT)]
    if case != "no_row":
        assert env.db.drafts[draft_id]["status"] == "pending"


def test_second_press_says_already_saved(env):
    draft_id = _draft(env.db, RECEIPT["result"])
    _press(env.db, draft_id, at=datetime.datetime(2026, 10, 4, 3, 0, tzinfo=UTC))
    assert _press(env.db, draft_id, at=datetime.datetime(2026, 10, 4, 3, 1, tzinfo=UTC)) == "already"
    assert len(env.db.inserts) == 3
    assert [_texts(row) for row in env.db.outbox] == [["已儲存 3 種藥。/ Saved 3 medicines."],
                                                       ["已經儲存過了。/ Already saved."]]


@pytest.mark.parametrize("status, hours_left", [("pending", -0.01), ("failed", 20), ("reading", 20),
                                                ("refused", 20)])
def test_after_24_hours_says_expired(env, status, hours_left):
    draft_id = _draft(env.db, RECEIPT["result"], status=status, hours_left=hours_left)
    assert _press(env.db, draft_id) == "expired"
    assert env.db.inserts == []
    assert env.db.drafts[draft_id]["status"] == status
    [row] = env.db.outbox
    assert _texts(row) == ["這份清單已過期，請重新傳送照片。/ This list has expired. Please send the photo again."]
    assert row["kind"] == "line_photo" and row["priority"] == 1 and env.replies == []


def test_save_all_skips_duplicates_and_problems_and_names_them(env, monkeypatch):
    env.db.meds.append({"u_id": 7, "med_name": "Panadol", "is_active": True})
    env.db.meds.append({"u_id": 7, "med_name": "OLDMED", "is_active": False})   # archived: no duplicate
    result = _result(_med("PANADOL"), _med("LOWSTOCK", stock=0), _med("BADUNITS"), _med("OLDMED"), _med("GOOD"))
    real = line_photo.scan_rules.medication_payload

    def payload(draft, res):   # a payload the API's own validation refuses
        made = real(draft, res)
        return {**made, "units_per_dose": 0} if made["name"] == "BADUNITS" else made

    monkeypatch.setattr(line_photo.scan_rules, "medication_payload", payload)
    draft_id = _draft(env.db, result)
    assert _press(env.db, draft_id) == "saved"
    assert [args[1] for args in env.db.inserts] == ["OLDMED", "GOOD"]
    [row] = env.db.outbox
    assert _texts(row) == ["已儲存 2 種藥。/ Saved 2 medicines.\n"
                           "PANADOL（已在清單中 / already saved）\n"
                           "LOWSTOCK（請在 App 檢查 / check in the app）\n"
                           "BADUNITS（請在 App 檢查 / check in the app）"]


def test_duplicates_inside_one_photo_are_both_saved(env):
    draft_id = _draft(env.db, _result(_med("SAME"), _med("SAME")))
    _press(env.db, draft_id)
    assert [args[1] for args in env.db.inserts] == ["SAME", "SAME"]


def test_save_all_rechecks_consent(env):
    env.db.consent[7] = False
    draft_id = _draft(env.db, RECEIPT["result"])
    assert _press(env.db, draft_id) == "consent"
    assert env.db.inserts == [] and env.db.drafts[draft_id]["status"] == "pending"
    [row] = env.db.outbox
    assert _texts(row) == ["請先打開 App 並同意使用條款。/ Please open the app and accept the terms first."]
    assert env.consent_reads == [(True, 7)]


def test_save_all_uses_the_taipei_date(env, monkeypatch):
    class Clock(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            moment = datetime.datetime(2026, 10, 4, 16, 30, tzinfo=UTC)   # 10-05 00:30 in Taipei
            return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)

    monkeypatch.setattr(memory, "local_today", REAL_LOCAL_TODAY)   # the real one, on a fixed clock
    monkeypatch.setattr(memory.config, "MEDCARE_TIMEZONE", "Asia/Taipei")
    monkeypatch.setattr(memory, "datetime", Clock)
    # ENDS4 runs 10-02..10-04: still running on the UTC date, ended on Taipei's
    draft_id = _draft(env.db, _result(_med("ENDS4", days=3), _med("ENDS6", days=5), dispensed="2026-10-02"))
    _press(env.db, draft_id)
    assert [args[1] for args in env.db.inserts] == ["ENDS6"]
    [row] = env.db.outbox
    assert _texts(row) == ["已儲存 1 種藥。/ Saved 1 medicines.\nENDS4（請在 App 檢查 / check in the app）"]


def test_a_failed_insert_rolls_back_and_says_so(env, caplog):
    caplog.set_level(logging.INFO)
    env.db.fail_insert_at = 2
    draft_id = _draft(env.db, RECEIPT["result"])
    at = datetime.datetime(2026, 10, 4, 3, 0, tzinfo=UTC)
    assert _press(env.db, draft_id, at=at) == "failed"
    assert env.db.inserts == [] and env.db.intakes == [] and env.db.meds == []
    assert env.db.drafts[draft_id]["status"] == "pending"
    [row] = env.db.outbox
    assert row["dedupe_key"] == f"line_photo:{draft_id}:save:{at.isoformat()}"
    assert _texts(row) == ["儲存失敗，請在 App 的掃描頁再試一次。/ Couldn't save. Please try again on the Scan page in the app."]
    assert row["payload"]["reply_token"] == TOKEN and env.wakes
    lines = [r.getMessage() for r in caplog.records if "[LINE] photo save" in r.getMessage()]
    assert len(lines) == 1 and lines[0].startswith("[LINE] photo save: failed")
    assert "the database went away" not in caplog.text

    env.db.fail_insert_at = None   # the patient presses again: it saves now
    assert _press(env.db, draft_id, at=at + datetime.timedelta(seconds=5)) == "saved"
    assert len(env.db.inserts) == 3


def test_concurrent_presses_save_once(env):
    draft_id = _draft(env.db, RECEIPT["result"])
    data = line_photo.sign_save(draft_id)

    async def both():
        async def press(second):
            async with FakePool(env.db).acquire() as conn:
                return await line_photo.handle_save(
                    conn, data, PATIENT, TOKEN, datetime.datetime(2026, 10, 4, 3, 0, second, tzinfo=UTC))
        return await asyncio.gather(press(0), press(1))

    assert sorted(asyncio.run(both())) == ["already", "saved"]
    assert len(env.db.inserts) == 3
    assert sorted(_texts(row)[0] for row in env.db.outbox) == ["已儲存 3 種藥。/ Saved 3 medicines.",
                                                               "已經儲存過了。/ Already saved."]


def test_a_tampered_or_foreign_postback_is_not_a_save(env):
    draft_id = _draft(env.db, RECEIPT["result"])

    async def run(data):
        async with FakePool(env.db).acquire() as conn:
            return await line_photo.handle_save(conn, data, PATIENT, TOKEN, datetime.datetime.now(UTC))

    tampered = line_photo.sign_save(draft_id)[:-1] + ("0" if line_photo.sign_save(draft_id)[-1] != "0" else "1")
    assert asyncio.run(run(tampered)) is None
    assert asyncio.run(run("action=dose_confirm&id=5&a=taken&c=3&s=abc")) is None
    assert env.db.inserts == [] and env.db.outbox == [] and env.replies == []


# ── the webhook ──────────────────────────────────────────────────────────────

SECRET = "channel-secret"


def _webhook(events):
    body = json.dumps({"events": events}).encode()
    signature = base64.b64encode(hmac.new(SECRET.encode(), body, hashlib.sha256).digest()).decode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request({"type": "http", "headers": [(b"x-line-signature", signature.encode())]}, receive)
    return asyncio.run(api_notify.line_webhook(request))


def _postback(data, user_id=PATIENT):
    return {"type": "postback", "postback": {"data": data}, "source": {"type": "user", "userId": user_id},
            "replyToken": TOKEN}


def test_dose_confirm_postbacks_still_reach_their_handler(env, monkeypatch):
    monkeypatch.setattr(api_notify, "LINE_CHANNEL_SECRET", SECRET)
    monkeypatch.setattr(api_notify, "get_pool", lambda: FakePool(env.db))
    handled = []

    async def handle_postback(conn, data, sender, reply_token, received_at=None):
        handled.append((data, sender, reply_token))
        return {"status": "taken"}

    monkeypatch.setattr(dose_confirmation, "handle_postback", handle_postback)
    dose_data = "action=dose_confirm&id=5&a=taken&c=3&s=0123456789abcdef"
    draft_id = _draft(env.db, RECEIPT["result"])

    _webhook([_postback(dose_data, OTHER), _postback(line_photo.sign_save(draft_id))])

    assert handled == [(dose_data, OTHER, TOKEN)]          # the dose answer reached its handler, the save did not
    assert len(env.db.inserts) == 3 and env.db.drafts[draft_id]["status"] == "saved"
