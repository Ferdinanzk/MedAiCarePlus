"""Device API routes on the private port, the browser/robot 409, and manual 'Use Reachy' tasks."""

import json
import os
import sys
import types
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

sys.path.insert(0, os.path.dirname(__file__))
import dose_facts  # noqa: E402

import app.services as services_pkg
from app import config
from app.config import DEVICE_PORT, PUBLIC_PORT
from app.dependencies import get_current_user
from app.routers import api_device, api_monitor, api_reachy
from app.services import consent_service, dose_safety, monitor_service, outbox, reachy_tasks, schedule
from app.services.device_auth import get_device, get_device_for_heartbeat
from app.services.monitor_service import MonitorRegistry

DEVICE_ID = str(uuid.uuid4())
TASK_ID = str(uuid.uuid4())
SLOT = datetime(2026, 9, 30, 0, 0, tzinfo=timezone.utc)
CONSENT = {"core": {"granted": True, "terms_version": config.TERMS_VERSION},
           "robot_camera": {"granted": True, "terms_version": config.TERMS_VERSION}}


class _Transaction:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        self.conn.depth += 1
        return self

    async def __aexit__(self, *args):
        self.conn.depth -= 1
        return False


class FakeConn:
    def __init__(self):
        self.depth = 0
        self.task = {"task_id": uuid.UUID(TASK_ID), "u_id": 7, "slot_time": SLOT, "intk_ids": [101, 102],
                     "status": "in_progress", "lease_owner": DEVICE_ID, "reason": "upcoming"}
        self.doses = {101: {"dose_form": "solid_oral", "units_per_dose": Decimal("1.00"), "intake_time_stamp": SLOT},
                      102: {"dose_form": "liquid", "units_per_dose": Decimal("1.00"), "intake_time_stamp": SLOT},
                      103: {"dose_form": "solid_oral", "units_per_dose": Decimal("1.00"), "intake_time_stamp": SLOT}}
        self.extra = set()
        self.executed = []
        self.intakes = {101: SLOT, 102: SLOT + timedelta(minutes=2)}
        self.previous = {}   # intk_id -> the same medicine's previous dose time (schedule.previous_sql)
        self.facts = {}      # intk_id -> dose_safety.FACTS_SQL fields that differ from a plain dose
        self.facts_asked = []
        self.device_row = {"device_id": uuid.UUID(DEVICE_ID)}
        self.source = self.doses   # monitor starts read `doses`, manual tasks `intakes`
        self.status_detail = None  # what the heartbeat stored (asyncpg hands JSONB back as text)
        self.task_due = False      # what reachy_tasks.task_due finds: a medicine task waiting (companion spec 3.4)
        self.task_due_asked = []   # (arguments, transaction depth) of each task_due read
        self.requests = []         # dose_confirmation rows the family-answer route reads (newest found by created_at)

    def transaction(self):
        return _Transaction(self)

    async def fetchrow(self, query, *args):
        if "FROM reachy_task WHERE task_id = $1::uuid AND u_id = $2 AND lease_owner = $3::uuid" in query:
            t = self.task
            ok = str(t["task_id"]) == args[0] and t["u_id"] == args[1] and t["lease_owner"] == args[2]
            return dict(t) if ok else None
        if "SELECT m.dose_form, m.units_per_dose, i.intake_time_stamp FROM intake i" in query:
            self.source = self.doses
            dose = self.doses.get(args[0])
            return dict(dose) if dose and args[1] == 7 else None
        if "SELECT intake_time_stamp FROM intake WHERE intk_id = $1" in query:
            self.source = self.intakes
            stamp = self.intakes.get(args[0])
            return {"intake_time_stamp": stamp} if stamp and args[1] == 7 else None
        if "FROM notification_settings" in query:
            return None
        if "SELECT task_id, status, slot_time, intk_ids FROM reachy_task" in query:
            return {"task_id": uuid.UUID(args[0]), "status": "queued", "slot_time": SLOT, "intk_ids": [101, 102]}
        if 'SELECT u_id, name, face_label FROM "user"' in query:
            return {"u_id": 7, "name": "Pearl", "face_label": "pearl"}
        if "SELECT i.intk_id, m.dose_form, m.units_per_dose, i.intake_time_stamp FROM intake i JOIN medication m" \
                in query:
            self.source = self.doses
            return {"intk_id": args[0],
                    **self.doses.get(args[0], {"dose_form": "solid_oral", "units_per_dose": Decimal("1.00"),
                                               "intake_time_stamp": SLOT})}
        if "FROM reachy_device WHERE u_id = $1 AND revoked_at IS NULL" in query:
            assert "status_detail" in query   # api_reachy._status reads the robot's camera from it
            return {"device_id": uuid.UUID(DEVICE_ID), "label": "Reachy Mini", "auto_record": False,
                    "live_voice": False, "last_seen_at": datetime.now(timezone.utc), "robot_reachable": True,
                    "landmark_fps": 9.9, "status_detail": self.status_detail}
        if "SELECT task_id, status, slot_time, finished_at FROM reachy_task WHERE u_id = $1" in query:
            return None
        if 'SELECT reachy_call_name, name FROM "user" WHERE u_id = $1' in query:
            return {"reachy_call_name": None, "name": "Pearl"}
        if "SELECT m.med_name FROM intake i JOIN medication m" in query:   # the dose event route's medicine
            return {"med_name": "Allegra"} if args[0] in self.doses and args[1] == 7 else None
        if query == api_device.FAMILY_ANSWER_SQL:
            u_id, task_id, wanted = args
            rows = [r for r in self.requests if r["u_id"] == u_id and str(r["task_id"]) == task_id
                    and (wanted is None or str(r["confirmation_id"]) == wanted)]
            return dict(max(rows, key=lambda r: r["created_at"])) if rows else None
        raise AssertionError(query)

    def _stamp(self, intk_id):
        found = self.source.get(intk_id)
        return found["intake_time_stamp"] if isinstance(found, dict) else found

    async def fetch(self, query, *args):
        if dose_facts.is_facts(query):
            u_id, ids, at, zone = args
            self.facts_asked.append(list(ids))
            rows = [{"intk_id": i, "u_id": u_id, "med_id": i, "intake_stats": "pending",
                     "intake_time_stamp": self._stamp(i) or SLOT, "previous_time": self.previous.get(i),
                     "next_time": None, "med_name": f"med {i}", "schedule_time": None, "min_interval_minutes": None,
                     "max_daily_doses": None, "protection": True, "language": None, "last_taken_at": None,
                     "taken_that_day": 0, **self.facts.get(i, {})} for i in ids]
            return sorted(rows, key=lambda r: (r["intake_time_stamp"], r["intk_id"]))
        if "SELECT i.intk_id FROM intake i JOIN medication m" in query:
            # Only the slot's doses that may start (due, and not expired, under the patient's switch).
            assert dose_safety.startable_sql("i", "$4::timestamptz") in query
            return [{"intk_id": i} for i, stamp in self.intakes.items() if args[1] <= stamp < args[2]
                    and (not self.facts.get(i, {}).get("protection", True)
                         or schedule.is_due(stamp, args[3], self.previous.get(i)))]
        raise AssertionError(query)

    async def fetchval(self, query, *args):
        if "INSERT INTO monitor_extra_event" in query:
            assert self.depth > 0
            if args[0] in self.extra:
                return None
            self.extra.add(args[0])
            self.executed.append((query, args))
            return args[0]
        if "SELECT device_id FROM reachy_device" in query:
            return self.device_row["device_id"] if self.device_row else None
        if 'SELECT name FROM "user"' in query:
            return "Pearl"
        if "FROM notification_outbox" in query and "kind = 'double_dose_alert'" in query:
            return None      # no alert for this dose within the hour
        if "FROM family_contacts" in query:
            return 1         # contacts that can receive family alerts (the Reachy card's status)
        if "SELECT EXISTS (SELECT 1 FROM reachy_task t WHERE" in query:   # reachy_tasks.task_due
            self.task_due_asked.append((args, self.depth))
            return self.task_due
        raise AssertionError(query)

    async def execute(self, query, *args):
        self.executed.append((query, args, self.depth))
        if "status_detail = $4::jsonb" in query:
            self.status_detail = args[3]


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


