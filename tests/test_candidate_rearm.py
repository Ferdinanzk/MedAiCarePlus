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


def _packet(frame_seq, timestamp, distance=None, owned=True, front=None, held=None):
    # The hand sits below the mouth (y 0.6), so the face is never "covered" and a candidate can become ready.
    # owned=False: no body for the face, so select_owned_observations can't tie the hand to the patient.
    # front: the depth model's reading of the hand against the face (a robot frame's hand_depth).
    # held: what the object detector saw in the hand (hand_objects): a label, or "" for a checked empty hand.
    hand = None if distance is None else [distance / 4, 0.6]
    packet = {"frame_seq": frame_seq, "timestamp": float(timestamp), "width": 100, "height": 100,
              "faces": [{"box": [0.1, 0.1, 0.4, 0.4], "points": [[0.3, 0.3]] * 9}],
              "poses": [{"nose": [0.3, 0.3], "shoulders": [[0.2, 0.5], [0.4, 0.5]],
                         "wrists": [] if hand is None else [[hand[0], hand[1], 0.9]]}] if owned else [],
              "hands": [] if hand is None else [[hand] * 13]}
    if front is not None and hand is not None:
        packet["hand_depth"] = [{"face": 0, "front": front}]
    if held is not None and hand is not None:
        packet["hand_objects"] = [{"label": held or None}]
    return packet


def _observations(detector, face_landmarks, hand_landmarks, width, height, hand_depth=None, hand_objects=None,
                  **kwargs):
    """build_temporal_observations with the geometry read from the packet: mouth open, a pinch, distance = 4 x,
    the depth reading's `front` and the object detector's label when the packet carried them."""
    geometry = {"mouth_open": True, "mouth_open_ratio": .3, "mouth_open_baseline": .1, "mouth_open_delta": .2,
                "mouth_opened_at": None, "mouth_motion_cycles": 0, "tongue_score": None, "tongue_quality": 0.0,
                "tongue_frame_support": False}
    return geometry, [Observation(center=(hand[0]["x"], hand[0]["y"]), distance=hand[0]["x"] * 4, mouth_open=True,
                                  pinch=True, flat_palm=False, occlusion=0.0,
                                  front=(hand_depth[index] or {}).get("front") if hand_depth else None,
                                  object_checked=bool(hand_objects) and isinstance(hand_objects[index], dict),
                                  held_object=(hand_objects[index] or {}).get("label") if hand_objects else None)
                      for index, hand in enumerate(hand_landmarks)]


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

    async def frame(self, distance=None, vision=True, verified=True, owned=True, front=None, held=None):
        self.seq += 1
        self.clock.t += 1 / FPS
        state = self.state
        if verified:
            state.identity_hits, state.verified_at = 2, self.clock.t
            state.target_box = [0.1, 0.1, 0.4, 0.4]
        else:
            state.identity_hits, state.target_box = 0, None
        result = await self.registry.landmarks(state, _packet(self.seq, self.clock.t, distance, owned, front, held))
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


def test_a_request_that_names_a_just_expired_candidate_logs_its_late_use(clock, caplog):
    """The robot read the candidate at about 14.9 s and its request came after the 15.0 s expiry: the log says so."""
    caplog.set_level(logging.INFO, logger="app.services.monitor_service")

    async def scenario():
        run = _Run(clock)
        await run.rest(1.0)
        await run.gesture()
        event_id = run.state.candidate["event_id"]
        await run.rest(15.2)
        assert run.state.candidate is None
        assert service.mark_candidate_used(run.state, event_id, "confirmation") is False
        assert service.mark_candidate_used(run.state, event_id, "confirmation") is False   # a retried request
        assert service.mark_candidate_used(run.state, "evt-other", "confirmation") is False
        return run, event_id

    run, event_id = asyncio.run(scenario())
    late = _lines(caplog, "after it expired")
    assert late == [f"intake candidate {event_id} of session {run.state.session_id}: used by confirmation "
                    f"after it expired"]
    assert run.state.candidate_used is None


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
    """Identity lost in the middle of a gesture for longer than OWNERSHIP_GRACE_SECONDS: the monitor ends that
    detector session (the identity gate), once, at the stage the gap began."""
    caplog.set_level(logging.INFO, logger="app.services.monitor_service")

    async def scenario():
        run = _Run(clock)
        await run.rest(1.0)
        for distance in GESTURE[:15]:     # into the contact zone
            await run.frame(distance)
        assert run.state.latest_detector["stage"] in ("AT_MOUTH", "APPROACHING")
        stage = run.state.latest_detector["stage"]
        detector_session = run.state.detector_session_id
        for _ in range(5):                # 0.33 s: within the grace, the event is kept
            await run.frame(0.5, verified=False)
        assert run.state.latest_detector["stage"] == "WAITING_FOR_PEARL"
        assert run.state.detector_session_id == detector_session
        for _ in range(10):               # 1.0 s: past it
            await run.frame(0.5, verified=False)
        assert run.state.detector_session_id != detector_session
        return run, stage

    run, stage = asyncio.run(scenario())
    resets = _lines(caplog, ": reset at stage ")
    assert resets == [f"intake detector of session {run.state.session_id}: reset at stage {stage} (identity)"]


