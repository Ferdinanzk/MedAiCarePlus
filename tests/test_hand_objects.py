"""What the hand holds (3 Nov 2026): services/landmarks/objects.py runs a COCO YOLOX on the robot's frames and
says which detected object each hand holds; the engine attaches it to the packets, the temporal pipeline counts
it over the contact zone (`object_in_hand`), and the policy makes a veto object (a phone) a prompt at most, or
no event with INTAKE_OBJECT_VETO=drop. Everything here runs without the model file."""

import math
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from app.intake_v1 import config
from app.intake_v1.policy import apply_policy, decide
from app.services.intake_temporal import Observation, TemporalConfig, TemporalIntakePipeline
from app.services.landmarks import engine, objects

PHONE = objects.COCO_CLASSES.index("cell phone")
CHAIR = objects.COCO_CLASSES.index("chair")
CUP = objects.COCO_CLASSES.index("cup")


# ── objects.py: decoding, NMS, which hand holds what ────────────────────────

def _raw(size, hits):
    """A raw YOLOX output (N, 85) of zeros with one prediction per hit: (stride, cell_x, cell_y, w, h, label, score)."""
    cell, stride = objects.grid(size)
    raw = np.zeros((len(cell), 85), dtype=np.float32)
    for s, cx, cy, w, h, label, score in hits:
        offset = sum((size // t) ** 2 for t in objects.STRIDES if t < s)
        row = offset + cy * (size // s) + cx
        assert cell[row][0] == cx and cell[row][1] == cy and stride[row][0] == s
        raw[row, :4] = (0.5, 0.5, math.log(w / s), math.log(h / s))
        raw[row, 4] = 1.0
        raw[row, 5 + label] = score
    return raw


def test_decode_and_detections_follow_the_yolox_demo():
    size, width, height = 416, 640, 360
    ratio = min(size / height, size / width)                     # 0.65: the letterbox scale
    raw = _raw(size, [(8, 10, 12, 40, 60, PHONE, 0.9), (16, 3, 4, 50, 50, CHAIR, 0.95), (32, 2, 2, 30, 30, CUP, 0.2)])
    cell, stride = objects.grid(size)
    decoded = objects.decode(raw, cell, stride)
    assert decoded[12 * 52 + 10, :4] == pytest.approx((84.0, 100.0, 40.0, 60.0))   # (10.5 * 8, 12.5 * 8, w, h)
    found = objects.detections(decoded, ratio, width, height, score=0.5)
    assert [item["label"] for item in found] == ["cell phone"]      # the chair is not WATCHED, the cup too weak
    phone = found[0]
    assert phone["score"] == pytest.approx(0.9)
    x, y, w, h = phone["box"]
    assert (x * width, y * height) == pytest.approx((64 / ratio, 70 / ratio), abs=0.5)
    assert (w * width, h * height) == pytest.approx((40 / ratio, 60 / ratio), abs=0.5)
    assert objects.detections(decoded, ratio, width, height, score=0.95) == []


def test_preprocess_letterboxes_with_114_in_bgr_order():
    bgr = np.zeros((36, 64, 3), dtype=np.uint8)
    bgr[:, :, 0] = 200                                             # blue
    tensor, ratio = objects.preprocess(bgr, 64)
    assert tensor.shape == (1, 3, 64, 64) and tensor.dtype == np.float32 and ratio == 1.0
    assert tensor[0, 0, 0, 0] == 200 and tensor[0, 1, 0, 0] == 0   # channel order kept (BGR in, BGR out)
    assert tensor[0, :, 50, 10].tolist() == [114, 114, 114]        # below the image: padding


def test_nms_keeps_the_stronger_of_two_overlapping_boxes():
    boxes = np.array([[0, 0, 100, 100], [10, 10, 110, 110], [300, 300, 350, 350]], dtype=np.float32)
    scores = np.array([0.6, 0.9, 0.5], dtype=np.float32)
    assert objects.nms(boxes, scores) == [1, 2]


def _hand(x0, y0, x1, y1):
    return [[x0, y0]] + [[x1, y1]] * 12


def test_held_objects_matches_an_object_to_the_hand_it_overlaps():
    phone = {"label": "cell phone", "score": 0.8, "box": [0.42, 0.52, 0.06, 0.1]}
    cup = {"label": "cup", "score": 0.7, "box": [0.8, 0.8, 0.1, 0.1]}
    holding = _hand(0.4, 0.5, 0.5, 0.6)
    away = _hand(0.1, 0.1, 0.2, 0.2)
    assert objects.held_objects([phone, cup], [holding, away]) == [{"label": "cell phone", "score": 0.8}, {"label": None}]
    assert objects.held_objects([], [holding]) == [{"label": None}]
    assert objects.held_objects([phone], []) == []


# ── engine.py: when the detector runs ───────────────────────────────────────

def _points(count, values, at):
    points = np.zeros((count, values), dtype=np.float32)
    points[:, 0], points[:, 1] = at
    return points


class _Tracker:
    def __init__(self, found):
        self.found = found

    def step(self, image):
        return self.found


class _Detector:
    def __init__(self):
        self.calls = 0

    def detect(self, image):
        self.calls += 1
        return [{"label": "cell phone", "score": 0.8, "box": [0.4, 0.5, 0.1, 0.1]}]


def _engine(hand_near, detector, every=1):
    face = _points(478, 3, (30.0, 30.0))
    face[10], face[152] = (30.0, 20.0, 0), (30.0, 60.0, 0)
    face[33], face[263] = (20.0, 35.0, 0), (60.0, 35.0, 0)
    hand = _points(21, 3, (45.0, 55.0) if hand_near else (95.0, 95.0))
    hand[0] = (45.0, 90.0, 0)
    vision = object.__new__(engine.VisionEngine)
    vision._executor = ThreadPoolExecutor(max_workers=4)
    vision._depth, vision._depth_every, vision._objects, vision._objects_every = None, 1, detector, every
    vision._near_face, vision._frames = False, 0
    vision.trackers = {"face": _Tracker([face]), "hand": _Tracker([hand]), "pose": _Tracker([])}
    return vision


class _Image:
    size = (100, 100)


def test_the_detector_runs_from_the_frame_after_a_hand_came_near_a_face():
    detector = _Detector()
    vision = _engine(hand_near=True, detector=detector)
    first = vision.process(_Image(), 1, 1000.0)
    assert "objects" not in first and "hand_objects" not in first and detector.calls == 0
    second = vision.process(_Image(), 2, 1066.0)
    assert detector.calls == 1 and second["objects"][0]["label"] == "cell phone"
    assert second["hand_objects"] == [{"label": "cell phone", "score": 0.8}]


def test_the_detector_runs_every_nth_frame_and_never_without_a_hand_near_a_face():
    detector = _Detector()
    vision = _engine(hand_near=True, detector=detector, every=3)
    with_objects = [seq for seq in range(1, 11) if "objects" in vision.process(_Image(), seq, 1000.0 + seq)]
    assert with_objects == [4, 7, 10] and detector.calls == 3
    idle = _Detector()
    vision = _engine(hand_near=False, detector=idle)
    assert all("objects" not in vision.process(_Image(), seq, 1000.0 + seq) for seq in range(1, 5))
    assert idle.calls == 0


# ── intake_temporal.py: the count over the contact zone ─────────────────────

GESTURE = ([2.0] * 6 + [2.0 - 1.5 * k / 9 for k in range(1, 10)] + [0.5] * 5
           + [0.5 + 1.5 * k / 6 for k in range(1, 7)] + [2.0] * 3)


def _run_gesture(held, checked=True, **config_values):
    pipeline = TemporalIntakePipeline(TemporalConfig(**config_values))
    for index, (distance, label) in enumerate(zip(GESTURE, held)):
        result = pipeline.process(1000.0 + index / 15, True, [Observation(
            center=(0.5, 0.5), distance=distance, mouth_open=True, pinch=True, flat_palm=False, occlusion=0.0,
            object_checked=checked, held_object=label)])
        if result["decision"] != "none":
            return result
    raise AssertionError("the gesture made no event")


def test_without_the_detector_nothing_changes():
    result = _run_gesture([None] * len(GESTURE), checked=False)
    assert (result["decision"], result["decision_reason"]) == ("confirmed", "coherent_delivery_sequence")
    assert (result["object_samples"], result["object_in_hand"]) == (0, None)


def test_a_phone_held_through_the_contact_is_the_object_in_hand():
    result = _run_gesture(["cell phone"] * len(GESTURE))
    assert result["object_in_hand"] == "cell phone" and result["object_samples"] >= 2
    assert (result["decision"], result["decision_reason"]) == ("confirmed", "coherent_delivery_sequence")


def test_an_empty_hand_and_a_minority_object_give_none():
    assert _run_gesture([None] * len(GESTURE))["object_in_hand"] is None
    mostly_empty = ["cell phone" if index == 16 else None for index in range(len(GESTURE))]
    assert _run_gesture(mostly_empty)["object_in_hand"] is None
    half = ["cell phone" if index % 2 else "cup" for index in range(len(GESTURE))]
    assert _run_gesture(half, object_ratio=0.5)["object_in_hand"] in ("cell phone", "cup")
    assert _run_gesture(half, object_ratio=0.6)["object_in_hand"] is None


# ── policy.py: a veto object prompts, or drops the event ────────────────────

def test_the_defaults_veto_a_phone_with_a_prompt_and_not_a_cup():
    assert config.OBJECT_VETO == config.OBJECT_VETO_PROMPT
    assert {"cell phone", "remote", "toothbrush"} <= config.OBJECT_VETO_LABELS
    assert "cup" not in config.OBJECT_VETO_LABELS


@pytest.mark.parametrize("mode", config.VALID_MODES)
def test_a_phone_in_the_hand_never_auto_logs(mode):
    verdict = decide("confirmed", 0.9, mode=mode, object_in_hand="cell phone", object_veto="prompt")
    assert verdict.band == "uncertain" and verdict.object_in_hand == "cell phone" and verdict.downgraded
    assert verdict.reason in ("object_in_hand_needs_confirmation", "object_in_hand_blocks_auto_confirm",
                              "manual_only_mode")
    dropped = decide("confirmed", 0.9, mode=mode, object_in_hand="cell phone", object_veto="drop")
    assert (dropped.band, dropped.reason) == ("none", "object_in_hand_no_event")
    assert decide("uncertain", 0.5, mode=mode, object_in_hand="cell phone", object_veto="drop").band == "none"


@pytest.mark.parametrize("mode", config.VALID_MODES)
def test_a_cup_changes_nothing(mode):
    with_cup = decide("confirmed", 0.9, mode=mode, object_in_hand="cup", object_veto="drop")
    without = decide("confirmed", 0.9, mode=mode)
    assert (with_cup.band, with_cup.reason) == (without.band, without.reason)
    assert with_cup.object_in_hand == "cup" and without.object_in_hand is None


def test_a_veto_never_invents_an_event():
    assert decide("none", 0.9, object_in_hand="cell phone", object_veto="prompt").band == "none"


def test_apply_policy_reads_the_object_from_the_detector_result(monkeypatch):
    result = {"decision": "confirmed", "decision_reason": "coherent_delivery_sequence", "event_confidence": 0.82,
              "candidate_token": "abc", "event_detected": True, "ingestion_detected": True}
    assert apply_policy(result, mode=config.MODE_ASSISTED)["decision"] == "confirmed"
    phone = apply_policy({**result, "object_in_hand": "cell phone"}, mode=config.MODE_ASSISTED)
    assert (phone["decision"], phone["decision_reason"]) == ("uncertain", "object_in_hand_needs_confirmation")
    assert phone["policy"]["object_in_hand"] == "cell phone" and phone["policy"]["detector_band"] == "confirmed"
    monkeypatch.setattr(config, "OBJECT_VETO", config.OBJECT_VETO_DROP)
    dropped = apply_policy({**result, "object_in_hand": "cell phone"}, mode=config.MODE_ASSISTED)
    assert (dropped["decision"], dropped["decision_reason"]) == ("none", "object_in_hand_no_event")
    assert dropped["event_detected"] is False and dropped["policy"]["event_id"]
    assert apply_policy({**result, "object_in_hand": "cup"}, mode=config.MODE_ASSISTED)["decision"] == "confirmed"
