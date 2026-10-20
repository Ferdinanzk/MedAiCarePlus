"""Quick fix 7: the camera's intake detector re-arms after a candidate nothing used (contract CONTRACT7.md).

A dose session's detector latches after its one candidate (intake_temporal.py, "stay latched"). The robot reads a
candidate only in WATCHING, after its ~10-11 s medicine prompt, and the server dropped any candidate after 5 s: a
patient who took the pill during the prompt left the detector latched, and every later gesture was ignored. Now a
ready robot candidate waits READY_CANDIDATE_SECONDS from its creation, and one that expires unused starts a fresh
detector session. These tests run the real IntakeDetectionService and TemporalIntakePipeline; only the landmark
geometry is replaced (a hand's distance to the mouth is written in its x coordinate)."""

import asyncio
import logging
import sys
import types

import cv2
import numpy as np
import pytest

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app.services import intake_detection  # noqa: E402
from app.services import monitor_service as service  # noqa: E402
from app.services.intake_temporal import Observation  # noqa: E402
from app.services.monitor_service import MonitorRegistry  # noqa: E402

FPS = 15
# One hand-to-mouth gesture, a distance to the mouth (in mouth widths) per frame at 15 fps: rest, approach,
# contact, withdrawal, rest. The real pipeline makes one confirmed candidate of it, at the withdrawal.
GESTURE = ([2.0] * 3 + [2.0 - 1.5 * k / 9 for k in range(1, 10)] + [0.5] * 5
           + [0.5 + 1.5 * k / 6 for k in range(1, 7)] + [2.0] * 3)


def _jpeg():
    ok, encoded = cv2.imencode(".jpg", np.zeros((100, 100, 3), dtype=np.uint8))
    assert ok
    return encoded.tobytes()


JPEG = _jpeg()


def _packet(frame_seq, timestamp, distance=None):
    # The hand sits below the mouth (y 0.6), so the face is never "covered" and a candidate can become ready.
    hand = None if distance is None else [distance / 4, 0.6]
    return {"frame_seq": frame_seq, "timestamp": float(timestamp), "width": 100, "height": 100,
            "faces": [{"box": [0.1, 0.1, 0.4, 0.4], "points": [[0.3, 0.3]] * 9}],
            "poses": [{"nose": [0.3, 0.3], "shoulders": [[0.2, 0.5], [0.4, 0.5]],
                       "wrists": [] if hand is None else [[hand[0], hand[1], 0.9]]}],
            "hands": [] if hand is None else [[hand] * 13]}


def _observations(detector, face_landmarks, hand_landmarks, width, height, **kwargs):
    """build_temporal_observations with the geometry read from the packet: mouth open, a pinch, distance = 4 x."""
    geometry = {"mouth_open": True, "mouth_open_ratio": .3, "mouth_open_baseline": .1, "mouth_open_delta": .2,
                "mouth_opened_at": None, "mouth_motion_cycles": 0, "tongue_score": None, "tongue_quality": 0.0,
                "tongue_frame_support": False}
    return geometry, [Observation(center=(hand[0]["x"], hand[0]["y"]), distance=hand[0]["x"] * 4, mouth_open=True,
                                  pinch=True, flat_palm=False, occlusion=0.0) for hand in hand_landmarks]


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t


class _Faces:
    def identify_faces(self, frame):
        return {"faces": [{"label": "pearl", "distance": 0.2, "box": [10, 10, 40, 40]}],
                "error": None, "saturated": False}


class _Emotion:
    def predict_crop(self, crop):
        return {"detected": True, "label": "happy", "probabilities": {"happy": 1.0}}


class _Commits:
    def __init__(self):
        self.calls = []

    async def __call__(self, state, candidate, method):
        self.calls.append((candidate["event_id"], method))
        return {"event_id": candidate["event_id"], "status": "taken"}


@pytest.fixture
def clock(monkeypatch):
    clock = _Clock()
    detector = intake_detection.IntakeDetectionService(clock=lambda: clock.t)
    monkeypatch.setattr(service, "time", clock)
    monkeypatch.setattr(intake_detection, "build_temporal_observations", _observations)
    monkeypatch.setattr(service.IntakeDetectionService, "get_instance", classmethod(lambda cls: detector))
    monkeypatch.setattr(service.FaceRecognitionService, "get_instance", classmethod(lambda cls: _Faces()))
    monkeypatch.setattr(service.EmotionService, "get_instance", classmethod(lambda cls: _Emotion()))
    clock.detector = detector
    return clock


