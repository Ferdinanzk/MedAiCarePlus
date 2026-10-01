"""Environment-driven configuration for the v1 intake policy.

Values are read once at import. There is no logic here — see `policy.py`.
"""

from __future__ import annotations

import os
from pathlib import Path

# ── Operating mode ───────────────────────────────────────────────────────────
# assisted    — auto-confirm only on strong evidence, prompt otherwise (v1)
# manual_only — never auto-confirm; every real event becomes a prompt
# autonomous  — pass the detector's own bands through unchanged (v0 behaviour)
MODE_ASSISTED = "assisted"
MODE_MANUAL_ONLY = "manual_only"
MODE_AUTONOMOUS = "autonomous"

VALID_MODES = (MODE_ASSISTED, MODE_MANUAL_ONLY, MODE_AUTONOMOUS)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_mode() -> str:
    raw = (os.getenv("INTAKE_V1_MODE") or MODE_ASSISTED).strip().lower()
    return raw if raw in VALID_MODES else MODE_ASSISTED


MODE: str = _env_mode()

# Event confidence needed to auto-log a dose without asking. Deliberately far
# above the detector's own CONFIRM_THRESHOLD of 0.40: at 0.40 genuine intakes and
# false positives share the same band, so v1 only auto-logs the unambiguous top.
AUTO_CONFIRM_MIN: float = _env_float("INTAKE_V1_AUTO_CONFIRM_MIN", 0.75)

# Event confidence needed to surface a one-tap prompt.
PROMPT_MIN: float = _env_float("INTAKE_V1_PROMPT_MIN", 0.30)

# Mouth-behaviour rollout controls. Both features are independently reversible;
# tongue support is deliberately capped inside the detector's uncertain band.
ADAPTIVE_MOUTH: bool = _env_flag("INTAKE_V1_ADAPTIVE_MOUTH", True)
TONGUE_SUPPORT: bool = _env_flag("INTAKE_V1_TONGUE_SUPPORT", True)

# ── Training corpus collection ───────────────────────────────────────────────
COLLECT: bool = _env_flag("INTAKE_V1_COLLECT", True)

DATASET_DIR: Path = Path(
    os.getenv("INTAKE_V1_DATASET_DIR", "data/intake_events")
)

# Salt for pseudonymising user and session identifiers in the corpus. Falls back
# to SECRET_KEY; set it separately in production so rotating one key does not
# silently re-key the other and split one person into two pseudonyms.
PSEUDONYM_SALT: str = (
    os.getenv("INTAKE_V1_PSEUDONYM_SALT")
    or os.getenv("SECRET_KEY")
    or "intake-v1-unsalted"
)

# ── Safety ───────────────────────────────────────────────────────────────────
# Downgrades preflight criticals to warnings. Never set this in production.
ALLOW_UNSAFE: bool = _env_flag("INTAKE_V1_ALLOW_UNSAFE", False)

# Bumped whenever the logged record layout changes, so a corpus assembled across
# several deployments can still be parsed correctly.
SCHEMA_VERSION = 2
