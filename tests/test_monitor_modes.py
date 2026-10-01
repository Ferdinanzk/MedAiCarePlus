"""Monitor session modes, the server-side frame-rate gate, post-record observation and client concurrency."""

import asyncio
import itertools
import sys
import time
import types

import cv2
import numpy as np
import pytest

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app.services import monitor_service as service
from app.services.monitor_service import BusyOtherClient, MonitorRegistry


def _jpeg():
    ok, encoded = cv2.imencode(".jpg", np.zeros((100, 100, 3), dtype=np.uint8))
    assert ok
    return encoded.tobytes()


def _packet(frame_seq, timestamp):
    return {
        "frame_seq": frame_seq,
        "timestamp": float(timestamp),
        "width": 100,
        "height": 100,
        "faces": [{"box": [0.1, 0.1, 0.4, 0.4], "points": [[0.3, 0.3]] * 9}],
        "poses": [{"nose": [0.3, 0.3], "shoulders": [[0.2, 0.5], [0.4, 0.5]], "wrists": []}],
        "hands": [],
    }


class _Detector:
    """Returns a scripted decision for chosen frames, WAITING otherwise."""

    def __init__(self, script=None):
        self.script = script or {}
        self.calls = 0

    async def process_frame(self, u_id, session_id, payload, result_transform=None):
        self.calls += 1
        seq = payload["frame_seq"]
        if seq in self.script:
            decision, event_id = self.script[seq]
            return {"stage": "WITHDRAWING", "decision": decision, "event_confidence": .9, "frame_seq": seq,
                    "policy": {"event_id": event_id}}
        return {"stage": "WAITING", "decision": "none", "frame_seq": seq, "policy": {"event_id": None}}

    async def end_session(self, *args, **kwargs):
        return None


class _Faces:
    def identify_faces(self, frame):
        return {"faces": [{"label": "pearl", "distance": 0.2, "box": [10, 10, 40, 40]}],
                "error": None, "saturated": False}


class _Emotion:
    def predict_crop(self, crop):
        return {"detected": True, "label": "happy", "probabilities": {"happy": 1.0}}


@pytest.fixture
def detector(monkeypatch):
    detector = _Detector()
    monkeypatch.setattr(service.IntakeDetectionService, "get_instance", classmethod(lambda cls: detector))
    monkeypatch.setattr(service.FaceRecognitionService, "get_instance", classmethod(lambda cls: _Faces()))
    monkeypatch.setattr(service.EmotionService, "get_instance", classmethod(lambda cls: _Emotion()))
    return detector


def _verify(state):
    state.identity_hits = 2
    state.verified_at = time.monotonic()
    state.target_box = [0.1, 0.1, 0.4, 0.4]


async def _feed(registry, state, fps, seconds, start_seq=1, start_ts=0.0):
    count = int(round(fps * seconds))
    for i in range(count):
        await registry.landmarks(state, _packet(start_seq + i, start_ts + i / fps))
    return start_seq + count - 1


class _Commits:
    def __init__(self):
        self.calls = []

    async def __call__(self, state, candidate, method):
        self.calls.append((candidate["event_id"], method))
        return {"event_id": candidate["event_id"], "status": "taken"}


def _candidate(frame_seq, decision="confirmed", event_id="evt"):
    return {"event_id": event_id, "decision": decision, "confidence": .9, "created_at": time.monotonic(),
            "frame_seq": frame_seq, "emotion_probabilities": None, "identity_distance": .2, "ready": False}


# ── FPS gate ──────────────────────────────────────────────────────────────────

def test_new_session_is_degraded_until_enough_capture_history(detector):
    async def scenario():
        registry = MonitorRegistry()
        state = registry.start(7, 11, "Pearl", "Pearl")
        assert state.public()["degraded"] is True
        await _feed(registry, state, 15, 0.5)
        assert state.public()["degraded"] is True          # span < 1 s
        await _feed(registry, state, 15, 1.0, start_seq=100, start_ts=0.5)
        public = state.public()
        assert public["degraded"] is False
        assert 14.0 <= public["landmark_fps"] <= 16.0
    asyncio.run(scenario())


