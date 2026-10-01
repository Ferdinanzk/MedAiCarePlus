"""Append-only JSONL corpus of detection events and their human outcomes.

Every prompt a patient answers is a human-labelled training example, recorded
under real deployment conditions with real hard negatives. This module writes
them down.

Two rules govern everything here:

1. **It never raises into a request.** A corpus write failing must not stop a
   patient logging their medication. Every public function swallows its errors
   and warns instead.
2. **Numeric features only.** No images, no video, no landmarks, no names, no
   raw identifiers. User and session references are truncated HMACs so events
   can be grouped by person without the file identifying who that person is.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from app.intake_v1 import config

logger = logging.getLogger(__name__)

_write_lock = threading.Lock()

# Whitelisted feature keys, copied from the detector's trimmed debug block. An
# explicit allow-list keeps the schema stable and guarantees that a future
# detector change cannot silently start writing new fields into the corpus.
FEATURE_KEYS = (
    "frame_confidence",
    "event_confidence",
    "peak_confidence",
    "event_window_closed",
    "peak_mouth_contact",
    "mouth_open",
    "mouth_open_ratio",
    "mouth_open_baseline",
    "mouth_open_delta",
    "mouth_opened_at",
    "mouth_motion_cycles",
    "peak_mouth_open_ratio",
    "peak_mouth_open_delta",
    "tongue_score",
    "tongue_quality",
    "tongue_peak_score",
    "tongue_peak_quality",
    "tongue_support_frames",
    "tongue_support",
    "event_style",
    "withdrew_enough",
    "dwell",
    "raw_event_score",
    "confirm_threshold",
    "hand_near_mouth",
    "hands",
    "status",
    "stage",
    "candidate_id",
    "event_started_at",
    "completion_reason",
    "transition_reason",
    "waiting_reason",
    "missing_observation",
    "approach_velocity",
    "withdrawal_velocity",
    "occlusion_duration",
    "hand_lost",
    "reacquired",
    "delivery_evidence",
    "safety_contradiction",
    "flat_palm_ratio",
    "peak_mouth_occlusion",
)

RECORD_EVENT = "event"
RECORD_OUTCOME = "outcome"

# What the person actually did after being shown the prompt. This is the label.
VALID_OUTCOMES = frozenset({
    "taken_confirmed",   # patient confirmed the dose was taken
    "rejected",          # patient said this was not an intake  → hard negative
    "timeout",           # prompt expired unanswered            → weak label
    "manual",            # logged manually without a detection  → missed positive
})


def _pseudonym(value: Any) -> Optional[str]:
    """Stable, non-reversible reference for grouping events by person."""
    if value is None:
        return None
    digest = hmac.new(
        config.PSEUDONYM_SALT.encode("utf-8"),
        str(value).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return digest[:16]


def _today_path(now: Optional[datetime] = None) -> Path:
    now = now or datetime.now(timezone.utc)
    return config.DATASET_DIR / f"intake-events-{now:%Y-%m-%d}.jsonl"


def _append(record: Dict[str, Any]) -> bool:
    """Append one record. Returns success; never raises."""
    try:
        path = _today_path()
        with _write_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return True
    except Exception:  # noqa: BLE001 - a corpus write must never break a request
        logger.warning("intake_v1: failed to append corpus record", exc_info=True)
        return False


def extract_features(result: Dict[str, Any]) -> Dict[str, Any]:
    """Pull the whitelisted numeric feature vector out of a detector response."""
    debug = result.get("debug") or {}
    features: Dict[str, Any] = {}
    for key in FEATURE_KEYS:
        if key in debug:
            features[key] = debug[key]
        elif key in result:
            features[key] = result[key]
    return features


def log_event(
    result: Dict[str, Any],
    *,
    u_id: Any = None,
    session_id: Any = None,
) -> Optional[str]:
    """Record a closed detection event. Returns its `event_id`, or None.

    Idle frames — where the detector never closed an event — are skipped, so
    this is cheap to call on every frame.
    """
    if not config.COLLECT:
        return None

    policy = result.get("policy") or {}
    event_id = policy.get("event_id")
    if not event_id:
        return None

    record = {
        "schema_version": config.SCHEMA_VERSION,
        "type": RECORD_EVENT,
        "event_id": event_id,
        "ts": datetime.now(timezone.utc).isoformat(),
        "user_ref": _pseudonym(u_id),
        "session_ref": _pseudonym(session_id),
        "detector_band": policy.get("detector_band"),
        "detector_reason": policy.get("detector_reason"),
        "policy_band": policy.get("band"),
        "policy_reason": policy.get("reason"),
        "mode": policy.get("mode"),
        "downgraded": policy.get("downgraded"),
        "safety_contradiction": policy.get("safety_contradiction"),
        "features": extract_features(result),
        "outcome": None,  # filled in by a later `log_outcome` record
    }

    return event_id if _append(record) else None


def log_outcome(event_id: str, outcome: str, note: Optional[str] = None) -> bool:
    """Record what the person did about an event. This is the training label.

    Written as a separate append rather than an in-place edit, so the corpus
    stays append-only and safe to tail, copy, or ship mid-write.
    """
    if not config.COLLECT:
        return False
    if not event_id:
        return False
    if outcome not in VALID_OUTCOMES:
        logger.warning(
            "intake_v1: refusing to log unknown outcome %r (expected one of %s)",
            outcome,
            ", ".join(sorted(VALID_OUTCOMES)),
        )
        return False

    return _append({
        "schema_version": config.SCHEMA_VERSION,
        "type": RECORD_OUTCOME,
        "event_id": event_id,
        "ts": datetime.now(timezone.utc).isoformat(),
        "outcome": outcome,
        "note": note,
    })
