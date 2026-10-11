"""The search before talking verifies the patient with the OpenVINO match alone; identity is logged.

A robot session that started in observe mode (the search before a check-in or a dose prompt) no longer needs
MediaPipe's short-range face detector to see the face too: it misses faces under about 50 px wide (beyond about
1.35 m from the robot). Every other session keeps the MediaPipe gate. Robot sessions log each identity outcome,
rate-limited, with numbers only (spec 2026-10-11, B1 and B2).
"""

import asyncio
import logging

import pytest

from tests.test_monitor_backend import _Detector, _Emotion, _jpeg, _packet
from tests.test_monitor_modes import _Commits, _Detector as _ScriptedDetector, _feed, _verify
from tests.test_monitor_modes import _packet as _timed_packet

from app.services import monitor_service as service
from app.services.monitor_service import MonitorRegistry

# OpenVINO boxes are pixels of the 100x100 test frame: [10, 10, 40, 40] is the normalized [0.1, 0.1, 0.4, 0.4] of
# the MediaPipe face in _packet().
PATIENT = {"label": "pearl", "distance": 0.31, "box": [10, 10, 40, 40]}


class _OpenVino:
    """FaceRecognitionService.identify_faces, with the faces a test sets."""

    def __init__(self):
        self.faces = [dict(PATIENT)]
        self.saturated = False

    def identify_faces(self, frame):
        return {"faces": [dict(face) for face in self.faces], "error": None, "saturated": self.saturated}


@pytest.fixture
def faces(monkeypatch):
    openvino = _OpenVino()
    monkeypatch.setattr(service.IntakeDetectionService, "get_instance", classmethod(lambda cls: _Detector()))
    monkeypatch.setattr(service.FaceRecognitionService, "get_instance", classmethod(lambda cls: openvino))
    monkeypatch.setattr(service.EmotionService, "get_instance", classmethod(lambda cls: _Emotion()))
    return openvino


def _far(seq):
    """The patient beyond about 1.35 m: OpenVINO finds the face, MediaPipe's short-range detector doesn't."""
    return {**_packet(seq), "faces": []}


async def _never_commit(state, candidate, method):
    raise AssertionError("an identity check must not record a dose")


async def _identity_checks(registry, state, packets):
    """One landmark packet, then one identity check on the same frame, as the robot's frame stream does."""
    result = None
    for packet in packets:
        await registry.landmarks(state, packet)
        result = await registry.vision(state, packet["frame_seq"], _jpeg(), _never_commit)
    return result


def _identity_lines(caplog):
    return [record for record in caplog.records
            if record.name == service.log.name and record.getMessage().startswith("identity of session ")]


# ── B1: which sessions use the OpenVINO-only rule ────────────────────────────

@pytest.mark.parametrize("client_type,mode,expected", [
    ("reachy", "observe", True), ("reachy", "dose", False), ("browser", "observe", False), ("browser", "dose", False)])
def test_only_a_robot_session_that_started_in_observe_uses_the_openvino_only_rule(client_type, mode, expected):
    state = MonitorRegistry().start(7, 11 if mode == "dose" else None, "Pearl", "Pearl",
                                    mode=mode, client_type=client_type)
    assert state.started_mode == mode
    assert service.openvino_only(state) is expected


def test_a_search_session_verifies_the_patient_without_a_mediapipe_face(faces):
    async def scenario():
        registry = MonitorRegistry()
        state = registry.start(7, None, "Pearl", "Pearl", mode="observe", client_type="reachy")
        first = await _identity_checks(registry, state, [_far(1)])
        assert first["identity_status"] == "verifying" and state.identity_hits == 1
        result = await _identity_checks(registry, state, [_far(2)])
        assert result["identity_status"] == "verified"
        assert result["target_box"] == [0.1, 0.1, 0.4, 0.4]
        assert result["intk_id"] is None and result["recorded"] is None
    asyncio.run(scenario())


@pytest.mark.parametrize("openvino,saturated,status", [
    ([], True, "searching"),                                                    # 4 or more faces: none is trusted
    ([PATIENT, {**PATIENT, "box": [55, 55, 30, 30]}], False, "mismatch"),       # two faces labelled as the patient
    ([{**PATIENT, "label": "bob"}], False, "mismatch"),                         # someone else
])
def test_a_search_session_still_needs_exactly_one_patient_face_among_fewer_than_four(faces, openvino, saturated,
                                                                                       status):
    faces.faces, faces.saturated = openvino, saturated

    async def scenario():
        registry = MonitorRegistry()
        state = registry.start(7, None, "Pearl", "Pearl", mode="observe", client_type="reachy")
        result = await _identity_checks(registry, state, [_far(1), _far(2), _far(3)])
        assert result["identity_status"] == status
        assert state.identity_hits == 0 and result["target_box"] is None
    asyncio.run(scenario())


def test_a_search_session_needs_two_hits_in_a_row_on_overlapping_boxes(faces):
    async def scenario():
        registry = MonitorRegistry()
        state = registry.start(7, None, "Pearl", "Pearl", mode="observe", client_type="reachy")
        await _identity_checks(registry, state, [_far(1)])
        faces.faces = [{**PATIENT, "box": [60, 60, 30, 30]}]      # the match jumped across the frame: start again
        result = await _identity_checks(registry, state, [_far(2)])
        assert result["identity_status"] == "verifying" and state.identity_hits == 1
        result = await _identity_checks(registry, state, [_far(3)])
        assert result["identity_status"] == "verified"
    asyncio.run(scenario())