class _Detector:
    async def end_session(self, *args):
        return None


@pytest.fixture
def env(monkeypatch):
    from app.main import app

    conn = FakeConn()
    registry = MonitorRegistry()
    device = {"device_id": DEVICE_ID, "u_id": 7, "auto_record": False, "face_label": "pearl", "name": "Pearl"}
    notices, confirmations, enqueued = [], [], []

    async def enqueue_to_contacts(c, u_id, **kwargs):
        assert c.depth > 0
        notices.append({"u_id": u_id, **kwargs})
        return 2

    async def create(c, *, u_id, task_id, intk_ids, source, evidence, started_at=None):
        assert c.depth > 0
        confirmations.append({"u_id": u_id, "task_id": task_id, "intk_ids": intk_ids, "source": source,
                              "evidence": evidence, "started_at": started_at})
        if intk_ids == [102] and source == "patient_claim":
            raise ValueError("Dose is not awaiting a result")
        return "conf-1"

    async def get_state(u_id):
        return CONSENT

    async def enqueue_reachy_task(c, u_id, slot_time, intk_ids, reason, expires_at, at=None):
        enqueued.append((u_id, slot_time, intk_ids, reason, expires_at, c.depth))
        return TASK_ID

    fake_confirmation = types.ModuleType("app.services.dose_confirmation")
    fake_confirmation.create = create
    monkeypatch.setitem(sys.modules, "app.services.dose_confirmation", fake_confirmation)
    monkeypatch.setattr(services_pkg, "dose_confirmation", fake_confirmation, raising=False)
    monkeypatch.setattr(app, "dependency_overrides", {
        get_device: lambda: dict(device),
        get_device_for_heartbeat: lambda: {**device, "revoked": False, "consent_current": True},
        get_current_user: lambda: {"u_id": 7, "name": "Pearl"},
    })
    for module in (api_device, api_monitor, api_reachy, dose_safety):
        monkeypatch.setattr(module, "get_pool", lambda: FakePool(conn))
    monkeypatch.setattr(api_device, "registry", registry)
    monkeypatch.setattr(api_monitor, "registry", registry)
    monkeypatch.setattr(outbox, "enqueue_to_contacts", enqueue_to_contacts)
    monkeypatch.setattr(consent_service, "get_state", get_state)
    monkeypatch.setattr(reachy_tasks, "enqueue_reachy_task", enqueue_reachy_task)
    monkeypatch.setattr(monitor_service.IntakeDetectionService, "get_instance", classmethod(lambda cls: _Detector()))
    monkeypatch.setattr(api_device.FaceRecognitionService, "_available", True)
    monkeypatch.setattr(api_monitor.EmotionService, "_available", True)   # browser sessions still need it
    return types.SimpleNamespace(app=app, conn=conn, registry=registry, device=device, notices=notices,
                                 confirmations=confirmations, enqueued=enqueued,
                                 robot=TestClient(app, base_url=f"http://testserver:{DEVICE_PORT}"),
                                 browser=TestClient(app, base_url=f"http://testserver:{PUBLIC_PORT}"))


# ── Tasks ────────────────────────────────────────────────────────────────────

def test_next_task_long_poll_returns_204_or_task(env, monkeypatch):
    calls = []

    async def lease_next(u_id, device_id, wait):
        calls.append((u_id, device_id, wait))
        return {"task_id": TASK_ID} if len(calls) > 1 else None

    monkeypatch.setattr(reachy_tasks, "lease_next", lease_next)
    assert env.robot.get("/api/device/tasks/next?wait=0").status_code == 204
    response = env.robot.get("/api/device/tasks/next?wait=25")
    assert response.status_code == 200 and response.json() == {"task_id": TASK_ID}
    assert calls == [(7, DEVICE_ID, 0), (7, DEVICE_ID, 25)]
    assert env.robot.get("/api/device/tasks/next?wait=26").status_code == 422


def test_current_task_204_when_none(env, monkeypatch):
    async def current_task(u_id, device_id):
        return None

    monkeypatch.setattr(reachy_tasks, "current_task", current_task)
    assert env.robot.get("/api/device/tasks/current").status_code == 204


def test_status_route_maps_errors(env, monkeypatch):
    async def set_status(u_id, device_id, task_id, status, detail):
        if status == "completed":
            raise ValueError("Illegal transition leased -> completed")
        if task_id == "missing":
            raise reachy_tasks.TaskNotFound("Task not found")
        return {"task_id": task_id, "status": status, "detail": detail}

    monkeypatch.setattr(reachy_tasks, "set_status", set_status)
    ok = env.robot.post(f"/api/device/tasks/{TASK_ID}/status", json={"status": "searching", "detail": {"a": 1}})
    assert ok.status_code == 200 and ok.json()["detail"] == {"a": 1}
    assert env.robot.post(f"/api/device/tasks/{TASK_ID}/status", json={"status": "completed"}).status_code == 409
    assert env.robot.post("/api/device/tasks/missing/status", json={"status": "searching"}).status_code == 404
    assert env.robot.post(f"/api/device/tasks/{TASK_ID}/status", json={"status": "queued"}).status_code == 422


def test_confirmation_calls_dose_confirmation_create(env):
    response = env.robot.post(f"/api/device/tasks/{TASK_ID}/confirmation",
                              json={"intk_id": 101, "source": "auto_record_off", "evidence": {"score": .8}})
    assert response.status_code == 200 and response.json() == {"confirmation_id": "conf-1"}
    assert env.confirmations == [{"u_id": 7, "task_id": TASK_ID, "intk_ids": [101], "source": "auto_record_off",
                                  "evidence": {"score": .8, "video": "none:no_consent"},   # dose videos are off here
                                  "started_at": None}]   # no dose session open