class _Run:
    """Feeds one session at 15 fps on the fake clock, its identity kept verified, a vision call on every frame."""

    def __init__(self, clock, client_type="reachy", auto_commit=False):
        self.clock = clock
        self.registry = MonitorRegistry()
        self.state = self.registry.start(7, 11, "Pearl", "Pearl", client_type=client_type, auto_commit=auto_commit)
        self.commits = _Commits()
        self.seq = 0
        self.candidates = []   # event ids, in the order they appeared

    async def frame(self, distance=None, vision=True, verified=True):
        self.seq += 1
        self.clock.t += 1 / FPS
        state = self.state
        if verified:
            state.identity_hits, state.verified_at = 2, self.clock.t
            state.target_box = [0.1, 0.1, 0.4, 0.4]
        else:
            state.identity_hits, state.target_box = 0, None
        result = await self.registry.landmarks(state, _packet(self.seq, self.clock.t, distance))
        if vision and verified:
            result = await self.registry.vision(state, self.seq, JPEG, self.commits)
        candidate = state.candidate
        if candidate and (not self.candidates or self.candidates[-1] != candidate["event_id"]):
            self.candidates.append(candidate["event_id"])
        return result

    async def rest(self, seconds, distance=2.0, vision=True):
        for _ in range(int(round(seconds * FPS))):
            await self.frame(distance, vision=vision)

    async def gesture(self, vision=True):
        for distance in GESTURE:
            await self.frame(distance, vision=vision)


def _plant(state, clock, ready):
    state.candidate = {"event_id": "evt-planted", "decision": "uncertain", "confidence": .48,
                       "created_at": clock.t, "frame_seq": state.last_frame_seq, "emotion_probabilities": None,
                       "identity_distance": .2, "degraded": False, "evidence": {}, "ready": ready}


# ── (a) the re-intake case ────────────────────────────────────────────────────

def test_a_candidate_nobody_read_re_arms_the_detector_and_the_next_gesture_makes_a_second(clock):
    """The patient takes the pill during Reachy's prompt: a ready candidate nothing reads. After it expires, the
    detector starts afresh (a new detector session), and the gesture the patient makes in WATCHING is seen."""
    async def scenario():
        run = _Run(clock)
        await run.rest(1.0)
        await run.gesture()
        first = run.state.candidate
        assert first is not None and first["ready"] and run.candidates == [first["event_id"]]
        detector_session = run.state.detector_session_id
        await run.rest(15.2)
        assert run.state.candidate is None
        assert run.state.detector_session_id != detector_session      # a fresh detector session
        await run.rest(1.0)
        await run.gesture()
        assert len(run.candidates) == 2 and run.candidates[1] != run.candidates[0]
        assert run.state.candidate["event_id"] == run.candidates[1]
        assert run.commits.calls == []                                 # auto_commit off: both wait for the robot
    asyncio.run(scenario())


def test_an_unready_candidate_also_re_arms_once_its_5_s_are_over(clock):
    """A candidate that never became ready (no fresh identity match) still goes after CANDIDATE_TIMEOUT_SECONDS,
    and since nothing used it, the next gesture makes a new one."""
    async def scenario():
        run = _Run(clock)
        await run.rest(1.0, vision=False)
        await run.gesture(vision=False)
        assert run.state.candidate is not None and not run.state.candidate["ready"]
        await run.rest(5.2, vision=False)
        assert run.state.candidate is None
        await run.rest(1.0, vision=False)
        await run.gesture(vision=False)
        assert len(run.candidates) == 2
    asyncio.run(scenario())


def test_a_gesture_while_a_candidate_waits_is_still_ignored(clock):
    """Detection stays paused while a candidate exists, as before: no event from frames the detector never got."""
    async def scenario():
        run = _Run(clock)
        await run.rest(1.0)
        await run.gesture()
        detector_session = run.state.detector_session_id
        await run.gesture()
        await run.gesture()
        assert len(run.candidates) == 1 and run.state.detector_session_id == detector_session
    asyncio.run(scenario())


# ── (b) how long a candidate is kept ──────────────────────────────────────────

@pytest.mark.parametrize("ready, kept, gone", [(True, 14.9, 15.1), (False, 4.9, 5.1)])
def test_a_ready_robot_candidate_is_kept_15_s_and_an_unready_one_5_s(clock, ready, kept, gone):
    async def scenario():
        registry = MonitorRegistry()
        state = registry.start(7, 11, "Pearl", "Pearl", client_type="reachy", auto_commit=False)
        _plant(state, clock, ready)
        created = clock.t
        clock.t = created + kept
        await registry.landmarks(state, _packet(state.last_frame_seq + 1, clock.t))
        assert state.candidate is not None and state.candidate["event_id"] == "evt-planted"
        clock.t = created + gone
        await registry.landmarks(state, _packet(state.last_frame_seq + 1, clock.t))
        assert state.candidate is None
    asyncio.run(scenario())


