"""The v1 intake policy's ownership_gap input (3 Nov 2026): an event the monitor pieced together across a gap in the
patient's hand ownership (monitor_service.OWNERSHIP_GRACE_SECONDS) may prompt, never auto-log, in every mode. The
policy still never upgrades a band or invents an event."""

import pytest

from app.intake_v1 import config
from app.intake_v1.policy import apply_policy, decide


@pytest.mark.parametrize("mode", config.VALID_MODES)
def test_an_ownership_gap_never_auto_logs(mode):
    verdict = decide("confirmed", 0.9, mode=mode, ownership_gap=True)
    assert verdict.band == "uncertain" and verdict.ownership_gap and verdict.downgraded
    assert verdict.reason in ("ownership_gap_needs_confirmation", "ownership_gap_blocks_auto_confirm",
                              "manual_only_mode")


@pytest.mark.parametrize("mode", config.VALID_MODES)
def test_without_a_gap_the_bands_are_as_before(mode):
    verdict = decide("confirmed", 0.9, mode=mode)
    assert verdict.band == ("uncertain" if mode == config.MODE_MANUAL_ONLY else "confirmed")
    assert verdict.ownership_gap is False


def test_an_ownership_gap_never_invents_or_upgrades_an_event():
    assert decide("none", 0.9, ownership_gap=True).band == "none"
    assert decide("uncertain", 0.9, ownership_gap=True).band == "uncertain"
    assert decide("uncertain", 0.1, ownership_gap=True).band == "none"      # below the prompt floor, as before


def test_apply_policy_passes_the_gap_through_and_keeps_the_legacy_flags_consistent():
    result = {"decision": "confirmed", "decision_reason": "coherent_delivery_sequence", "event_confidence": 0.82,
              "candidate_token": "abc", "event_detected": True, "ingestion_detected": True}
    out = apply_policy(result, mode=config.MODE_ASSISTED, ownership_gap=True)
    assert out["decision"] == "uncertain" and out["decision_reason"] == "ownership_gap_needs_confirmation"
    assert out["event_detected"] is False and out["ingestion_detected"] is False
    assert out["policy"]["ownership_gap"] is True and out["policy"]["detector_band"] == "confirmed"
    assert out["policy"]["event_id"]
    assert result["decision"] == "confirmed"                                  # the input is not mutated
    assert apply_policy(result, mode=config.MODE_ASSISTED)["decision"] == "confirmed"