def test_a_confirmation_request_keeps_its_task_alive_for_the_family_wait(env):
    """The robot waits up to 120 s for family's answer and may then search and run the intake again: in the same
    transaction as the request, the task's expiry moves to at least 600 s from now (never earlier) while it is
    leased, searching or in progress."""
    response = env.robot.post(f"/api/device/tasks/{TASK_ID}/confirmation",
                              json={"intk_id": 101, "source": "patient_claim"})
    assert response.status_code == 200 and response.json() == {"confirmation_id": "conf-1"}
    (extend,) = [entry for entry in env.conn.executed if "UPDATE reachy_task SET expires_at" in entry[0]]
    query, args, depth = extend
    assert api_device.CONFIRM_WAIT_EXTEND_SECONDS == 600 and depth == 1   # inside the request's transaction
    assert "GREATEST(expires_at, NOW() + INTERVAL '600 seconds')" in query
    assert "status IN ('leased','searching','in_progress')" in query and args == (TASK_ID,)
    env.conn.executed.clear()   # a refused request (409) extends nothing
    assert env.robot.post(f"/api/device/tasks/{TASK_ID}/confirmation",
                          json={"intk_id": 102, "source": "patient_claim"}).status_code == 409
    assert not any("UPDATE reachy_task SET expires_at" in entry[0] for entry in env.conn.executed)


def _request(confirmation_id, minutes, resolution=None, task_id=TASK_ID, u_id=7, intk_ids=(101,)):
    created = SLOT + timedelta(minutes=minutes)
    return {"confirmation_id": uuid.UUID(confirmation_id), "u_id": u_id, "task_id": uuid.UUID(task_id),
            "intk_ids": list(intk_ids), "resolution": resolution, "created_at": created,
            "resolved_at": created + timedelta(minutes=1) if resolution else None}


def test_family_answer_is_none_when_the_task_has_no_request(env):
    response = env.robot.get(f"/api/device/tasks/{TASK_ID}/confirmation")
    assert response.status_code == 200 and response.json() == {"status": "none"}   # never a 404: no session lost


@pytest.mark.parametrize("resolution, status", [
    (None, "open"), ("confirmed", "confirmed"), ("denied", "denied"), ("expired", "expired")])
def test_family_answer_gives_the_newest_requests_status(env, resolution, status):
    first, newest = str(uuid.uuid4()), str(uuid.uuid4())
    env.conn.requests = [_request(first, 1, "denied"), _request(newest, 5, resolution, intk_ids=(101, 102))]
    response = env.robot.get(f"/api/device/tasks/{TASK_ID}/confirmation")
    assert response.status_code == 200
    created = SLOT + timedelta(minutes=5)
    assert response.json() == {
        "confirmation_id": newest, "status": status, "intk_ids": [101, 102], "created_at": created.isoformat(),
        "resolved_at": (created + timedelta(minutes=1)).isoformat() if resolution else None}


def test_family_answer_reads_the_request_its_id_names(env):
    """A slot of two medicines has two requests: the robot reads each by the id its POST answered."""
    first, second, other_task = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    env.conn.requests = [_request(first, 1, "confirmed"), _request(second, 5, None, intk_ids=(102,)),
                         _request(str(uuid.uuid4()), 9, "denied", task_id=other_task)]
    response = env.robot.get(f"/api/device/tasks/{TASK_ID}/confirmation", params={"id": first})
    assert response.json()["confirmation_id"] == first and response.json()["status"] == "confirmed"
    assert response.json()["intk_ids"] == [101]
    assert env.robot.get(f"/api/device/tasks/{TASK_ID}/confirmation").json()["confirmation_id"] == second
    # an id that names no request of this task (another task's, or none): no request, never a 404
    assert env.robot.get(f"/api/device/tasks/{TASK_ID}/confirmation",
                         params={"id": str(uuid.uuid4())}).json() == {"status": "none"}
    assert env.robot.get(f"/api/device/tasks/{TASK_ID}/confirmation", params={"id": "not-a-uuid"}).status_code == 422


def test_family_answer_needs_this_devices_task(env):
    env.conn.requests = [_request(str(uuid.uuid4()), 1, "confirmed")]
    assert env.robot.get(f"/api/device/tasks/{uuid.uuid4()}/confirmation").status_code == 404
    assert env.robot.get("/api/device/tasks/not-a-task/confirmation").status_code == 404
    env.conn.task["lease_owner"] = str(uuid.uuid4())   # another robot holds it
    assert env.robot.get(f"/api/device/tasks/{TASK_ID}/confirmation").status_code == 404
    assert env.browser.get(f"/api/device/tasks/{TASK_ID}/confirmation").status_code == 404   # device port only


def test_dose_confirmation_has_an_index_for_the_family_answer_poll():
    from pathlib import Path
    schema = (Path(__file__).resolve().parents[1] / "sql" / "init.sql").read_text(encoding="utf-8")
    statement = ("CREATE INDEX IF NOT EXISTS dose_confirmation_task_idx ON dose_confirmation "
                 "(task_id, created_at DESC);")
    assert statement in schema and schema.index(statement) > schema.index("CREATE TABLE IF NOT EXISTS dose_confirmation")


def test_confirmation_rejects_foreign_dose_task_and_errors(env):
    assert env.robot.post(f"/api/device/tasks/{TASK_ID}/confirmation",
                          json={"intk_id": 999, "source": "degraded"}).status_code == 409
    assert env.robot.post(f"/api/device/tasks/{uuid.uuid4()}/confirmation",
                          json={"intk_id": 101, "source": "degraded"}).status_code == 404
    assert env.robot.post(f"/api/device/tasks/{TASK_ID}/confirmation",
                          json={"intk_id": 102, "source": "patient_claim"}).status_code == 409
    assert env.robot.post(f"/api/device/tasks/{TASK_ID}/confirmation",
                          json={"intk_id": 101, "source": "made_up"}).status_code == 422
    env.conn.task["lease_owner"] = str(uuid.uuid4())
    assert env.robot.post(f"/api/device/tasks/{TASK_ID}/confirmation",
                          json={"intk_id": 101, "source": "degraded"}).status_code == 404


def test_extra_event_records_row_and_family_notice_once(env):
    event_id = str(uuid.uuid4())
    body = {"event_id": event_id, "decision": "uncertain", "confidence": .55}
    first = env.robot.post(f"/api/device/tasks/{TASK_ID}/extra-event", json=body)
    assert first.status_code == 200
    assert first.json()["recorded"] is True and first.json()["notified"] == 2
    assert len(env.notices) == 1
    notice = env.notices[0]
    assert notice["kind"] == "extra_event" and notice["contact_flag"] == "notify_missed" and notice["priority"] == 1
    message = notice["messages"][0]
    assert message["type"] == "text"
    assert "08:00" in message["text"]
    assert message["text"].index("手部靠近嘴巴") < message["text"].index("hand-to-mouth")
    assert "This may not be a pill" in message["text"]
    insert = next(entry for entry in env.conn.executed if "INSERT INTO monitor_extra_event" in entry[0])
    assert insert[1][1:] == (7, TASK_ID, "uncertain", .55)
    # A replay of the same event neither duplicates the row nor re-notifies.
    again = env.robot.post(f"/api/device/tasks/{TASK_ID}/extra-event", json=body)
    assert again.json() == {**first.json(), "recorded": False, "notified": 0}
    assert len(env.notices) == 1


