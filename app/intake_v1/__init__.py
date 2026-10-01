"""Assisted intake confirmation for v1.

Sits downstream of the frozen detector (`app/services/intake_detection.py`) and
upstream of the API response. Nothing in this package modifies frozen code, and
importing it changes no behaviour until `apply_policy` is wired in — see
`README.md`.
"""

from app.intake_v1.config import (
    MODE,
    MODE_ASSISTED,
    MODE_AUTONOMOUS,
    MODE_MANUAL_ONLY,
    SCHEMA_VERSION,
)
from app.intake_v1.event_log import log_event, log_outcome
from app.intake_v1.policy import (
    BAND_CONFIRMED,
    BAND_NONE,
    BAND_UNCERTAIN,
    PolicyOutcome,
    apply_policy,
    decide,
)
from app.intake_v1.preflight import assert_safe, health_report, run_checks

__all__ = [
    "MODE",
    "MODE_ASSISTED",
    "MODE_AUTONOMOUS",
    "MODE_MANUAL_ONLY",
    "SCHEMA_VERSION",
    "BAND_CONFIRMED",
    "BAND_NONE",
    "BAND_UNCERTAIN",
    "PolicyOutcome",
    "apply_policy",
    "decide",
    "log_event",
    "log_outcome",
    "assert_safe",
    "health_report",
    "run_checks",
]
