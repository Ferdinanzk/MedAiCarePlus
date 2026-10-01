"""Device API routes on the private port, the browser/robot 409, and manual 'Use Reachy' tasks."""

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

import app.services as services_pkg
from app import config
from app.config import DEVICE_PORT, PUBLIC_PORT
from app.dependencies import get_current_user
from app.routers import api_device, api_monitor, api_reachy
from app.services import consent_service, monitor_service, outbox, reachy_tasks
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
                     "status": "in_progress", "lease_owner": DEVICE_ID}
        self.doses = {101: {"dose_form": "solid_oral", "units_per_dose": Decimal("1.00")},
                      102: {"dose_form": "liquid", "units_per_dose": Decimal("1.00")},
                      103: {"dose_form": "solid_oral", "units_per_dose": Decimal("1.00")}}
        self.extra = set()
        self.executed = []
        self.intakes = {101: SLOT, 102: SLOT + timedelta(minutes=2)}
        self.device_row = {"device_id": uuid.UUID(DEVICE_ID)}

    def transaction(self):
        return _Transaction(self)

    async def fetchrow(self, query, *args):
        if "FROM reachy_task WHERE task_id = $1::uuid AND u_id = $2 AND lease_owner = $3::uuid" in query:
            t = self.task
            ok = str(t["task_id"]) == args[0] and t["u_id"] == args[1] and t["lease_owner"] == args[2]
            return dict(t) if ok else None
        if "SELECT m.dose_form, m.units_per_dose FROM intake i" in query:
            return self.doses.get(args[0]) if args[1] == 7 else None
        if "SELECT intake_time_stamp FROM intake" in query:
            stamp = self.intakes.get(args[0])
            return {"intake_time_stamp": stamp} if stamp and args[1] == 7 else None
        if "FROM notification_settings" in query:
            return None
        if "SELECT task_id, status, slot_time, intk_ids FROM reachy_task" in query:
            return {"task_id": uuid.UUID(args[0]), "status": "queued", "slot_time": SLOT, "intk_ids": [101, 102]}
        if 'SELECT u_id, name, face_label FROM "user"' in query:
            return {"u_id": 7, "name": "Pearl", "face_label": "pearl"}
        if "SELECT i.intk_id FROM intake i JOIN medication m ON m.med_id=i.med_id" in query:
            return {"intk_id": args[0]}
        raise AssertionError(query)

    async def fetch(self, query, *args):
        if "SELECT i.intk_id FROM intake i JOIN medication m" in query:
            return [{"intk_id": i} for i, stamp in self.intakes.items() if args[1] <= stamp < args[2]]
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
        raise AssertionError(query)

    async def execute(self, query, *args):
        self.executed.append((query, args, self.depth))


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

    async def create(c, *, u_id, task_id, intk_ids, source, evidence):
        assert c.depth > 0
        confirmations.append({"u_id": u_id, "task_id": task_id, "intk_ids": intk_ids, "source": source,
                              "evidence": evidence})
        if intk_ids == [102] and source == "patient_claim":
            raise ValueError("Dose is not awaiting a result")
        return "conf-1"

    async def get_state(u_id):
        return CONSENT

    async def enqueue_reachy_task(c, u_id, slot_time, intk_ids, reason, expires_at):
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
    for module in (api_device, api_monitor, api_reachy):
        monkeypatch.setattr(module, "get_pool", lambda: FakePool(conn))
    monkeypatch.setattr(api_device, "registry", registry)
    monkeypatch.setattr(api_monitor, "registry", registry)
    monkeypatch.setattr(outbox, "enqueue_to_contacts", enqueue_to_contacts)
    monkeypatch.setattr(consent_service, "get_state", get_state)
    monkeypatch.setattr(reachy_tasks, "enqueue_reachy_task", enqueue_reachy_task)
    monkeypatch.setattr(monitor_service.IntakeDetectionService, "get_instance", classmethod(lambda cls: _Detector()))
    monkeypatch.setattr(api_device.FaceRecognitionService, "_available", True)
    monkeypatch.setattr(api_device.EmotionService, "_available", True)
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
                                  "evidence": {"score": .8}}]


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