def test_extra_event_validates_payload(env):
    assert env.robot.post(f"/api/device/tasks/{TASK_ID}/extra-event",
                          json={"event_id": "x", "decision": "uncertain", "confidence": .5}).status_code == 422
    assert env.robot.post(f"/api/device/tasks/{TASK_ID}/extra-event",
                          json={"event_id": str(uuid.uuid4()), "decision": "none", "confidence": .5}).status_code == 422


def test_heartbeat_updates_device_and_extends_leases(env, monkeypatch):
    extended = []

    async def extend_leases(conn, device_id):
        extended.append((device_id, conn.depth))

    monkeypatch.setattr(reachy_tasks, "extend_leases", extend_leases)
    response = env.robot.post("/api/device/heartbeat", json={
        "robot_reachable": True, "landmark_fps": 14.8, "vision_fps": 4.9, "bridge_version": "0.2.0",
        "missing_clips": 1})
    assert response.status_code == 200
    body = response.json()
    assert body["stop_all"] is False and datetime.fromisoformat(body["server_time"])
    update = next(entry for entry in env.conn.executed if "last_seen_at = NOW()" in entry[0])
    assert update[1][:3] == (DEVICE_ID, True, 14.8) and update[2] == 1
    assert extended == [(DEVICE_ID, 1)]
    assert body["microphone"] is False   # no robot_microphone consent


def test_heartbeat_reports_microphone_consent(env, monkeypatch):
    async def extend_leases(conn, device_id):
        return None

    async def get_state(u_id):
        return {**CONSENT, "robot_microphone": {"granted": True, "terms_version": config.TERMS_VERSION}}

    monkeypatch.setattr(reachy_tasks, "extend_leases", extend_leases)
    monkeypatch.setattr(consent_service, "get_state", get_state)
    assert env.robot.post("/api/device/heartbeat", json={}).json()["microphone"] is True


def test_heartbeat_says_whether_a_medicine_task_waits(env, monkeypatch):
    # Companion spec 3.4: a robot in a companion session ends it, with its medicine line, when task_due is true.
    _no_leases(monkeypatch)
    assert env.robot.post("/api/device/heartbeat", json={"bridge_version": "0.9.0"}).json()["task_due"] is False
    env.conn.task_due = True
    assert env.robot.post("/api/device/heartbeat", json={"bridge_version": "0.9.0"}).json()["task_due"] is True
    assert env.conn.task_due_asked == [((7,), 0), ((7,), 0)]   # this patient, after the heartbeat's transaction ended


def test_a_companion_heartbeat_keeps_the_held_task_alive(env, monkeypatch):
    _no_leases(monkeypatch)

    def extensions():
        return [entry for entry in env.conn.executed
                if "SET expires_at = GREATEST(expires_at" in entry[0] and "status = 'in_progress'" in entry[0]]

    assert env.robot.post("/api/device/heartbeat", json={"companion": False}).status_code == 200
    assert env.robot.post("/api/device/heartbeat", json={}).status_code == 200
    assert extensions() == []
    assert env.robot.post("/api/device/heartbeat", json={"companion": True}).status_code == 200
    found = extensions()
    assert len(found) == 1
    assert found[0][1] == (DEVICE_ID,) and found[0][2] == 1   # inside the write transaction, with the lease extension


def test_the_heartbeat_stores_whether_a_companion_session_runs(env, monkeypatch):
    """Re-review of quick fix 4: a meal retry with the patient holds its dose from the missed-dose job except in the
    companion chat after the dose, which the job reads from the robot's latest heartbeat (status_detail's companion
    flag, reachy_tasks.meal_retry_waiting_sql). Each heartbeat replaces status_detail, so the flag is there only
    while a companion session runs."""
    _no_leases(monkeypatch)
    for body, stored in (({"companion": True}, True), ({"companion": False}, None), ({}, None)):
        assert env.robot.post("/api/device/heartbeat", json=body).status_code == 200
        update = [entry for entry in env.conn.executed if "last_seen_at = NOW()" in entry[0]][-1]
        assert json.loads(update[1][3]).get("companion") is stored, body


# ── The robot's camera (robot app 0.7.0): stored with the heartbeat, shown on the Reachy card ──

CAMERA_MEDIA = {"available": True, "released": False, "no_media": False}   # the daemon's GET /api/media/status


def _no_leases(monkeypatch):
    async def extend_leases(conn, device_id):
        return None

    monkeypatch.setattr(reachy_tasks, "extend_leases", extend_leases)


def _stored_detail(env) -> dict:
    update = next(entry for entry in env.conn.executed if "last_seen_at = NOW()" in entry[0])
    return json.loads(update[1][3])


@pytest.mark.parametrize("state", ["idle", "ok", "stalled", "recovering", "unavailable"])
def test_heartbeat_stores_the_camera_report_and_status_returns_it(env, monkeypatch, state):
    _no_leases(monkeypatch)
    camera = {"state": state, "fps": 9.8 if state == "ok" else 0.0, "since": 1791700000.5, "media": CAMERA_MEDIA}
    response = env.robot.post("/api/device/heartbeat", json={"bridge_version": "0.7.0", "camera": camera})
    assert response.status_code == 200
    assert _stored_detail(env) == {"vision_fps": None, "bridge_version": "0.7.0", "missing_clips": None,
                                   "camera": camera}
    status = env.browser.get("/api/reachy/status")
    assert status.status_code == 200 and status.json()["camera"] == camera


def test_an_older_robot_sends_no_camera_and_status_returns_none(env, monkeypatch):
    _no_leases(monkeypatch)
    assert env.browser.get("/api/reachy/status").json()["camera"] is None       # no heartbeat yet
    assert env.robot.post("/api/device/heartbeat", json={"bridge_version": "0.5.4"}).status_code == 200
    assert "camera" not in _stored_detail(env)
    assert env.browser.get("/api/reachy/status").json()["camera"] is None


@pytest.mark.parametrize("camera", [
    {"state": "broken"}, {"state": "OK"}, {"fps": 9.8}, {"state": "ok", "fps": -1}, {"state": "ok", "fps": 241},
    {"state": "ok", "media": "released"}, "stalled",
])
def test_heartbeat_refuses_a_bad_camera_report(env, monkeypatch, camera):
    _no_leases(monkeypatch)
    response = env.robot.post("/api/device/heartbeat", json={"camera": camera})
    assert response.status_code == 422
    assert not any("last_seen_at = NOW()" in entry[0] for entry in env.conn.executed)


