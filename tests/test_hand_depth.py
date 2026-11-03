"""Hand depth against the face (3 Nov 2026): services/landmarks/depth.py turns a monocular depth map into one
number per hand (`front`), the engine attaches it to the robot's landmark packets, the temporal pipeline counts
it over the contact zone (`depth_no_contact`), and the policy lets such an event prompt but never auto-log.
Everything here runs without the model file: the depth map is synthetic."""

from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from app.intake_v1 import config
from app.intake_v1.policy import apply_policy, decide
from app.services.intake_temporal import Observation, TemporalConfig, TemporalIntakePipeline
from app.services.landmarks import depth, engine


# ── depth.py: the reading ───────────────────────────────────────────────────

def _face(box):
    """A packet face: 9 compact points inside the box, the mouth in its lower middle."""
    x, y, w, h = box
    mouth_y = y + 0.75 * h
    points = [[x + w / 2, y + 0.55 * h], [x + w / 2, y + 0.05 * h], [x + w / 2, mouth_y - 0.02 * h],
              [x + w / 2, mouth_y + 0.02 * h], [x + 0.2 * w, y + 0.35 * h], [x + 0.3 * w, mouth_y],
              [x + w / 2, y + 0.98 * h], [x + 0.8 * w, y + 0.35 * h], [x + 0.7 * w, mouth_y]]
    return {"box": list(box), "points": points}


def _hand(tip, wrist):
    """A packet hand: every point at the tip except the wrist (compact index 0)."""
    return [list(wrist)] + [list(tip)] * 12


def _scene(hand_value, face_value=2.0, size=60):
    """A map with the face box [0.2, 0.2, 0.4, 0.4] at face_value, a hand blob at hand_value, background 0.5."""
    disparity = np.full((size, size), 0.5, dtype=np.float32)
    disparity[12:36, 12:36] = face_value
    disparity[33:40, 24:31] = hand_value          # around the finger sample point (27, 36)
    return disparity


FACE = _face((0.2, 0.2, 0.4, 0.4))
HAND = _hand(tip=(0.45, 0.55), wrist=(0.45, 0.9))


def test_a_hand_in_front_of_the_face_reads_positive_and_the_scale_cancels():
    [reading] = depth.hand_depth(_scene(3.0), [FACE], [HAND])
    assert reading["face"] == 0 and reading["front"] == pytest.approx(0.5, abs=0.02)
    assert reading["tip"] == pytest.approx(3.0) and reading["ref"] == pytest.approx(2.0)
    [scaled] = depth.hand_depth(_scene(3.0) * 3, [FACE], [HAND])
    assert scaled["front"] == pytest.approx(reading["front"], abs=1e-6)
    [shifted] = depth.hand_depth(_scene(3.0) + 1, [FACE], [HAND])
    assert 0 < shifted["front"] < reading["front"]        # an offset only shrinks the number


def test_a_hand_at_the_face_reads_zero_and_behind_it_negative():
    [at_face] = depth.hand_depth(_scene(2.0), [FACE], [HAND])
    assert at_face["front"] == pytest.approx(0.0, abs=0.02)
    [behind] = depth.hand_depth(_scene(1.0), [FACE], [HAND])
    assert behind["front"] < -0.4


def test_the_face_reference_leaves_the_hand_out():
    """A hand over half the face at a very different depth does not move the reference, and the lips are not
    read while a hand box covers them."""
    covering = _hand(tip=(0.3, 0.5), wrist=(0.3, 0.5))     # a box of one point: cut out with its margin only
    scene = _scene(9.0)
    scene[24:36, 12:24] = 9.0                              # the lower-left quarter of the face box, hand-deep
    [reading] = depth.hand_depth(scene, [FACE], [covering])
    assert reading["ref"] == pytest.approx(2.0)
    lips = _hand(tip=(0.4, 0.5), wrist=(0.4, 0.5))        # on the mouth point
    [over_lips] = depth.hand_depth(_scene(3.0), [FACE], [lips])
    assert over_lips["lip"] is None
    [clear] = depth.hand_depth(_scene(3.0), [FACE], [HAND])
    assert clear["lip"] == pytest.approx(2.0)


def test_no_reading_without_a_usable_face():
    assert depth.hand_depth(_scene(3.0), [], [HAND]) == [None]
    tiny = _face((0.2, 0.2, 0.05, 0.05))                   # 3 px on a 60 px map: below MIN_FACE_PX
    assert depth.hand_depth(_scene(3.0), [tiny], [HAND]) == [None]
    whole_face = [list(p) for p in ([0.1, 0.1], [0.7, 0.7])] + [[0.4, 0.4]] * 11   # a box over the whole face
    assert depth.hand_depth(_scene(3.0), [FACE], [whole_face]) == [None]
    assert depth.hand_depth(_scene(3.0), [FACE], []) == []


