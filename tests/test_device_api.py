"""Device API routes on the private port, the browser/robot 409, and manual 'Use Reachy' tasks."""

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
                     "status": "in_progress", "lease_owner": DEVICE_ID}
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
                                  "evidence": {"score": .8}, "started_at": None}]   # no dose session open


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
