"""The v1 intake decision policy.

Re-bands the frozen detector's result into the v1 "assisted" contract. Pure
functions only — no I/O, no globals mutated, nothing that can fail a request.

The safety invariant, enforced by `decide()` and covered by the test suite:

    the policy may only ever be MORE conservative than the detector.

A band can be downgraded or left alone. It can never be upgraded, and an event
is never invented when the detector reported none.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional

from app.intake_v1 import config

# ── Bands ────────────────────────────────────────────────────────────────────
BAND_NONE = "none"
BAND_UNCERTAIN = "uncertain"
BAND_CONFIRMED = "confirmed"

BAND_RANK: Dict[str, int] = {
    BAND_NONE: 0,
    BAND_UNCERTAIN: 1,
    BAND_CONFIRMED: 2,
}

# Detector reasons that mean "the geometry contradicts a clean delivery". The
# detector already refuses to auto-log these; v1 additionally refuses to let any
# mode auto-log them, including `autonomous`.
SAFETY_REASONS = frozenset({
    "unknown_open_mouth_no_delivery",
    "wide_open_mouth_cover",
})


def rank(band: str) -> int:
    """Ordering over bands. Unknown bands sort as `none` — fail closed."""
    return BAND_RANK.get(band, 0)


@dataclass(frozen=True)
class PolicyOutcome:
    """The policy's verdict on a single closed detection event."""

    band: str
    reason: str
    detector_band: str
    detector_reason: str
    event_confidence: float
    mode: str
    downgraded: bool
    safety_contradiction: bool


def is_safety_contradiction(detector_reason: str, explicit: Optional[bool] = None) -> bool:
    """Whether the detector flagged geometry that contradicts a clean delivery.

    Prefers an explicit boolean when the detector supplies one; otherwise infers
    it from the reason string, which is what the current frozen detector exposes
    through its trimmed response.
    """
    if explicit is not None:
        return bool(explicit)
    return detector_reason in SAFETY_REASONS


def decide(
    detector_band: str,
    event_confidence: float,
    *,
    detector_reason: str = "",
    safety_contradiction: bool = False,
    mode: Optional[str] = None,
    auto_confirm_min: Optional[float] = None,
    prompt_min: Optional[float] = None,
) -> PolicyOutcome:
    """Re-band one detection event under the v1 policy.

    Thresholds default to the module config but are injectable so the tests can
    pin them without touching the environment.
    """
    mode = mode or config.MODE
    auto_confirm_min = config.AUTO_CONFIRM_MIN if auto_confirm_min is None else auto_confirm_min
    prompt_min = config.PROMPT_MIN if prompt_min is None else prompt_min

    def outcome(band: str, reason: str) -> PolicyOutcome:
        return PolicyOutcome(
            band=band,
            reason=reason,
            detector_band=detector_band,
            detector_reason=detector_reason,
            event_confidence=event_confidence,
            mode=mode,
            downgraded=rank(band) < rank(detector_band),
            safety_contradiction=safety_contradiction,
        )

    # Invariant, checked first: an event the detector did not report is never
    # invented, in any mode.
    if rank(detector_band) == 0:
        return outcome(BAND_NONE, "detector_reported_no_event")

    # Pass-through mode keeps v0 behaviour so the policy can be A/B'd or reverted
    # without a code change — except that a safety contradiction still blocks an
    # automatic log.
    if mode == config.MODE_AUTONOMOUS:
        if safety_contradiction and detector_band == BAND_CONFIRMED:
            return outcome(BAND_UNCERTAIN, "safety_contradiction_blocks_auto_confirm")
        return outcome(detector_band, "autonomous_passthrough")

    # Nothing auto-logs in manual-only mode; a real event still earns a prompt.
    if mode == config.MODE_MANUAL_ONLY:
        if event_confidence >= prompt_min:
            return outcome(BAND_UNCERTAIN, "manual_only_mode")
        return outcome(BAND_NONE, "below_prompt_floor")

    # ── assisted (the v1 default) ────────────────────────────────────────────
    if event_confidence < prompt_min:
        return outcome(BAND_NONE, "below_prompt_floor")

    if safety_contradiction:
        return outcome(BAND_UNCERTAIN, "safety_contradiction_needs_confirmation")

    # Auto-confirm requires BOTH strong evidence and the detector's own top band.
    # Requiring the detector band keeps the invariant intact: a high-confidence
    # event the detector held back as `uncertain` stays a prompt.
    if detector_band == BAND_CONFIRMED and event_confidence >= auto_confirm_min:
        return outcome(BAND_CONFIRMED, "strong_evidence_auto_confirmed")

    if detector_band == BAND_CONFIRMED:
        return outcome(BAND_UNCERTAIN, "below_auto_confirm_threshold")

    return outcome(BAND_UNCERTAIN, "detector_uncertain_needs_confirmation")


def apply_policy(result: Dict[str, Any], **overrides: Any) -> Dict[str, Any]:
    """Apply the v1 policy to a detector response.

    Returns a new dict; the input is not mutated. The response shape is
    preserved — `decision` still carries the band the frontend already reads —
    with a `policy` block added for debugging and audit.
    """
    detector_band = str(result.get("decision") or BAND_NONE)
    detector_reason = str(result.get("decision_reason") or "")

    # `event_confidence` is the score that actually drove the detector's band.
    # `confidence` is the decaying on-screen meter and is only a fallback.
    raw_confidence = result.get("event_confidence")
    if raw_confidence is None:
        raw_confidence = result.get("confidence", 0.0)
    try:
        event_confidence = float(raw_confidence or 0.0)
    except (TypeError, ValueError):
        event_confidence = 0.0

    verdict = decide(
        detector_band,
        event_confidence,
        detector_reason=detector_reason,
        safety_contradiction=is_safety_contradiction(
            detector_reason, result.get("safety_contradiction")
        ),
        **overrides,
    )

    out = dict(result)
    out["decision"] = verdict.band
    out["decision_reason"] = verdict.reason

    # Keep the legacy booleans consistent with the new band. Leaving these true
    # after a downgrade would let a dose be logged twice.
    out["event_detected"] = verdict.band == BAND_CONFIRMED
    out["ingestion_detected"] = verdict.band == BAND_CONFIRMED

    policy_block = asdict(verdict)
    # Correlates the response with its corpus record and its later outcome. Only
    # issued when the detector actually closed an event, so idle frames stay cheap.
    candidate_token = out.get("candidate_token")
    policy_block["event_id"] = (
        str(uuid.uuid5(uuid.NAMESPACE_URL, f"intake:{candidate_token}"))
        if rank(detector_band) > 0 and candidate_token
        else str(uuid.uuid4())
        if rank(detector_band) > 0
        else None
    )
    out["policy"] = policy_block

    return out