def test_near_face_is_a_fingertip_within_the_widened_face_box():
    assert depth.near_face([FACE], [HAND])
    far = _hand(tip=(0.95, 0.95), wrist=(0.99, 0.99))
    assert not depth.near_face([FACE], [far])
    assert not depth.near_face([], [HAND]) and not depth.near_face([FACE], [])


def test_the_model_input_keeps_the_aspect_ratio_in_multiples_of_14():
    model = object.__new__(depth.DepthModel)
    model.long_side = 266
    assert model.input_size(640, 360) == (266, 154)
    assert model.input_size(360, 640) == (154, 266)
    assert model.input_size(100, 100) == (266, 266)
    assert all(side % 14 == 0 for side in model.input_size(1920, 1080))


# ── engine.py: when the reading is made ─────────────────────────────────────

def _points(count, values, at):
    points = np.zeros((count, values), dtype=np.float32)
    points[:, 0], points[:, 1] = at
    return points


class _Tracker:
    def __init__(self, found):
        self.found = found

    def step(self, image):
        return self.found


class _Depth:
    def __init__(self):
        self.calls = 0

    def disparity(self, image):
        self.calls += 1
        return _scene(3.0)


def _engine(hand_near, depth_model, every=1):
    """A VisionEngine with stub trackers on a 100 x 100 image: one face in [20, 20, 40, 40] px, a hand whose
    points sit at the face's mouth (near) or in a corner (not near)."""
    face = _points(478, 3, (30.0, 30.0))
    face[10] = (30.0, 20.0, 0)
    face[152] = (30.0, 60.0, 0)
    face[33], face[263] = (20.0, 35.0, 0), (60.0, 35.0, 0)      # box x 20-60, y 20-60
    face[13], face[14], face[61], face[291] = (40, 49, 0), (40, 51, 0), (34, 50, 0), (46, 50, 0)
    hand = _points(21, 3, (45.0, 55.0) if hand_near else (95.0, 95.0))
    hand[0] = (45.0, 90.0, 0)
    vision = object.__new__(engine.VisionEngine)
    vision._executor = ThreadPoolExecutor(max_workers=4)
    vision._depth, vision._depth_every, vision._objects, vision._objects_every = depth_model, every, None, 1
    vision._near_face, vision._frames = False, 0
    vision.trackers = {"face": _Tracker([face]), "hand": _Tracker([hand]), "pose": _Tracker([])}
    return vision


class _Image:
    size = (100, 100)


def test_depth_runs_from_the_frame_after_a_hand_came_near_a_face():
    model = _Depth()
    vision = _engine(hand_near=True, depth_model=model)
    first = vision.process(_Image(), 1, 1000.0)
    assert "hand_depth" not in first and model.calls == 0
    second = vision.process(_Image(), 2, 1066.0)
    assert model.calls == 1 and len(second["hand_depth"]) == len(second["hands"]) == 1
    assert second["hand_depth"][0]["face"] == 0 and isinstance(second["hand_depth"][0]["front"], float)


def test_depth_runs_every_nth_frame_and_never_without_a_hand_near_a_face():
    model = _Depth()
    vision = _engine(hand_near=True, depth_model=model, every=3)
    with_depth = [seq for seq in range(1, 11) if "hand_depth" in vision.process(_Image(), seq, 1000.0 + seq)]
    assert with_depth == [4, 7, 10] and model.calls == 3
    idle = _Depth()
    vision = _engine(hand_near=False, depth_model=idle)
    assert all("hand_depth" not in vision.process(_Image(), seq, 1000.0 + seq) for seq in range(1, 5))
    assert idle.calls == 0


def test_without_the_depth_model_the_packet_is_as_before():
    vision = _engine(hand_near=True, depth_model=None)
    for seq in range(1, 4):
        packet = vision.process(_Image(), seq, 1000.0 + seq)
        assert "hand_depth" not in packet
        assert set(packet) == {"frame_seq", "timestamp", "width", "height", "faces", "hands", "poses"}


# ── intake_temporal.py: the count over the contact zone ─────────────────────

GESTURE = ([2.0] * 6 + [2.0 - 1.5 * k / 9 for k in range(1, 10)] + [0.5] * 5
           + [0.5 + 1.5 * k / 6 for k in range(1, 7)] + [2.0] * 3)