def test_heartbeat_refuses_a_camera_number_that_is_not_finite(env, monkeypatch):
    """json.dumps writes NaN and Infinity as bare words, which PostgreSQL's jsonb refuses: that must be a 422, not a 500."""
    _no_leases(monkeypatch)
    for field in ("since", "fps"):
        for word in ("NaN", "Infinity", "-Infinity"):
            body = '{"camera": {"state": "ok", "%s": %s}}' % (field, word)    # raw text: the test client would refuse to write it
            response = env.robot.post("/api/device/heartbeat", content=body, headers={"content-type": "application/json"})
            assert response.status_code == 422, f"{field}={word} gave {response.status_code}"
    assert not any("last_seen_at = NOW()" in entry[0] for entry in env.conn.executed)


# ── Frame stream: the server computes the landmarks ─────────────────────────

def _jpeg(width=64, height=48) -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (90, 90, 90)).save(buffer, format="JPEG")
    return buffer.getvalue()


class _Engine:
    def __init__(self):
        self.frames = []

    def process(self, image, frame_seq, timestamp_ms):
        self.frames.append((frame_seq, image.size, timestamp_ms))
        return {"frame_seq": frame_seq, "timestamp": timestamp_ms / 1000, "width": image.size[0],
                "height": image.size[1], "faces": [], "hands": [], "poses": []}


@pytest.fixture
def frames(env, monkeypatch):
    engines, visions = [], []

    class _Service:
        def new_engine(self):
            engines.append(_Engine())
            return engines[-1]

    async def frame_vision(state, frame_seq, jpeg):
        visions.append(frame_seq)

    monkeypatch.setattr(api_device.LandmarkService, "_available", True)
    monkeypatch.setattr(api_device.LandmarkService, "get_instance", classmethod(lambda cls: _Service()))
    monkeypatch.setattr(api_device, "_frame_vision", frame_vision)
    session = _start(env, mode="observe").json()

    def post(seq, timestamp, data=None):
        return env.robot.post("/api/device/monitor/frame", data={
            "session_id": session["session_id"], "generation": session["generation"],
            "frame_seq": str(seq), "timestamp": str(timestamp)},
            files={"file": ("frame.jpg", data if data is not None else _jpeg(), "image/jpeg")})
    return types.SimpleNamespace(post=post, engines=engines, visions=visions, session=session)


def test_frame_stream_feeds_the_monitor_session_at_capture_rate(env, frames):
    for seq in range(1, 31):
        response = frames.post(seq, 100 + seq / 15)
        assert response.status_code == 200
    body = response.json()
    assert body["frame_seq"] == 30 and body["landmark_fps"] == 15.0 and body["degraded"] is False
    assert len(frames.engines) == 1   # one engine (tracker state) per session
    assert [f[0] for f in frames.engines[0].frames] == list(range(1, 31))
    assert frames.engines[0].frames[0][1:] == ((64, 48), pytest.approx((100 + 1 / 15) * 1000))
    assert frames.visions and frames.visions[0] == 1   # identity runs on streamed frames, throttled
    assert len(frames.visions) < 30


def test_frame_stream_ignores_stale_frames_and_rejects_bad_ones(env, frames):
    assert frames.post(5, 100.0).status_code == 200
    stale = frames.post(4, 100.1)
    assert stale.status_code == 200 and stale.json()["frame_seq"] == 5
    assert [f[0] for f in frames.engines[0].frames] == [5]
    assert frames.post(6, 100.2, data=b"not a jpeg").status_code == 422
    assert frames.post(7, 100.3, data=_jpeg(2000, 100)).status_code == 422
    assert frames.post(8, 100.4, data=b"x" * (api_device.MAX_FRAME_BYTES + 1)).status_code == 413


def test_a_frame_the_server_holds_up_is_logged_with_where_it_waited(env, frames, monkeypatch, caplog):
    with caplog.at_level("WARNING", logger=api_device.log.name):
        assert frames.post(1, 100.0).status_code == 200
        monkeypatch.setattr(api_device, "SLOW_FRAME_SECONDS", -1.0)   # every frame counts as slow
        assert frames.post(2, 100.1).status_code == 200
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("monitor frame ")]
    assert len(lines) == 1 and lines[0].startswith(f"monitor frame 2 of session {frames.session['session_id']}")
    assert "waiting for the session's frame lock" in lines[0] and "computing landmarks" in lines[0]


def test_a_frame_for_a_session_that_just_ended_answers_409_not_500(env, frames, monkeypatch):
    """The intake detector ends a session at its completion barrier; a frame already past the frame lock's
    state.ended check then meets SessionEnded inside registry.landmarks, and gets the same 409 as the check gives."""
    from app.services.intake_detection import SessionEnded

    assert frames.post(1, 100.0).status_code == 200

    async def ended(state, packet):
        raise SessionEnded("session_ended")

    monkeypatch.setattr(env.registry, "landmarks", ended)
    late = frames.post(2, 100.1)
    assert late.status_code == 409
    assert late.json()["detail"] == "Session expired or belongs to another account"


def test_frame_stream_needs_a_live_session_and_the_models(env, frames, monkeypatch):
    other = env.robot.post("/api/device/monitor/frame", data={
        "session_id": str(uuid.uuid4()), "generation": str(uuid.uuid4()), "frame_seq": "1", "timestamp": "1"},
        files={"file": ("frame.jpg", _jpeg(), "image/jpeg")})
    assert other.status_code == 409
    monkeypatch.setattr(api_device.LandmarkService, "_available", False)
    assert frames.post(1, 100.0).status_code == 503


def test_a_dose_session_gets_identity_and_emotion_four_times_a_second(env, monkeypatch):
    """Until its emotion result is written (services/dose_emotion.py), a verified patient's dose session is scored
    every 0.25 s, for the uncovered faces just before and after the pill; otherwise every FRAME_VISION_INTERVAL. Not
    while unverified: each call then runs identity, which stays at most 2 Hz."""
    import time

    visions = []

    class _Service:
        def new_engine(self):
            return _Engine()

    async def frame_vision(state, frame_seq, jpeg):
        visions.append(frame_seq)

    monkeypatch.setattr(api_device.LandmarkService, "_available", True)
    monkeypatch.setattr(api_device.LandmarkService, "get_instance", classmethod(lambda cls: _Service()))
    monkeypatch.setattr(api_device, "_frame_vision", frame_vision)
    session = _start(env, mode="dose", intk_id=101, task_id=TASK_ID).json()
    state = env.registry.sessions[session["session_id"]]

    def post(seq):
        return env.robot.post("/api/device/monitor/frame", data={
            "session_id": session["session_id"], "generation": session["generation"],
            "frame_seq": str(seq), "timestamp": str(100 + seq / 15)},
            files={"file": ("frame.jpg", _jpeg(), "image/jpeg")})

    assert post(1).status_code == 200
    state.last_vision_started = time.monotonic() - 0.3     # past 0.25 s, within 0.5 s
    assert post(2).status_code == 200                       # not verified yet: every 0.5 s
    state.identity_hits, state.verified_at = 2, time.monotonic()
    state.last_vision_started = time.monotonic() - 0.3
    assert post(3).status_code == 200
    state.dose_emotion_done = True                          # result written: back to every 0.5 s
    state.last_vision_started = time.monotonic() - 0.3
    assert post(4).status_code == 200
    state.last_vision_started = time.monotonic() - 0.6
    assert post(5).status_code == 200
    assert visions == [1, 3, 5]


