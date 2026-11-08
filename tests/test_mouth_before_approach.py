"""The mouth before the approach (8 Nov 2026, intake_temporal.py): a hand that comes to a mouth that was already open
(a cough, a yawn, talk) and never showed a holding grip is no event; a pill-taker's mouth stays closed until the
hand arrives. The pipeline keeps the face's mouth readings (`mouth_open` on process()) and reads their open fraction
over the 1.5 s before the approach starts."""

import pytest

from app.services.intake_temporal import Observation, Stage, TemporalConfig, TemporalIntakePipeline

FPS = 15
LEAD = 24   # frames of the hand resting before the approach (1.6 s at 15 fps)
GESTURE = ([2.0] * LEAD + [2.0 - 1.5 * k / 9 for k in range(1, 10)] + [0.5] * 5
           + [0.5 + 1.5 * k / 6 for k in range(1, 7)] + [2.0] * 3)
APPROACH = LEAD + 5   # the first frame under the exit distance while moving in (k = 6: 2.0 - 1.5 * 6 / 9 = 1.0)


def _run(open_before, *, grip=False, readings=True, start=1000.0, pipeline=None, **config_values):
    """One hand-to-mouth gesture; `open_before(index)` says whether the mouth reads open on a frame before the
    approach (it reads open at the mouth and closed after, as in an intake). Returns (event result, pipeline)."""
    pipeline = pipeline or TemporalIntakePipeline(TemporalConfig(**config_values))
    for index, distance in enumerate(GESTURE):
        at_mouth = APPROACH <= index < LEAD + 9 + 5
        mouth = at_mouth or (index < APPROACH and open_before(index))
        result = pipeline.process(start + index / FPS, True, [Observation(
            center=(0.5, 0.5), distance=distance, mouth_open=mouth, pinch=grip, flat_palm=False, occlusion=0.0)],
            mouth_open=mouth if readings else None)
        if result["decision"] != "none" or result.get("decision_reason") == "mouth_open_before_approach_rejected":
            return result, pipeline
    raise AssertionError("the gesture made no event and no rejection")


def test_a_closed_mouth_before_the_approach_keeps_the_event():
    result, _ = _run(lambda index: False)
    assert result["decision"] == "uncertain"
    assert result["pre_approach_open_ratio"] == 0.0 and result["pre_approach_samples"] >= 8


def test_an_open_mouth_before_the_approach_without_a_grip_is_no_event():
    result, pipeline = _run(lambda index: True)
    assert (result["decision"], result["decision_reason"]) == ("none", "mouth_open_before_approach_rejected")
    assert result["reset_reason"] == "mouth_open_before_approach_rejected"
    assert result["candidate_id"] is None and result["event_confidence"] == 0.0
    assert pipeline.event.stage == Stage.READY   # re-armed at once


def test_the_next_real_gesture_after_a_rejection_still_prompts():
    _, pipeline = _run(lambda index: True)
    later = 1000.0 + len(GESTURE) / FPS + 1.0
    result, _ = _run(lambda index: False, pipeline=pipeline, start=later)
    assert result["decision"] == "uncertain"


def test_a_grip_keeps_the_event_whatever_the_mouth_did():
    result, _ = _run(lambda index: True, grip=True)
    assert result["decision"] == "confirmed"   # pinch seen, visible withdrawal, no contradiction
    assert result["pre_approach_open_ratio"] == 1.0


def test_too_few_readings_before_the_approach_leave_the_rule_off():
    # Only the last 5 frames before the approach have a face reading: under pre_approach_min_samples (8)
    pipeline = TemporalIntakePipeline()
    result = None
    for index, distance in enumerate(GESTURE):
        at_mouth = APPROACH <= index < LEAD + 9 + 5
        reading = True if (APPROACH - 5 <= index < APPROACH or at_mouth) else None
        result = pipeline.process(1000.0 + index / FPS, True, [Observation(
            center=(0.5, 0.5), distance=distance, mouth_open=True, pinch=False, flat_palm=False, occlusion=0.0)],
            mouth_open=reading)
        if result["decision"] != "none":
            break
    assert result["decision"] == "uncertain"
    assert result["pre_approach_open_ratio"] is None and result["pre_approach_samples"] == 5


def test_without_mouth_readings_nothing_changes():
    result, _ = _run(lambda index: True, readings=False)
    assert result["decision"] == "uncertain" and result["pre_approach_open_ratio"] is None


@pytest.mark.parametrize("open_frames, rejected", [(7, False), (8, True)])
def test_the_threshold_is_an_open_fraction_of_the_window(open_frames, rejected):
    # 22 readings in the 1.5 s before the approach: 7 open is 0.32, 8 open is 0.36 (threshold 0.35)
    window_start = APPROACH - 22
    result, _ = _run(lambda index: window_start <= index < window_start + open_frames)
    assert result["pre_approach_samples"] == 22
    assert (result["decision_reason"] == "mouth_open_before_approach_rejected") is rejected


def test_the_threshold_is_configurable():
    result, _ = _run(lambda index: True, pre_approach_open_max=1.01)
    assert result["decision"] == "uncertain"


# ── a mouth the hand hides completely (covered_mouth_occlusion) ────────────────────────────────────────────────────

def _closed_gesture(occlusion, *, grip=False, open_before=False):
    """A gesture whose mouth is never seen open at the mouth, with this mouth occlusion while the hand is there."""
    pipeline = TemporalIntakePipeline()
    for index, distance in enumerate(GESTURE):
        at_mouth = APPROACH <= index < LEAD + 9 + 5
        mouth = index < APPROACH and open_before
        result = pipeline.process(1000.0 + index / FPS, True, [Observation(
            center=(0.5, 0.5), distance=distance, mouth_open=mouth, pinch=grip, flat_palm=False,
            occlusion=occlusion if at_mouth else 0.0)], mouth_open=mouth)
        if result["decision"] != "none" or str(result.get("decision_reason")).endswith("_rejected"):
            return result
    raise AssertionError("the gesture made no event and no rejection")


def test_a_closed_mouth_the_hand_only_partly_covers_is_still_rejected():
    result = _closed_gesture(0.5)
    assert (result["decision"], result["decision_reason"]) == ("none", "closed_mouth_sequence_rejected")


def test_a_mouth_the_hand_hides_completely_is_no_closed_mouth_and_prompts():
    result = _closed_gesture(1.0)
    assert (result["decision"], result["decision_reason"]) == (
        "uncertain", "completed_ambiguous_or_occluded_sequence")
    assert result["peak_mouth_occlusion"] == 1.0


def test_a_hidden_mouth_never_auto_logs_even_with_a_grip():
    result = _closed_gesture(1.0, grip=True)
    assert result["decision"] == "uncertain" and result["event_confidence"] < 0.75


def test_a_hidden_mouth_that_was_open_before_the_approach_is_still_no_event():
    result = _closed_gesture(1.0, open_before=True)
    assert (result["decision"], result["decision_reason"]) == ("none", "mouth_open_before_approach_rejected")
