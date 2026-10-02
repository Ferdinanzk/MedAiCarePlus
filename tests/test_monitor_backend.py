import asyncio
import sys
import time
import types
from decimal import Decimal

import cv2
import numpy as np

# The repository unit tests use a fake pool and do not require a PostgreSQL
# driver. Keep collection runnable on a lightweight developer Python too.
_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app.services import intake_repository
from app.services.monitor_service import MonitorRegistry, select_owned_observations


def _jpeg():
    ok, encoded = cv2.imencode(".jpg", np.zeros((100, 100, 3), dtype=np.uint8))
    assert ok
    return encoded.tobytes()


def _packet(frame_seq):
    return {
        "frame_seq": frame_seq,
        # Realistic capture clock: the browser worker runs at ~15 fps and the
        # server-side frame-rate gate blocks auto-commit below 12 fps.
        "timestamp": frame_seq / 15,
        "width": 100,
        "height": 100,
        "faces": [{
            "box": [0.1, 0.1, 0.4, 0.4],
            "points": [[0.3, 0.3]] * 9,
        }],
        "poses": [{
            "nose": [0.3, 0.3],
            "shoulders": [[0.2, 0.5], [0.4, 0.5]],
            "wrists": [],
        }],
        "hands": [],
    }


def _hand(x=0.2, y=0.6):
    return [[x, y]] * 13


class _Detector:
    async def process_frame(self, *args, **kwargs):
        return {"stage": "WAITING", "decision": "none", "frame_seq": args[-1]["frame_seq"]}

    async def end_session(self, *args, **kwargs):
        return None


class _Faces:
    def identify_faces(self, frame):
        return {
            "faces": [{"label": "pearl", "distance": 0.2, "box": [10, 10, 40, 40]}],
            "error": None,
            "saturated": False,
        }


class _Emotion:
    def predict_crop(self, crop):
        return {"detected": True, "label": "happy", "probabilities": {"happy": 1.0}}


def test_vision_uses_matching_landmark_snapshot_when_newer_packets_arrive(monkeypatch):
    from app.services import monitor_service as service

    monkeypatch.setattr(service.IntakeDetectionService, "get_instance", classmethod(lambda cls: _Detector()))
    monkeypatch.setattr(service.FaceRecognitionService, "get_instance", classmethod(lambda cls: _Faces()))
    monkeypatch.setattr(service.EmotionService, "get_instance", classmethod(lambda cls: _Emotion()))

    async def scenario():
        registry = MonitorRegistry()
        state = registry.start(7, 11, "Pearl", "Pearl")
        # Seed an exact frame packet, then let a much newer landmark request
        # arrive while face recognition is still running.
        await registry.landmarks(state, _packet(1))
        state.packets[1] = _packet(1)
        state.last_frame_seq = 1

        original = _Faces.identify_faces

        def delayed(self, frame):
            time.sleep(0.03)
            return original(self, frame)

        _Faces.identify_faces = delayed
        try:
            task = asyncio.create_task(registry.vision(state, 1, _jpeg(), lambda *_: None))
            await asyncio.sleep(0.005)
            await registry.landmarks(state, _packet(10))
            result = await task
        finally:
            _Faces.identify_faces = original
        assert result["identity_status"] == "verifying"
        assert state.identity_hits == 1

    asyncio.run(scenario())


def test_stale_candidate_expires_on_landmark_request_without_vision(monkeypatch):
    from app.services import monitor_service as service

    monkeypatch.setattr(service.IntakeDetectionService, "get_instance", classmethod(lambda cls: _Detector()))

    async def scenario():
        state = MonitorRegistry().start(7, 11, "Pearl", "Pearl")
        state.candidate = {"event_id": "expired", "created_at": time.monotonic() - 6, "ready": False}
        await service.registry.landmarks(state, _packet(1))
        assert state.candidate is None

    asyncio.run(scenario())