@pytest.mark.parametrize("fps,degraded", [(8, True), (11, True), (15, False)])
def test_frame_rate_gate_thresholds(detector, fps, degraded):
    async def scenario():
        registry = MonitorRegistry()
        state = registry.start(7, 11, "Pearl", "Pearl")
        await _feed(registry, state, fps, 3.0)
        assert state.degraded is degraded
        assert state.public()["degraded"] is degraded
    asyncio.run(scenario())


def test_single_300ms_gap_degrades(detector):
    async def scenario():
        registry = MonitorRegistry()
        state = registry.start(7, 11, "Pearl", "Pearl")
        last = await _feed(registry, state, 15, 2.0)
        assert state.degraded is False
        await registry.landmarks(state, _packet(last + 1, (last - 1) / 15 + 0.3))
        assert state.degraded is True
    asyncio.run(scenario())


def test_backwards_capture_clock_fails_closed(detector):
    async def scenario():
        registry = MonitorRegistry()
        state = registry.start(7, 11, "Pearl", "Pearl")
        last = await _feed(registry, state, 15, 2.0, start_ts=10.0)
        assert state.degraded is False
        await registry.landmarks(state, _packet(last + 1, 1.0))
        assert state.degraded is True
    asyncio.run(scenario())


@pytest.mark.parametrize("fps", [8, 11])
def test_degraded_session_never_commits_and_downgrades_candidate(detector, fps):
    async def scenario():
        registry = MonitorRegistry()
        state = registry.start(7, 11, "Pearl", "Pearl")
        last = await _feed(registry, state, fps, 3.0)
        assert state.degraded
        _verify(state)
        state.candidate = _candidate(last)
        commits = _Commits()
        result = await registry.vision(state, last, _jpeg(), commits)
        assert commits.calls == []
        assert state.recorded is None
        assert result["candidate"]["ready"] is True
        assert result["candidate"]["decision"] == "uncertain"
        assert result["candidate"]["hold_reason"] == "degraded"
    asyncio.run(scenario())


def test_candidate_created_while_degraded_stays_held_after_recovery(detector):
    async def scenario():
        registry = MonitorRegistry()
        state = registry.start(7, 11, "Pearl", "Pearl")
        last = await _feed(registry, state, 8, 3.0)
        _verify(state)
        detector.script = {last + 1: ("confirmed", "evt-degraded")}
        await registry.landmarks(state, _packet(last + 1, (last - 1) / 8 + 1 / 8))
        assert state.candidate["event_id"] == "evt-degraded"
        # Rate recovers before the identity frame arrives: the event was still scored at 8 fps.
        state.degraded = False
        _verify(state)
        commits = _Commits()
        result = await registry.vision(state, last + 1, _jpeg(), commits)
        assert commits.calls == []
        assert result["candidate"]["decision"] == "uncertain"
    asyncio.run(scenario())


def test_healthy_browser_session_auto_commits_at_15_fps(detector):
    async def scenario():
        registry = MonitorRegistry()
        state = registry.start(7, 11, "Pearl", "Pearl")
        last = await _feed(registry, state, 15, 2.0)
        _verify(state)
        detector.script = {last + 1: ("confirmed", "evt-ok")}
        await registry.landmarks(state, _packet(last + 1, last / 15))
        assert state.candidate["decision"] == "confirmed"
        commits = _Commits()
        result = await registry.vision(state, last + 1, _jpeg(), commits)
        assert commits.calls == [("evt-ok", "auto")]
        assert result["recorded"]["status"] == "taken"
        assert result["mode"] == "observe"
    asyncio.run(scenario())


def test_auto_commit_off_holds_confirmed_candidate(detector):
    async def scenario():
        registry = MonitorRegistry()
        state = await registry.replace(7, 11, "Pearl", "Pearl", client_type="reachy", auto_commit=False)
        last = await _feed(registry, state, 15, 2.0)
        _verify(state)
        state.candidate = _candidate(last)
        commits = _Commits()
        result = await registry.vision(state, last, _jpeg(), commits)
        assert commits.calls == []
        assert result["candidate"]["decision"] == "uncertain"
        assert result["candidate"]["hold_reason"] == "auto_commit_off"
        assert result["auto_commit"] is False and result["client_type"] == "reachy"
    asyncio.run(scenario())