# ── Monitor over the device port ────────────────────────────────────────────

def _start(env, **body):
    return env.robot.post("/api/device/monitor/start", json=body)


@pytest.mark.parametrize("auto_record,intk_id,expected", [
    (False, 101, False),    # D2 default: every event goes to confirmation
    (True, 101, True),      # supported solid oral, one unit
    (True, 102, False),     # unsupported dose form
])
def test_device_dose_session_auto_commit_is_server_policy(env, auto_record, intk_id, expected):
    env.device["auto_record"] = auto_record
    response = _start(env, mode="dose", intk_id=intk_id, task_id=TASK_ID)
    assert response.status_code == 200
    body = response.json()
    assert body["auto_commit"] is expected and body["client_type"] == "reachy" and body["mode"] == "dose"
    assert body["intk_id"] == intk_id and body["degraded"] is True


def test_device_dose_session_validates_task_and_dose(env):
    assert _start(env, mode="dose", intk_id=101).status_code == 422
    assert _start(env, mode="dose", intk_id=103, task_id=TASK_ID).status_code == 409
    assert _start(env, mode="dose", intk_id=101, task_id=str(uuid.uuid4())).status_code == 404
    env.conn.task["status"] = "completed"
    assert _start(env, mode="dose", intk_id=101, task_id=TASK_ID).status_code == 409


def test_device_observe_session_has_no_dose(env):
    env.device["auto_record"] = True
    response = _start(env, mode="observe", intk_id=101, task_id=TASK_ID)
    body = response.json()
    assert response.status_code == 200
    assert body["mode"] == "observe" and body["intk_id"] is None and body["auto_commit"] is False


def test_device_sessions_keep_the_mode_they_started_in(env):
    # Only the search of a check-in task (observe) verifies with the OpenVINO match alone. The search of a dose task
    # keeps the MediaPipe gate (final review of 11 Oct), and so does a dose session.
    def session(**body):
        return env.registry.sessions[_start(env, **body).json()["session_id"]]

    dose_search = session(mode="observe", task_id=TASK_ID)
    assert dose_search.started_mode == "observe" and dose_search.checkin_search is False
    assert monitor_service.openvino_only(dose_search) is False
    dose = session(mode="dose", intk_id=101, task_id=TASK_ID)
    assert dose.started_mode == "dose" and monitor_service.openvino_only(dose) is False
    no_task = session(mode="observe")
    assert no_task.checkin_search is False and monitor_service.openvino_only(no_task) is False
    env.conn.task.update(reason="checkin", intk_ids=[])
    checkin_search = session(mode="observe", task_id=TASK_ID)
    assert checkin_search.started_mode == "observe" and checkin_search.checkin_search is True
    assert monitor_service.openvino_only(checkin_search) is True


def test_a_companion_observe_session_of_a_dose_task_uses_the_checkin_face_rule(env):
    # After a dose the companion phase keeps an observe session of the DOSE task: it needs the check-in face rule.
    session = env.registry.sessions[_start(env, mode="observe", task_id=TASK_ID, companion=True).json()["session_id"]]
    assert env.conn.task["reason"] != "checkin"
    assert session.checkin_search is True and monitor_service.openvino_only(session) is True


def test_a_companion_flag_never_loosens_a_dose_session(env):
    session = env.registry.sessions[
        _start(env, mode="dose", intk_id=101, task_id=TASK_ID, companion=True).json()["session_id"]]
    assert session.checkin_search is False and monitor_service.openvino_only(session) is False


def test_device_start_returns_503_when_models_not_ready(env, monkeypatch):
    monkeypatch.setattr(api_device.FaceRecognitionService, "_available", False)
    assert _start(env, mode="observe").status_code == 503


def test_browser_then_robot_gets_busy_other_client(env):
    browser = env.browser.post("/api/intake/monitor/start", json={"intk_id": 101})
    assert browser.status_code == 200 and browser.json()["client_type"] == "browser"
    robot = _start(env, mode="dose", intk_id=101, task_id=TASK_ID)
    assert robot.status_code == 409 and robot.json() == {"detail": "busy_other_client"}
    assert len([s for s in env.registry.sessions.values() if not s.ended]) == 1


def test_robot_then_browser_gets_busy_other_client(env):
    robot = _start(env, mode="dose", intk_id=101, task_id=TASK_ID)
    assert robot.status_code == 200
    browser = env.browser.post("/api/intake/monitor/start", json={"intk_id": 101})
    assert browser.status_code == 409 and browser.json() == {"detail": "busy_other_client"}
    live = [s for s in env.registry.sessions.values() if not s.ended]
    assert [s.client_type for s in live] == ["reachy"]


def test_sessions_are_not_shared_across_clients(env):
    robot = _start(env, mode="observe").json()
    ids = {"session_id": robot["session_id"], "generation": robot["generation"]}
    packet = {**ids, "frame_seq": 1, "timestamp": 0.0, "width": 640, "height": 480}
    assert env.browser.post("/api/intake/monitor/landmarks", json=packet).status_code == 409
    assert env.browser.post("/api/intake/monitor/end", json=ids).status_code == 409
    response = env.robot.post("/api/device/monitor/landmarks", json=packet)
    assert response.status_code == 200 and response.json()["frame_seq"] == 1
    assert env.robot.post("/api/device/monitor/end", json=ids).json() == {"success": True}
    assert env.robot.post("/api/device/monitor/end", json=ids).status_code == 409


def test_device_commit_records_reachy_prompted_method(monkeypatch):
    import asyncio
    calls = []

    async def commit_monitored(state, candidate, method):
        calls.append(method)
        return {"status": "taken"}

    monkeypatch.setattr(api_device, "commit_monitored", commit_monitored)
    asyncio.run(api_device._reachy_commit(object(), {}, "auto"))
    assert calls == ["reachy_prompted"]


# ── Manual 'Use Reachy' task (public port) ───────────────────────────────────

def test_manual_task_enqueues_the_doses_of_that_slot(env):
    response = env.browser.post("/api/reachy/tasks", json={"intk_id": 102})
    assert response.status_code == 200
    assert response.json() == {"task_id": TASK_ID, "status": "queued", "slot_time": SLOT.isoformat(),
                               "intk_ids": [101, 102]}
    u_id, slot_time, intk_ids, reason, expires_at, depth = env.enqueued[0]
    assert (u_id, slot_time, intk_ids, reason, depth) == (7, SLOT, [101, 102], "manual", 1)
    assert expires_at >= datetime.now(timezone.utc) + timedelta(minutes=14)


def test_manual_task_errors(env, monkeypatch):
    assert env.browser.post("/api/reachy/tasks", json={"intk_id": 999}).json() == {"detail": "dose_not_found"}
    assert env.browser.post("/api/reachy/tasks", json={"intk_id": 999}).status_code == 404
    env.conn.device_row = None
    response = env.browser.post("/api/reachy/tasks", json={"intk_id": 101})
    assert response.status_code == 409 and response.json() == {"detail": "robot_not_paired"}