@pytest.mark.parametrize("client_type", ["reachy", "browser"])
def test_a_dose_session_still_needs_the_mediapipe_face(faces, client_type):
    async def scenario():
        registry = MonitorRegistry()
        state = registry.start(7, 11, "Pearl", "Pearl", client_type=client_type, auto_commit=False)
        result = await _identity_checks(registry, state, [_far(1), _far(2), _far(3)])
        assert result["identity_status"] == "ambiguous"
        assert state.identity_hits == 0 and result["target_box"] is None
        # The same patient, seen by MediaPipe too, verifies as before.
        result = await _identity_checks(registry, state, [_packet(4), _packet(5)])
        assert result["identity_status"] == "verified"
    asyncio.run(scenario())


def test_a_robot_dose_session_switched_to_observe_after_a_record_still_needs_the_mediapipe_face(faces, monkeypatch):
    detector = _ScriptedDetector()
    monkeypatch.setattr(service.IntakeDetectionService, "get_instance", classmethod(lambda cls: detector))

    async def scenario():
        registry = MonitorRegistry()
        state = registry.start(7, 11, "Pearl", "Pearl", client_type="reachy", auto_commit=True)
        last = await _feed(registry, state, 15, 2.0)
        _verify(state)
        detector.script = {last + 1: ("confirmed", "evt-dose")}
        commits = _Commits()
        await registry.landmarks(state, _timed_packet(last + 1, last / 15))
        await registry.vision(state, last + 1, _jpeg(), commits)
        assert commits.calls == [("evt-dose", "auto")] and state.mode == "observe"
        assert state.started_mode == "dose" and service.openvino_only(state) is False
        # The next identity check is due (once verified it runs every 0.5 s), and the patient has stepped back:
        # OpenVINO still matches, MediaPipe sees no face.
        state.verified_at -= 1.0
        for seq in (last + 2, last + 3):
            await registry.landmarks(state, {**_timed_packet(seq, seq / 15), "faces": []})
            result = await registry.vision(state, seq, _jpeg(), commits)
        assert result["identity_status"] == "ambiguous" and state.identity_hits == 0
        assert commits.calls == [("evt-dose", "auto")]
    asyncio.run(scenario())


# ── B2: the identity log ─────────────────────────────────────────────────────

def test_identity_log_lines_carry_numbers_and_no_names(faces, caplog):
    faces.faces = [dict(PATIENT), {"label": "bob", "distance": 0.52, "box": [55, 20, 30, 30]}]

    async def scenario():
        registry = MonitorRegistry()
        state = registry.start(7, None, "Pearl", "Pearl Chen", mode="observe", client_type="reachy")
        await _identity_checks(registry, state, [_far(1), _far(2)])
        return state

    with caplog.at_level(logging.INFO, logger=service.log.name):
        state = asyncio.run(scenario())
    lines = [record.getMessage() for record in _identity_lines(caplog)]
    # The first check logs its status (verifying), the second a change (verified).
    assert lines == [
        f"identity of session {state.session_id}: status=verifying rule=openvino_only distance=0.310 box_px=40 "
        "ov_faces=2 mp_faces=0",
        f"identity of session {state.session_id}: status=verified rule=openvino_only distance=0.310 box_px=40 "
        "ov_faces=2 mp_faces=0",
    ]
    for line in lines:
        assert "pearl" not in line.lower() and "chen" not in line.lower() and "bob" not in line.lower()


def test_identity_log_is_rate_limited_per_session_and_robot_only(caplog):
    registry = MonitorRegistry()
    state = registry.start(7, None, "Pearl", "Pearl", mode="observe", client_type="reachy")
    with caplog.at_level(logging.INFO, logger=service.log.name):
        service._log_identity(state, "openvino_only", None, None, 0, 0)          # searching: the first line
        service._log_identity(state, "openvino_only", None, None, 0, 0)          # same status within 5 s: dropped
        assert len(_identity_lines(caplog)) == 1
        state.identity_status = "verifying"
        service._log_identity(state, "openvino_only", 0.31, 40, 1, 0)            # a change always logs
        service._log_identity(state, "openvino_only", 0.30, 41, 1, 0)
        assert len(_identity_lines(caplog)) == 2
        state.identity_logged_at -= service.IDENTITY_LOG_SECONDS                  # 5 s later
        service._log_identity(state, "openvino_only", 0.29, 43, 1, 1)
        assert len(_identity_lines(caplog)) == 3
        browser = MonitorRegistry().start(8, 11, "Jane", "Jane")
        service._log_identity(browser, "with_mediapipe", 0.2, 40, 1, 1)          # browser sessions: never
    lines = _identity_lines(caplog)
    assert len(lines) == 3 and all(record.levelno == logging.INFO for record in lines)
    assert lines[0].getMessage().endswith("status=searching rule=openvino_only distance=none box_px=none "
                                          "ov_faces=0 mp_faces=0")
    assert lines[2].getMessage().endswith("status=verifying rule=openvino_only distance=0.290 box_px=43 "
                                          "ov_faces=1 mp_faces=1")