# ── Observe mode ─────────────────────────────────────────────────────────────

def test_observe_mode_requires_no_dose_and_cannot_auto_commit(detector):
    async def scenario():
        registry = MonitorRegistry()
        state = await registry.replace(7, None, "Pearl", "Pearl", mode="observe", client_type="reachy")
        assert state.intk_id is None and state.auto_commit is False
        public = state.public()
        assert public["mode"] == "observe" and public["intk_id"] is None
        with pytest.raises(ValueError):
            await registry.replace(7, None, "Pearl", "Pearl", mode="dose", client_type="reachy")
        with pytest.raises(ValueError):
            await registry.replace(7, 11, "Pearl", "Pearl", mode="chat", client_type="reachy")
    asyncio.run(scenario())


@pytest.mark.parametrize("decisions", list(itertools.product(("none", "uncertain", "confirmed"), repeat=2)))
def test_observe_mode_never_commits_for_any_detector_output(detector, decisions):
    async def scenario():
        registry = MonitorRegistry()
        state = await registry.replace(7, None, "Pearl", "Pearl", mode="observe", client_type="reachy",
                                       auto_commit=True)
        last = await _feed(registry, state, 15, 2.0)
        _verify(state)
        detector.script = {last + 1 + i: (decision, f"evt-{i}") for i, decision in enumerate(decisions)}
        commits = _Commits()
        for i in range(len(decisions)):
            seq = last + 1 + i
            await registry.landmarks(state, _packet(seq, (seq - 1) / 15))
            _verify(state)
            await registry.vision(state, seq, _jpeg(), commits)
        # Even a candidate forced into the session cannot reach the commit path.
        seq = last + len(decisions) + 1
        await registry.landmarks(state, _packet(seq, (seq - 1) / 15))
        _verify(state)
        state.candidate = _candidate(seq)
        await registry.vision(state, seq, _jpeg(), commits)
        assert commits.calls == []
        assert state.recorded is None
        expected = [f"evt-{i}" for i, d in enumerate(decisions) if d != "none"]
        assert [event["event_id"] for event in state.extra_events] == expected
    asyncio.run(scenario())


def test_after_record_session_switches_to_observe_and_reports_extra_events(detector):
    async def scenario():
        registry = MonitorRegistry()
        state = registry.start(7, 11, "Pearl", "Pearl")
        last = await _feed(registry, state, 15, 2.0)
        _verify(state)
        detector.script = {last + 1: ("confirmed", "evt-dose"), last + 2: ("uncertain", "evt-extra-1"),
                           last + 3: ("confirmed", "evt-extra-2"), last + 4: ("confirmed", "evt-extra-2")}
        commits = _Commits()
        await registry.landmarks(state, _packet(last + 1, last / 15))
        await registry.vision(state, last + 1, _jpeg(), commits)
        assert commits.calls == [("evt-dose", "auto")]
        assert state.mode == "observe"
        for seq in (last + 2, last + 3, last + 4):
            _verify(state)
            result = await registry.landmarks(state, _packet(seq, (seq - 1) / 15))
            await registry.vision(state, seq, _jpeg(), commits)
        assert commits.calls == [("evt-dose", "auto")]            # no second commit, no stock change
        assert state.candidate["event_id"] == "evt-dose"            # extra events never become candidates
        assert [e["event_id"] for e in result["extra_events"]] == ["evt-extra-1", "evt-extra-2"]
        assert set(result["extra_events"][0]) == {"event_id", "decision", "confidence", "frame_seq"}
    asyncio.run(scenario())