def test_a_short_gap_in_ownership_keeps_the_event_but_never_auto_logs_it(clock):
    """The hand's owner is unclear for 0.27 s while the hand is at the mouth (the frames where it crosses the face,
    on the C:\\pilldetector clips): the event survives, the detector still rates it confirmed, and the policy caps it
    at uncertain, so an auto-commit session makes a prompt instead of a record. Without the gap it auto-commits."""
    async def scenario():
        gapped = _Run(clock, auto_commit=True)
        await gapped.rest(1.0)
        for distance in GESTURE[:15]:
            await gapped.frame(distance)
        detector_session = gapped.state.detector_session_id
        for _ in range(4):
            await gapped.frame(0.5, owned=False)
        assert gapped.state.detector_session_id == detector_session     # no reset
        for distance in GESTURE[15:]:
            await gapped.frame(distance)
        summary = await clock.detector.get_session(7, gapped.state.detector_session_id)

        plain = _Run(clock, auto_commit=True)
        await plain.rest(1.0)
        await plain.gesture()
        return gapped, summary, plain

    gapped, summary, plain = asyncio.run(scenario())
    latest = summary["latest_result"]
    assert latest["policy"]["detector_band"] == "confirmed"
    assert (latest["decision"], latest["decision_reason"]) == ("uncertain", "ownership_gap_needs_confirmation")
    assert gapped.state.candidate is not None and gapped.state.candidate["decision"] == "uncertain"
    assert gapped.commits.calls == []
    assert plain.state.candidate is not None and plain.state.candidate["decision"] == "confirmed"
    assert [method for _, method in plain.commits.calls] == ["auto"]


def test_a_gap_in_ownership_past_the_grace_resets_and_the_rest_of_the_gesture_makes_nothing(clock, caplog):
    """The same gesture with 0.6 s of unclear ownership at the mouth: the detector session is reset (the ownership
    gate), and the withdrawal alone is not an event."""
    caplog.set_level(logging.INFO, logger="app.services.monitor_service")

    async def scenario():
        run = _Run(clock, auto_commit=True)
        await run.rest(1.0)
        for distance in GESTURE[:15]:
            await run.frame(distance)
        stage = run.state.latest_detector["stage"]
        for _ in range(9):
            await run.frame(0.5, owned=False)
        for distance in GESTURE[15:]:
            await run.frame(distance)
        await run.rest(1.0)
        return run, stage

    run, stage = asyncio.run(scenario())
    resets = _lines(caplog, ": reset at stage ")
    assert resets == [f"intake detector of session {run.state.session_id}: reset at stage {stage} (ownership)"]
    assert run.candidates == [] and run.commits.calls == []


def _closed_mouth_observations(detector, face_landmarks, hand_landmarks, width, height, **kwargs):
    geometry, observations = _observations(detector, face_landmarks, hand_landmarks, width, height, **kwargs)
    geometry["mouth_open"] = False
    for observation in observations:
        observation.mouth_open = False
    return geometry, observations


