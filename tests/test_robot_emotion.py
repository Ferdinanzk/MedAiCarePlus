"""Robots score emotion on the robot; the server accepts it only for the verified patient's uncovered face."""

import asyncio
import time

from tests.test_monitor_backend import _Detector, _Faces, _hand, _jpeg, _packet

from app.services import monitor_service as service
from app.services.emotion_service import LABELS
from app.services.monitor_service import MonitorRegistry

SAD = {name: (0.94 if name == "sad" else 0.01) for name in LABELS}


def _robot_session(registry, verified=True):
    state = registry.start(7, 11, "Pearl", "Pearl", client_type="reachy")
    if verified:
        state.identity_hits, state.verified_at = 2, time.monotonic()
        state.target_box = [0.1, 0.1, 0.4, 0.4]
    return state


def _with_emotion(seq, face_index=0, probabilities=SAD, hands=()):
    return {**_packet(seq), "hands": list(hands), "emotion": {"face_index": face_index, "probabilities": probabilities}}


def _run(monkeypatch, scenario):
    monkeypatch.setattr(service.IntakeDetectionService, "get_instance", classmethod(lambda cls: _Detector()))
    asyncio.run(scenario())


def test_robot_emotion_is_accepted_for_the_verified_face(monkeypatch):
    async def scenario():
        registry = MonitorRegistry()
        state = _robot_session(registry)
        result = await registry.landmarks(state, _with_emotion(1))
        assert result["emotion"]["emotion_type"] == "Sad" and result["emotion"]["source"] == "robot"
        assert result["target_box"] == [0.1, 0.1, 0.4, 0.4]
        assert len(state.emotion_samples) == 1 and state.emotion_samples[0][1]["sad"] == 0.94
    _run(monkeypatch, scenario)


def test_robot_emotion_is_refused_unverified_wrong_face_or_covered_mouth(monkeypatch):
    async def scenario():
        registry = MonitorRegistry()
        unverified = _robot_session(registry, verified=False)
        result = await registry.landmarks(unverified, _with_emotion(1))
        assert result["emotion"] is None and result["target_box"] is None and not unverified.emotion_samples
        await registry.end(unverified)

        state = _robot_session(registry)
        await registry.landmarks(state, _with_emotion(1, face_index=1))      # not the owned face
        assert state.emotion is None
        # A hand over the mouth (owned through a visible wrist next to it).
        packet = _with_emotion(2, hands=[_hand(0.3, 0.3)])
        packet["poses"][0]["wrists"] = [[0.3, 0.32, 0.9]]
        await registry.landmarks(state, packet)
        assert state.emotion is None and not state.emotion_samples

    _run(monkeypatch, scenario)


def test_browser_sessions_ignore_an_emotion_field(monkeypatch):
    async def scenario():
        registry = MonitorRegistry()
        state = registry.start(7, 11, "Pearl", "Pearl")
        state.identity_hits, state.verified_at, state.target_box = 2, time.monotonic(), [0.1, 0.1, 0.4, 0.4]
        await registry.landmarks(state, _with_emotion(1))
        assert state.emotion is None and not state.emotion_samples
    _run(monkeypatch, scenario)


def test_robot_snapshots_are_identity_only(monkeypatch):
    class _NoEmotion:
        def predict_crop(self, crop):
            raise AssertionError("the server must not score emotion for a robot session")

    monkeypatch.setattr(service.FaceRecognitionService, "get_instance", classmethod(lambda cls: _Faces()))
    monkeypatch.setattr(service.EmotionService, "get_instance", classmethod(lambda cls: _NoEmotion()))

    async def scenario():
        registry = MonitorRegistry()
        state = registry.start(7, 11, "Pearl", "Pearl", client_type="reachy")
        for seq in (1, 2):
            await registry.landmarks(state, _packet(seq))
            result = await registry.vision(state, seq, _jpeg(), lambda *_: None)
        assert result["identity_status"] == "verified" and result["target_box"] is not None
    _run(monkeypatch, scenario)


def test_a_robot_streaming_frames_gets_emotion_from_the_server(monkeypatch):
    """Server-vision robots (vision_on_server) send plain frames, so the server scores emotion for them."""
    scored = []

    class _Emotion:
        def predict_crop(self, crop):
            scored.append(crop.shape)
            return {"detected": True, "emotion_type": "Happy", "emotion_score": 0.9,
                    "probabilities": {name: (0.9 if name == "happy" else 0.1 / 6) for name in LABELS}}

    monkeypatch.setattr(service.FaceRecognitionService, "get_instance", classmethod(lambda cls: _Faces()))
    monkeypatch.setattr(service.EmotionService, "get_instance", classmethod(lambda cls: _Emotion()))

    async def scenario():
        registry = MonitorRegistry()
        state = registry.start(7, 11, "Pearl", "Pearl", client_type="reachy")
        state.vision_engine = object()   # set by POST /api/device/monitor/frame
        for seq in (1, 2):
            await registry.landmarks(state, _packet(seq))
            result = await registry.vision(state, seq, _jpeg(), lambda *_: None)
        assert scored and result["emotion"]["emotion_type"] == "Happy"
    _run(monkeypatch, scenario)