def test_owned_observations_reject_bystander_ambiguity_and_hand_saturation():
    packet = _packet(1)
    target = packet["faces"][0]["box"]
    bystander_pose = {
        "nose": [0.31, 0.31],
        "shoulders": [[0.2, 0.5], [0.4, 0.5]],
        "wrists": [],
    }
    assert select_owned_observations(
        packet["faces"], packet["poses"] + [bystander_pose], [], target) is None
    assert select_owned_observations(
        packet["faces"], packet["poses"], [_hand() for _ in range(8)], target) is None


def test_candidate_requires_fresh_identity_match_before_commit(monkeypatch):
    from app.services import monitor_service as service

    class CountingFaces(_Faces):
        calls = 0

        def identify_faces(self, frame):
            self.calls += 1
            return super().identify_faces(frame)

    faces = CountingFaces()
    monkeypatch.setattr(service.IntakeDetectionService, "get_instance", classmethod(lambda cls: _Detector()))
    monkeypatch.setattr(service.FaceRecognitionService, "get_instance", classmethod(lambda cls: faces))
    monkeypatch.setattr(service.EmotionService, "get_instance", classmethod(lambda cls: _Emotion()))

    async def scenario():
        registry = MonitorRegistry()
        state = registry.start(7, 11, "Pearl", "Pearl")
        # ~1.5 s of landmark capture at 15 fps satisfies the frame-rate gate.
        seq = 23
        for frame_seq in range(1, seq + 1):
            await registry.landmarks(state, _packet(frame_seq))
        packet = state.packets[seq]
        state.identity_hits = 2
        state.identity_frame_seq = 0
        state.verified_at = time.monotonic()
        state.target_box = packet["faces"][0]["box"]
        state.candidate = {
            "event_id": "fresh", "decision": "confirmed", "confidence": .9,
            "created_at": time.monotonic(), "frame_seq": seq,
            "emotion_probabilities": None, "identity_distance": .2, "ready": False,
        }
        commits = []

        async def commit(state, candidate, method):
            commits.append((candidate["event_id"], method))
            return {"event_id": candidate["event_id"], "status": "taken"}

        await registry.vision(state, seq, _jpeg(), commit)
        assert faces.calls == 1
        assert commits == [("fresh", "auto")]
        assert state.identity_frame_seq == seq

    asyncio.run(scenario())


def test_registry_replaces_and_prunes_detector_sessions(monkeypatch):
    from app.services import monitor_service as service

    detector = _Detector()
    monkeypatch.setattr(service.IntakeDetectionService, "get_instance", classmethod(lambda cls: detector))

    async def scenario():
        registry = MonitorRegistry()
        old = registry.start(7, 11, "Pearl", "Pearl")
        current = await registry.replace(7, 12, "Pearl", "Pearl")
        assert old.ended
        assert old.session_id not in registry.sessions
        assert registry.by_user[7] == current.session_id
        await registry.end(current)
        assert current.session_id not in registry.sessions
        assert 7 not in registry.by_user

    asyncio.run(scenario())


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _Connection:
    def __init__(self):
        self.executed = []

    def transaction(self):
        return _Transaction()

    async def fetchrow(self, query, *args):
        if "FROM intake" in query:
            return {"intk_id": 9, "med_id": 4, "intake_stats": "taken", "units_taken": Decimal("0.50")}
        raise AssertionError(f"unexpected query: {query}")

    async def execute(self, query, *args):
        self.executed.append((query, args))


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *args):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


def test_manual_status_correction_retires_monitor_event(monkeypatch):
    conn = _Connection()
    monkeypatch.setattr(intake_repository, "get_pool", lambda: _Pool(conn))
    result = asyncio.run(intake_repository.transition_intake(7, 9, "skipped"))
    assert result == {"intk_id": 9, "status": "skipped", "changed": True}
    status_sql = next(query for query, _ in conn.executed if "UPDATE intake SET intake_stats" in query)
    assert "$1::varchar" in status_sql
    assert any("UPDATE monitor_event SET outcome='rejected'" in query for query, _ in conn.executed)
    # Undoing the taken dose puts back exactly what it removed (half a tablet here).
    restore = next(args for query, args in conn.executed if "pills_remaining=pills_remaining+" in query)
    assert restore == (4, Decimal("0.50"))