def test_a_closed_mouth_rejection_is_logged_once_with_its_stage(clock, caplog, monkeypatch):
    """A whole gesture with the mouth shut: the pipeline rejects it at the withdrawal and makes no candidate."""
    caplog.set_level(logging.INFO, logger="app.services.monitor_service")
    monkeypatch.setattr(intake_detection, "build_temporal_observations", _closed_mouth_observations)

    async def scenario():
        run = _Run(clock)
        await run.rest(1.0)
        await run.gesture()
        await run.rest(1.0)
        return run

    run = asyncio.run(scenario())
    resets = _lines(caplog, ": reset at stage ")
    prefix = f"intake detector of session {run.state.session_id}: reset at stage "
    assert len(resets) == 1, resets
    assert resets[0] in (f"{prefix}AT_MOUTH (closed_mouth_sequence_rejected)",
                         f"{prefix}WITHDRAWING (closed_mouth_sequence_rejected)")
    assert run.candidates == [] and run.state.latest_detector["stage"] == "READY"


def test_fingers_the_depth_model_saw_in_front_of_the_face_make_a_prompt_not_a_record(clock):
    """The robot's frames carry the depth model's reading (hand_depth, services/landmarks/depth.py). Fingers in
    front of the face through the contact (front 0.9, against the 0.8 threshold) keep the detector's confirmed
    band, and the policy caps the event at uncertain, so an auto-commit session prompts instead of recording. The
    same gesture read at the lips (front 0.05) records as before."""
    async def scenario():
        runs = {}
        for name, front in (("in_front", 0.9), ("at_lips", 0.05)):
            run = _Run(clock, auto_commit=True)
            await run.rest(1.0)
            for distance in GESTURE:
                await run.frame(distance, front=front)
            runs[name] = (run, await clock.detector.get_session(7, run.state.detector_session_id))
        return runs

    runs = asyncio.run(scenario())
    run, summary = runs["in_front"]
    latest = summary["latest_result"]
    assert latest["policy"]["detector_band"] == "confirmed" and latest["depth_no_contact"] is True
    assert (latest["decision"], latest["decision_reason"]) == ("uncertain", "depth_no_contact_needs_confirmation")
    assert run.state.candidate["decision"] == "uncertain" and run.commits.calls == []
    assert run.state.candidate["evidence"]["depth"]["depth_no_contact"] is True
    run, summary = runs["at_lips"]
    assert summary["latest_result"]["depth_no_contact"] is False
    assert run.state.candidate["decision"] == "confirmed" and [method for _, method in run.commits.calls] == ["auto"]


def test_a_phone_in_the_hand_makes_a_prompt_or_nothing_and_a_cup_changes_nothing(clock, monkeypatch):
    """The robot's frames carry what the object detector saw in the hand (hand_objects,
    services/landmarks/objects.py). A phone held through the contact keeps the detector's confirmed band, and the
    policy caps the event at uncertain (INTAKE_OBJECT_VETO=prompt, the default) so an auto-commit session prompts
    instead of recording; with INTAKE_OBJECT_VETO=drop the gesture makes no candidate at all. A cup is not a veto
    object: the gesture records as before."""
    from app.intake_v1 import config as policy_config

    async def scenario(held):
        run = _Run(clock, auto_commit=True)
        await run.rest(1.0)
        for distance in GESTURE:
            await run.frame(distance, held=held)
        return run, await clock.detector.get_session(7, run.state.detector_session_id)

    run, summary = asyncio.run(scenario("cell phone"))
    latest = summary["latest_result"]
    assert latest["policy"]["detector_band"] == "confirmed" and latest["object_in_hand"] == "cell phone"
    assert (latest["decision"], latest["decision_reason"]) == ("uncertain", "object_in_hand_needs_confirmation")
    assert run.state.candidate["decision"] == "uncertain" and run.commits.calls == []
    assert run.state.candidate["evidence"]["objects"]["object_in_hand"] == "cell phone"

    run, summary = asyncio.run(scenario("cup"))
    assert summary["latest_result"]["object_in_hand"] == "cup"
    assert run.state.candidate["decision"] == "confirmed" and [method for _, method in run.commits.calls] == ["auto"]

    monkeypatch.setattr(policy_config, "OBJECT_VETO", policy_config.OBJECT_VETO_DROP)
    run, summary = asyncio.run(scenario("cell phone"))
    # The gesture completed (the detector is in its cooldown) and the policy dropped it: no candidate, so the
    # session kept detecting and the latest result is a later idle frame.
    assert run.state.latest_detector["stage"] == "COOLDOWN"
    assert run.candidates == [] and run.state.candidate is None and run.commits.calls == []