def _run_gesture(fronts, **config_values):
    """The gesture through the real pipeline at 15 fps; `fronts` gives each frame's depth reading (or None).
    Returns the completed event's result."""
    pipeline = TemporalIntakePipeline(TemporalConfig(**config_values))
    for index, (distance, front) in enumerate(zip(GESTURE, fronts)):
        result = pipeline.process(1000.0 + index / 15, True, [Observation(
            center=(0.5, 0.5), distance=distance, mouth_open=True, pinch=True, flat_palm=False, occlusion=0.0,
            front=front)])
        if result["decision"] != "none":
            return result
    raise AssertionError("the gesture made no event")


def test_without_readings_nothing_changes():
    result = _run_gesture([None] * len(GESTURE))
    assert (result["decision"], result["decision_reason"]) == ("confirmed", "coherent_delivery_sequence")
    assert (result["depth_samples"], result["depth_front_median"], result["depth_no_contact"]) == (0, None, False)


def test_fingers_in_front_of_the_face_through_the_contact_make_depth_no_contact():
    result = _run_gesture([0.9] * len(GESTURE), depth_front_threshold=0.5)
    assert result["depth_no_contact"] is True and result["depth_samples"] >= 2
    assert result["depth_front_median"] == pytest.approx(0.9)
    # The detector's own band is untouched: the policy decides what the flag means.
    assert (result["decision"], result["decision_reason"]) == ("confirmed", "coherent_delivery_sequence")


def test_only_contact_zone_readings_count():
    """Fingers in front of the face on the approach, at the lips during the contact: no flag."""
    fronts = [0.9 if distance > TemporalConfig.exit_distance else 0.1 for distance in GESTURE]
    result = _run_gesture(fronts, depth_front_threshold=0.5)
    assert result["depth_no_contact"] is False and result["depth_samples"] >= 2
    assert result["depth_front_median"] == pytest.approx(0.1)


def test_the_flag_needs_enough_readings_and_a_majority():
    one_reading = [None] * len(GESTURE)
    one_reading[16] = 0.9                                    # a single contact-zone reading
    assert _run_gesture(one_reading, depth_front_threshold=0.5)["depth_no_contact"] is False
    split = [0.9 if index % 2 else 0.1 for index in range(len(GESTURE))]
    assert _run_gesture(split, depth_front_threshold=0.5, depth_front_ratio=0.6)["depth_no_contact"] is False
    assert _run_gesture(split, depth_front_threshold=0.5, depth_front_ratio=0.4)["depth_no_contact"] is True


# ── policy.py: a prompt at most ──────────────────────────────────────────────

@pytest.mark.parametrize("mode", config.VALID_MODES)
def test_depth_no_contact_never_auto_logs(mode):
    verdict = decide("confirmed", 0.9, mode=mode, depth_no_contact=True)
    assert verdict.band == "uncertain" and verdict.depth_no_contact and verdict.downgraded
    assert verdict.reason in ("depth_no_contact_needs_confirmation", "depth_no_contact_blocks_auto_confirm",
                              "manual_only_mode")
    assert decide("confirmed", 0.9, mode=mode).depth_no_contact is False


def test_depth_no_contact_never_invents_or_upgrades_an_event():
    assert decide("none", 0.9, depth_no_contact=True).band == "none"
    assert decide("uncertain", 0.9, depth_no_contact=True).band == "uncertain"
    assert decide("uncertain", 0.1, depth_no_contact=True).band == "none"


def test_apply_policy_reads_the_flag_from_the_detector_result():
    result = {"decision": "confirmed", "decision_reason": "coherent_delivery_sequence", "event_confidence": 0.82,
              "candidate_token": "abc", "event_detected": True, "ingestion_detected": True}
    assert apply_policy(result, mode=config.MODE_ASSISTED)["decision"] == "confirmed"
    flagged = apply_policy({**result, "depth_no_contact": True}, mode=config.MODE_ASSISTED)
    assert (flagged["decision"], flagged["decision_reason"]) == ("uncertain", "depth_no_contact_needs_confirmation")
    assert flagged["event_detected"] is False and flagged["policy"]["depth_no_contact"] is True
    assert flagged["policy"]["detector_band"] == "confirmed"
    overridden = apply_policy({**result, "depth_no_contact": True}, mode=config.MODE_ASSISTED, depth_no_contact=False)
    assert overridden["decision"] == "confirmed"