def test_extra_events_are_capped(detector):
    async def scenario():
        registry = MonitorRegistry()
        state = await registry.replace(7, None, "Pearl", "Pearl", mode="observe", client_type="reachy")
        last = await _feed(registry, state, 15, 2.0)
        detector.script = {last + 1 + i: ("uncertain", f"evt-{i}") for i in range(30)}
        for i in range(30):
            _verify(state)
            seq = last + 1 + i
            await registry.landmarks(state, _packet(seq, (seq - 1) / 15))
        assert len(state.extra_events) == service.EXTRA_EVENT_CAP == 20
        assert state.extra_events[-1]["event_id"] == "evt-29"
    asyncio.run(scenario())


# ── Concurrency ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("first,second", [("browser", "reachy"), ("reachy", "browser")])
def test_live_session_of_other_client_blocks_start(detector, first, second):
    async def scenario():
        registry = MonitorRegistry()
        holder = await registry.replace(7, 11, "Pearl", "Pearl", client_type=first,
                                        auto_commit=first == "browser")
        await registry.landmarks(holder, _packet(1, 0.0))
        with pytest.raises(BusyOtherClient):
            await registry.replace(7, 11, "Pearl", "Pearl", client_type=second)
        assert registry.by_user[7] == holder.session_id and not holder.ended
    asyncio.run(scenario())


def test_just_started_session_blocks_other_client_before_first_landmark(detector):
    async def scenario():
        registry = MonitorRegistry()
        holder = await registry.replace(7, 11, "Pearl", "Pearl", client_type="reachy", auto_commit=False)
        with pytest.raises(BusyOtherClient):
            await registry.replace(7, 11, "Pearl", "Pearl", client_type="browser")
        assert registry.by_user[7] == holder.session_id
    asyncio.run(scenario())


def test_concurrent_starts_of_both_clients_yield_one_session(detector):
    async def scenario():
        registry = MonitorRegistry()
        stale = registry.start(7, 11, "Pearl", "Pearl")
        stale.last_activity_at -= 10
        results = await asyncio.gather(
            registry.replace(7, 11, "Pearl", "Pearl", client_type="browser"),
            registry.replace(7, 11, "Pearl", "Pearl", client_type="reachy"),
            return_exceptions=True)
        busy = [r for r in results if isinstance(r, BusyOtherClient)]
        sessions = [r for r in results if not isinstance(r, BaseException)]
        assert len(busy) == 1 and len(sessions) == 1
        live = [s for s in registry.sessions.values() if not s.ended]
        assert live == sessions
    asyncio.run(scenario())


def test_stale_other_client_session_is_evicted_after_5s_silence(detector):
    async def scenario():
        registry = MonitorRegistry()
        old = await registry.replace(7, 11, "Pearl", "Pearl", client_type="reachy", auto_commit=False)
        await registry.landmarks(old, _packet(1, 0.0))
        old.last_activity_at = time.monotonic() - 5.5
        new = await registry.replace(7, 11, "Pearl", "Pearl", client_type="browser")
        assert old.ended and registry.by_user[7] == new.session_id
    asyncio.run(scenario())


def test_same_client_replaces_live_session(detector):
    async def scenario():
        registry = MonitorRegistry()
        old = await registry.replace(7, 11, "Pearl", "Pearl", client_type="reachy", auto_commit=False)
        await registry.landmarks(old, _packet(1, 0.0))
        new = await registry.replace(7, 12, "Pearl", "Pearl", client_type="reachy", auto_commit=False)
        assert old.ended and registry.by_user[7] == new.session_id
    asyncio.run(scenario())


def test_get_rejects_session_of_other_client_type(detector):
    async def scenario():
        registry = MonitorRegistry()
        state = await registry.replace(7, 11, "Pearl", "Pearl", client_type="reachy", auto_commit=False)
        assert registry.get(7, state.session_id, state.generation, client_type="reachy") is state
        with pytest.raises(ValueError):
            registry.get(7, state.session_id, state.generation, client_type="browser")
    asyncio.run(scenario())


def test_public_exposes_new_fields(detector):
    state = MonitorRegistry().start(7, 11, "Pearl", "Pearl")
    public = state.public()
    for key in ("mode", "client_type", "auto_commit", "degraded", "landmark_fps", "extra_events"):
        assert key in public
    assert public["mode"] == "dose" and public["client_type"] == "browser" and public["auto_commit"] is True