# ── Doses that are not due yet (more than DOSE_EARLY_MINUTES ahead) ──────────

def _ahead(minutes):
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


def _assert_not_due(response, intk_id, lead=None):
    assert response.status_code == 409
    body = response.json()
    assert body["detail"] == "dose_not_due_yet" and body["intk_id"] == intk_id
    scheduled = datetime.fromisoformat(body["scheduled_time"])
    assert datetime.fromisoformat(body["due_from"]) == scheduled - (lead or schedule.DOSE_EARLY)
    assert scheduled.utcoffset() == timedelta(hours=8)   # the patient's time (Asia/Taipei)
    return body


def test_manual_task_refuses_a_dose_not_due_yet(env):
    """The Reachy card's test alert at 00:05 picked the 08:00 dose."""
    env.conn.intakes[101] = _ahead(7 * 60 + 55)
    _assert_not_due(env.browser.post("/api/reachy/tasks", json={"intk_id": 101}), 101)
    assert env.enqueued == []
    env.conn.intakes[101] = _ahead(schedule.DOSE_EARLY_MINUTES - 5)   # within the early window
    assert env.browser.post("/api/reachy/tasks", json={"intk_id": 101}).status_code == 200


def test_manual_task_leaves_out_a_slot_dose_that_is_not_due(env, monkeypatch):
    now = datetime(2026, 10, 2, 22, 0, tzinfo=timezone.utc)           # 06:00 on 3 Oct in Taipei
    monkeypatch.setattr(schedule, "_now", lambda: now)
    due = now + schedule.DOSE_EARLY                                    # 08:00 with the default 120 minutes
    env.conn.intakes = {101: due, 102: due + timedelta(minutes=2)}
    assert env.browser.post("/api/reachy/tasks", json={"intk_id": 101}).status_code == 200
    assert env.enqueued[0][2] == [101]                                  # 08:00 is due at 06:00; 08:02 is not


def test_manual_task_waits_until_halfway_from_the_same_medicines_previous_dose(env, monkeypatch):
    """The user's allegra is at 20:00 and 22:00, exactly DOSE_EARLY apart: right after the 20:00 dose a test alert
    would have used the 22:00 one too. It is due from 21:00, halfway between them."""
    clock = {"now": datetime(2026, 10, 3, 12, 5, tzinfo=timezone.utc)}         # 20:05 in Taipei
    monkeypatch.setattr(schedule, "_now", lambda: clock["now"])
    bedtime = datetime(2026, 10, 3, 14, 0, tzinfo=timezone.utc)               # 22:00
    env.conn.intakes = {8: bedtime}
    env.conn.previous = {8: bedtime - timedelta(hours=2)}                     # the 20:00 dose
    body = _assert_not_due(env.browser.post("/api/reachy/tasks", json={"intk_id": 8}), 8, lead=timedelta(hours=1))
    assert body["due_from"] == "2026-10-03T21:00:00+08:00" and env.enqueued == []
    clock["now"] = datetime(2026, 10, 3, 13, 0, tzinfo=timezone.utc)          # 21:00
    assert env.browser.post("/api/reachy/tasks", json={"intk_id": 8}).status_code == 200
    assert env.enqueued[0][2] == [8]


def test_device_dose_session_waits_until_halfway_from_the_previous_dose(env, monkeypatch):
    now = datetime(2026, 10, 3, 12, 30, tzinfo=timezone.utc)                  # 20:30 in Taipei
    monkeypatch.setattr(schedule, "_now", lambda: now)
    env.conn.doses[101]["intake_time_stamp"] = now + timedelta(minutes=90)    # 22:00, within DOSE_EARLY
    env.conn.previous[101] = now - timedelta(minutes=30)                      # 20:00
    _assert_not_due(_start(env, mode="dose", intk_id=101, task_id=TASK_ID), 101, lead=timedelta(hours=1))
    _assert_not_due(env.browser.post("/api/intake/monitor/start", json={"intk_id": 101}), 101,
                    lead=timedelta(hours=1))
    assert not [s for s in env.registry.sessions.values() if not s.ended]


def test_device_dose_session_refuses_a_dose_not_due_yet(env):
    env.conn.doses[101]["intake_time_stamp"] = _ahead(3 * 60)
    _assert_not_due(_start(env, mode="dose", intk_id=101, task_id=TASK_ID), 101)
    assert not [s for s in env.registry.sessions.values() if not s.ended]
    env.conn.doses[101]["intake_time_stamp"] = _ahead(60)
    assert _start(env, mode="dose", intk_id=101, task_id=TASK_ID).status_code == 200


def test_browser_dose_session_refuses_a_dose_not_due_yet(env):
    env.conn.doses[101]["intake_time_stamp"] = _ahead(3 * 60)
    _assert_not_due(env.browser.post("/api/intake/monitor/start", json={"intk_id": 101}), 101)
    assert env.registry.sessions == {}


def test_confirmation_for_a_dose_not_due_yet_is_409_with_its_time(env, monkeypatch):
    async def create(c, *, u_id, task_id, intk_ids, source, evidence, started_at=None):
        raise schedule.DoseNotDueYet(SLOT + timedelta(hours=12), intk_ids[0])

    monkeypatch.setattr(sys.modules["app.services.dose_confirmation"], "create", create)
    response = env.robot.post(f"/api/device/tasks/{TASK_ID}/confirmation",
                              json={"intk_id": 101, "source": "patient_claim"})
    body = _assert_not_due(response, 101)
    assert body["scheduled_time"] == "2026-09-30T20:00:00+08:00" and body["due_from"] == "2026-09-30T18:00:00+08:00"
    assert env.notices == []        # an early dose is not a second dose: no family alert


# ── Overdose protection: second doses, missed doses, the switch ─────────────

def test_robot_confirmation_of_a_second_dose_is_refused_and_family_alerted(env, monkeypatch):
    """The robot saw the hand-to-mouth gesture (or heard 「我吃完了」) for a dose an hour after the last one."""
    now = datetime.now(timezone.utc)

    async def create(c, *, u_id, task_id, intk_ids, source, evidence, started_at=None):
        refused = dose_safety.DoseTooSoon(last_taken_at=now - timedelta(minutes=20), gap=timedelta(hours=1),
                                          intk_id=intk_ids[0], med_name="allegra", scheduled_time=now, at=now,
                                          u_id=u_id)
        refused.after_intake = True        # as dose_confirmation.create marks it
        raise refused

    monkeypatch.setattr(sys.modules["app.services.dose_confirmation"], "create", create)
    response = env.robot.post(f"/api/device/tasks/{TASK_ID}/confirmation",
                              json={"intk_id": 101, "source": "auto_record_off"})
    assert response.status_code == 409
    body = response.json()
    assert body["detail"] == "dose_too_soon" and body["intk_id"] == 101 and body["med_name"] == "allegra"
    # Said after the pill went down: not "don't take it" but "not recorded, family told, tell them if unwell".
    assert body["after_intake"] is True and body["reply"].startswith("這次沒有記錄，因為這個藥您")
    assert "已經通知家人" in body["reply"] and body["speech_text"].startswith("这次没有记录，因为这个药您")
    (alert,) = env.notices
    assert alert["kind"] == "double_dose_alert" and alert["priority"] == 0 and alert["contact_flag"] == "notify_missed"
    assert alert["dedupe_prefix"].startswith("double_dose:101:")
    assert "可能重複服藥：Pearl" in alert["messages"][0]["text"]