def test_the_ready_window_is_measured_from_creation_not_from_ready(clock):
    async def scenario():
        registry = MonitorRegistry()
        state = registry.start(7, 11, "Pearl", "Pearl", client_type="reachy", auto_commit=False)
        _plant(state, clock, ready=False)
        created = clock.t
        clock.t = created + 4.0
        state.candidate["ready"] = True          # ready 4 s after it was made
        clock.t = created + 15.1
        await registry.landmarks(state, _packet(state.last_frame_seq + 1, clock.t))
        assert state.candidate is None
    asyncio.run(scenario())


def test_a_ready_browser_candidate_keeps_its_5_s(clock):
    """Browser sessions are unchanged apart from the re-arm: their ready candidate still goes after 5 s."""
    async def scenario():
        registry = MonitorRegistry()
        state = registry.start(7, 11, "Pearl", "Pearl", client_type="browser", auto_commit=False)
        _plant(state, clock, ready=True)
        created = clock.t
        clock.t = created + 4.9
        await registry.landmarks(state, _packet(state.last_frame_seq + 1, clock.t))
        assert state.candidate is not None
        clock.t = created + 5.1
        await registry.landmarks(state, _packet(state.last_frame_seq + 1, clock.t))
        assert state.candidate is None
    asyncio.run(scenario())


def test_vision_drops_an_unready_candidate_after_5_s_and_re_arms(clock):
    """vision() checks an unready candidate's 5 s again after its identity call: the same drop, the same re-arm."""
    async def scenario():
        registry = MonitorRegistry()
        state = registry.start(7, 11, "Pearl", "Pearl", client_type="reachy", auto_commit=False)
        await registry.landmarks(state, _packet(1, clock.t))
        _plant(state, clock, ready=False)
        detector_session = state.detector_session_id
        original = _Faces.identify_faces

        def slow(self, frame):     # the identity call is where the time passes
            clock.t += 5.1
            return original(self, frame)

        _Faces.identify_faces = slow
        try:
            state.identity_hits, state.verified_at, state.target_box = 2, clock.t, [0.1, 0.1, 0.4, 0.4]
            await registry.vision(state, 1, JPEG, _Commits())
        finally:
            _Faces.identify_faces = original
        assert state.candidate is None and state.detector_session_id != detector_session
    asyncio.run(scenario())


# ── (c) a used candidate keeps the latch ──────────────────────────────────────

def test_a_candidate_the_robots_request_used_keeps_the_latch(clock):
    """The robot filed its confirmation request with the candidate's event: when the candidate goes, the detector
    stays latched, so the same long gesture (the hand back to the mouth, again and again) gives no second event."""
    async def scenario():
        run = _Run(clock)
        await run.rest(1.0)
        await run.gesture()
        event_id = run.state.candidate["event_id"]
        assert service.mark_candidate_used(run.state, event_id, "confirmation") is True
        detector_session = run.state.detector_session_id
        await run.rest(15.2)
        assert run.state.candidate is None and run.state.detector_session_id == detector_session
        for _ in range(3):
            await run.gesture()
        assert run.candidates == [event_id]
    asyncio.run(scenario())


def test_an_auto_commit_uses_its_candidate_and_one_long_gesture_gives_one_event(clock):
    """A healthy browser session auto-commits: the record used the candidate. The session then observes, and the
    rest of the same long gesture gives no extra event, before or after the candidate goes."""
    async def scenario():
        run = _Run(clock, client_type="browser", auto_commit=True)
        await run.rest(1.5)
        await run.gesture()
        assert [method for _, method in run.commits.calls] == ["auto"]
        assert run.state.mode == "observe" and run.state.candidate_used == "auto_commit"
        detector_session = run.state.detector_session_id
        for _ in range(3):
            await run.gesture()
        await run.rest(5.2)
        assert run.state.candidate is None and run.state.detector_session_id == detector_session
        await run.gesture()
        assert len(run.commits.calls) == 1 and run.state.extra_events == []
    asyncio.run(scenario())


def test_marking_names_the_current_candidate_only(clock):
    async def scenario():
        registry = MonitorRegistry()
        state = registry.start(7, 11, "Pearl", "Pearl", client_type="reachy", auto_commit=False)
        assert service.mark_candidate_used(state, "evt-planted", "confirmation") is False    # none yet
        _plant(state, clock, ready=True)
        assert service.mark_candidate_used(state, "evt-other", "confirmation") is False
        assert state.candidate_used is None
        assert service.mark_candidate_used(state, "evt-planted", "confirmation") is True
        assert service.mark_candidate_used(state, "evt-planted", "auto_commit") is True       # the first use stays
        assert state.candidate_used == "confirmation"
    asyncio.run(scenario())


def test_a_new_candidate_starts_unused(clock):
    async def scenario():
        run = _Run(clock)
        await run.rest(1.0)
        await run.gesture()
        service.mark_candidate_used(run.state, run.state.candidate["event_id"], "confirmation")
        run.state.candidate = None       # e.g. the browser's "not taken"; the detector is still latched
        await run.registry.end(run.state)
        run2 = _Run(clock)
        await run2.rest(1.0)
        await run2.gesture()
        assert run2.state.candidate_used is None
    asyncio.run(scenario())


# ── (d) the log lines ─────────────────────────────────────────────────────────

def _lines(caplog, text):
    return [record.getMessage() for record in caplog.records
            if record.name == "app.services.monitor_service" and text in record.getMessage()]


def test_each_step_of_a_candidate_is_logged_once(clock, caplog):
    caplog.set_level(logging.INFO, logger="app.services.monitor_service")

    async def scenario():
        run = _Run(clock)
        await run.rest(1.0)
        await run.gesture()
        await run.rest(15.2)
        await run.rest(1.0)
        await run.gesture()
        return run

    run = asyncio.run(scenario())
    first, second = run.candidates
    created = _lines(caplog, ": created at frame_seq=")
    assert len(created) == 2 and first in created[0] and second in created[1]
    assert "decision=uncertain" in created[0] and "confidence=0.82" in created[0]   # held: auto-record is off
    assert len(_lines(caplog, f"intake candidate {first} of session")) == 3          # created, ready, expired
    ready = _lines(caplog, ": ready after ")
    assert len(ready) == 2 and first in ready[0]
    expired = _lines(caplog, ": expired unread")
    assert len(expired) == 1 and first in expired[0] and "ready=True" in expired[0] and "age 15.0 s" in expired[0]
    assert len(_lines(caplog, "re-armed")) == 1
    assert _lines(caplog, ": used by ") == []


def test_a_used_candidate_logs_its_use_and_no_expiry(clock, caplog):
    caplog.set_level(logging.INFO, logger="app.services.monitor_service")

    async def scenario():
        run = _Run(clock)
        await run.rest(1.0)
        await run.gesture()
        event_id = run.state.candidate["event_id"]
        service.mark_candidate_used(run.state, event_id, "confirmation")
        service.mark_candidate_used(run.state, event_id, "confirmation")      # a retried request
        await run.rest(15.2)
        return event_id

    event_id = asyncio.run(scenario())
    used = _lines(caplog, ": used by ")
    assert len(used) == 1 and event_id in used[0] and used[0].endswith("used by confirmation")
    assert _lines(caplog, ": expired unread") == [] and _lines(caplog, "re-armed") == []


def test_a_timeout_reset_is_logged_once_with_its_stage(clock, caplog):
    """An approach that never reaches the mouth: the pipeline gives up after its 4.5 s (event_timeout)."""
    caplog.set_level(logging.INFO, logger="app.services.monitor_service")

    async def scenario():
        run = _Run(clock)
        await run.rest(1.0)
        for distance in (1.6, 1.4, 1.2, 1.05, 1.0):
            await run.frame(distance)
        await run.rest(6.0, distance=1.0)
        return run

    run = asyncio.run(scenario())
    resets = _lines(caplog, ": reset at stage ")
    assert resets == [f"intake detector of session {run.state.session_id}: reset at stage APPROACHING "
                      f"(event_timeout)"]
    assert run.candidates == []


def test_a_gate_reset_is_logged_once_with_its_stage(clock, caplog):
    """Identity lost in the middle of a gesture: the monitor ends that detector session (the identity gate)."""
    caplog.set_level(logging.INFO, logger="app.services.monitor_service")

    async def scenario():
        run = _Run(clock)
        await run.rest(1.0)
        for distance in GESTURE[:15]:     # into the contact zone
            await run.frame(distance)
        assert run.state.latest_detector["stage"] in ("AT_MOUTH", "APPROACHING")
        stage = run.state.latest_detector["stage"]
        for _ in range(5):
            await run.frame(0.5, verified=False)
        return run, stage

    run, stage = asyncio.run(scenario())
    resets = _lines(caplog, ": reset at stage ")
    assert resets == [f"intake detector of session {run.state.session_id}: reset at stage {stage} (identity)"]