def test_robot_confirmation_is_judged_from_when_its_dose_session_started(env):
    """The robot files it while its camera session for the dose is open: due and not expired are judged as of the
    session's start (allowed then), so a pill swallowed a minute after the halfway point is still sent to family."""
    assert _start(env, mode="dose", intk_id=101, task_id=TASK_ID).status_code == 200
    (session,) = [s for s in env.registry.sessions.values() if not s.ended]
    response = env.robot.post(f"/api/device/tasks/{TASK_ID}/confirmation",
                              json={"intk_id": 101, "source": "patient_claim"})
    assert response.status_code == 200 and env.confirmations[-1]["started_at"] == session.started_at
    # Another dose of the task has no session of its own: judged now.
    response = env.robot.post(f"/api/device/tasks/{TASK_ID}/confirmation",
                              json={"intk_id": 102, "source": "uncertain_detection"})
    assert response.status_code == 200 and env.confirmations[-1]["started_at"] is None


@pytest.mark.parametrize("facts, detail", [
    ({"last_taken_at": datetime.now(timezone.utc) - timedelta(minutes=30)}, "dose_too_soon"),   # 4 h unscheduled
    # Scheduled doses are on whole minutes (one that is not is an ad-hoc dose, which never expires).
    ({"intake_time_stamp": datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(hours=3),
      "next_time": datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(hours=1)},
     "dose_expired"),
    ({"taken_that_day": 2, "max_daily_doses": 2}, "daily_max_reached"),
])
def test_dose_sessions_refuse_a_second_or_missed_dose_with_a_sentence(env, facts, detail):
    env.conn.facts[101] = facts
    for response in (_start(env, mode="dose", intk_id=101, task_id=TASK_ID),
                     env.browser.post("/api/intake/monitor/start", json={"intk_id": 101})):
        assert response.status_code == 409
        body = response.json()
        assert body["detail"] == detail and body["intk_id"] == 101 and body["reply"] and body["speech_text"]
    assert not [s for s in env.registry.sessions.values() if not s.ended]
    assert env.notices == []        # starting a session is no evidence of a pill
    env.conn.facts[101] = {**facts, "protection": False}
    assert _start(env, mode="dose", intk_id=101, task_id=TASK_ID).status_code == 200


def test_manual_task_leaves_out_a_slot_dose_taken_too_recently(env, monkeypatch):
    now = datetime(2026, 10, 2, 23, 0, tzinfo=timezone.utc)           # 07:00 on 3 Oct in Taipei
    monkeypatch.setattr(schedule, "_now", lambda: now)
    env.conn.intakes = {101: now + timedelta(hours=1), 102: now + timedelta(hours=1, minutes=2)}
    env.conn.facts[102] = {"last_taken_at": now - timedelta(minutes=30)}   # taken ad hoc at 06:30
    assert env.browser.post("/api/reachy/tasks", json={"intk_id": 101}).status_code == 200
    assert env.enqueued[0][2] == [101]
    response = env.browser.post("/api/reachy/tasks", json={"intk_id": 102})
    assert response.status_code == 409 and response.json()["detail"] == "dose_too_soon"
    assert len(env.enqueued) == 1


@pytest.mark.parametrize("path,body", [
    ("/api/medications/intake/101", {"status": "taken"}),
    ("/api/intake/record", {"intk_id": 101, "detection_method": "manual"}),
])
def test_manual_taken_of_a_dose_not_due_yet_is_409(env, monkeypatch, path, body):
    from app.routers import api_intake
    from app.services import intake_repository

    async def transition(u_id, intk_id, status, **kwargs):
        raise schedule.DoseNotDueYet(SLOT, intk_id)

    monkeypatch.setattr(intake_repository, "transition_intake", transition)
    monkeypatch.setattr(api_intake, "transition_intake", transition)
    method = env.browser.patch if path.startswith("/api/medications") else env.browser.post
    _assert_not_due(method(path, json=body), 101)


# ── Emotion scored on the robot ──────────────────────────────────────────────

def test_device_landmarks_validate_the_robot_emotion_report(env):
    robot = _start(env, mode="observe").json()
    ids = {"session_id": robot["session_id"], "generation": robot["generation"]}
    face = {"box": [0.1, 0.1, 0.4, 0.4], "points": [[0.3, 0.3]] * 9}
    probabilities = {name: 1 / 7 for name in ("angry", "disgust", "fear", "happy", "sad", "surprise", "neutral")}
    packet = {**ids, "frame_seq": 1, "timestamp": 0.0, "width": 640, "height": 480, "faces": [face]}
    ok = env.robot.post("/api/device/monitor/landmarks",
                        json={**packet, "emotion": {"face_index": 0, "probabilities": probabilities}})
    assert ok.status_code == 200 and "target_box" in ok.json()
    bad = [
        {"face_index": 1, "probabilities": probabilities},                         # no such face in the packet
        {"face_index": 0, "probabilities": {**probabilities, "happy": 0.9}},       # doesn't sum to 1
        {"face_index": 0, "probabilities": {"happy": 1.0}},                        # missing labels
    ]
    for seq, emotion in enumerate(bad, start=2):
        response = env.robot.post("/api/device/monitor/landmarks", json={**packet, "frame_seq": seq, "emotion": emotion})
        assert response.status_code == 422, emotion


def test_device_start_needs_only_the_identity_model(env, monkeypatch):
    from app.services.emotion_service import EmotionService

    monkeypatch.setattr(EmotionService, "_available", False)
    assert _start(env, mode="observe").status_code == 200


def test_a_failing_task_due_read_answers_false_and_keeps_the_heartbeat_writes(env, monkeypatch, caplog):
    extended = []

    async def extend_leases(conn, device_id):
        extended.append(device_id)

    async def task_due(conn, u_id):
        raise RuntimeError("database went away")

    monkeypatch.setattr(reachy_tasks, "extend_leases", extend_leases)
    monkeypatch.setattr(reachy_tasks, "task_due", task_due)
    with caplog.at_level("WARNING"):
        response = env.robot.post("/api/device/heartbeat", json={"bridge_version": "0.9.0"})
    assert response.status_code == 200 and response.json()["task_due"] is False
    assert any("last_seen_at = NOW()" in entry[0] for entry in env.conn.executed) and extended == [DEVICE_ID]
    assert [r.levelname for r in caplog.records if "medicine-task check" in r.getMessage()] == ["WARNING"]
