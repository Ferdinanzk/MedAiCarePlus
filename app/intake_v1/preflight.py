"""Startup safety checks.

Two production failure modes motivated this module, and both share a shape: the
application starts successfully, reports healthy, and is quietly wrong.

1. `SECRET_KEY` falls back to a value published in the repository
   (`app/config.py`). If `.env` is missing on the server the app boots normally
   and every face-session token becomes forgeable by anyone who can read the
   source. On an app holding medication records, that is an auth bypass.

2. `app/main.py` calls `ml_stubs.install()` unconditionally. It tries the real
   import first, so stubs are a fallback rather than the default — but when a
   dependency is missing from a rebuilt image, `_FakeFaceMesh` returns 468
   landmarks all at (0.5, 0.5), `_FakeYOLO` returns nothing, and the only signal
   is a `warnings.warn` that nobody reads.

Both are cheap to detect at startup and expensive to discover in production.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, asdict
from typing import Any, Dict, Iterable, List, Mapping, Optional

from app.intake_v1 import config

logger = logging.getLogger(__name__)

CRITICAL = "critical"
WARNING = "warning"

# Must match the fallback in app/config.py.
DEFAULT_SECRET_KEY = "change-me-in-production-32chars!!"
MIN_SECRET_KEY_LENGTH = 32

# Module names ml_stubs can replace, and the marker its fakes carry.
STUBBABLE_MODULES = (
    "torch",
    "torchvision",
    "cv2",
    "mediapipe",
    "ultralytics",
    "openvino",
)
STUB_TYPE_PREFIX = "_Fake"


@dataclass(frozen=True)
class Finding:
    level: str
    code: str
    message: str
    remedy: str


def check_secret_key(value: Optional[str] = None) -> List[Finding]:
    """Verify the session-signing key is a real secret."""
    if value is None:
        value = os.getenv("SECRET_KEY")

    if not value:
        return [Finding(
            CRITICAL,
            "secret_key_unset",
            "SECRET_KEY is not set, so app/config.py falls back to a value "
            "published in this repository. Every session token is forgeable.",
            "Set SECRET_KEY in .env to a random value of at least "
            f"{MIN_SECRET_KEY_LENGTH} characters.",
        )]

    if value == DEFAULT_SECRET_KEY:
        return [Finding(
            CRITICAL,
            "secret_key_is_default",
            "SECRET_KEY is still the published default from app/config.py. "
            "Every session token is forgeable by anyone who can read the source.",
            "Set SECRET_KEY in .env to a random value of at least "
            f"{MIN_SECRET_KEY_LENGTH} characters.",
        )]

    if len(value) < MIN_SECRET_KEY_LENGTH:
        return [Finding(
            WARNING,
            "secret_key_too_short",
            f"SECRET_KEY is {len(value)} characters; "
            f"{MIN_SECRET_KEY_LENGTH} or more is recommended.",
            "Regenerate it with `python -c \"import secrets; "
            "print(secrets.token_urlsafe(48))\"`.",
        )]

    return []


def check_ml_stubs(modules: Optional[Mapping[str, Any]] = None) -> List[Finding]:
    """Detect ml_stubs fakes standing in for real ML packages."""
    modules = sys.modules if modules is None else modules
    findings: List[Finding] = []

    for name in STUBBABLE_MODULES:
        module = modules.get(name)
        if module is None:
            continue
        if not type(module).__name__.startswith(STUB_TYPE_PREFIX):
            continue
        findings.append(Finding(
            CRITICAL,
            f"ml_stub_active:{name}",
            f"'{name}' is an ml_stubs fake, not the real package. Inference "
            "using it returns fabricated results while the service still "
            "reports healthy.",
            f"Install {name} in the image, or set INTAKE_V1_ALLOW_UNSAFE=1 to "
            "run degraded on purpose (never in production).",
        ))

    return findings


def check_database_url(value: Optional[str] = None) -> List[Finding]:
    """Flag the development Postgres credentials."""
    if value is None:
        value = os.getenv("DATABASE_URL", "")

    if "medai:medai@" in value:
        return [Finding(
            WARNING,
            "database_default_credentials",
            "DATABASE_URL uses the development credentials medai/medai from "
            "docker-compose.yml.",
            "Set a generated password for the Postgres user before exposing "
            "this host, and stop publishing port 5432.",
        )]

    return []


def check_pseudonym_salt() -> List[Finding]:
    """Warn when corpus pseudonyms are unsalted."""
    if config.PSEUDONYM_SALT == "intake-v1-unsalted":
        return [Finding(
            WARNING,
            "pseudonym_salt_unset",
            "Neither INTAKE_V1_PSEUDONYM_SALT nor SECRET_KEY is set, so corpus "
            "user references use a constant salt and are guessable.",
            "Set INTAKE_V1_PSEUDONYM_SALT to its own random value.",
        )]
    return []


#: Every available check, by name. A deployment that does not use a given
#: subsystem skips its check rather than being blocked by an irrelevant finding
#: — the standalone detection build has no auth and no database, for instance.
CHECKS = {
    "secret_key": check_secret_key,
    "ml_stubs": check_ml_stubs,
    "database": check_database_url,
    "pseudonym_salt": check_pseudonym_salt,
}


def run_checks(skip: Optional[Iterable[str]] = None) -> List[Finding]:
    """Run the applicable checks. Criticals first, so a truncated log shows them.

    `skip` names checks that do not apply to this deployment. An unknown name
    raises rather than being ignored, so a typo cannot silently disable a check.
    """
    skipped = set(skip or ())
    unknown = skipped - set(CHECKS)
    if unknown:
        raise ValueError(
            f"unknown preflight check(s): {', '.join(sorted(unknown))}. "
            f"Valid names: {', '.join(sorted(CHECKS))}"
        )

    findings: List[Finding] = []
    for name, check in CHECKS.items():
        if name not in skipped:
            findings.extend(check())

    return sorted(findings, key=lambda f: 0 if f.level == CRITICAL else 1)


def assert_safe(
    findings: Optional[List[Finding]] = None,
    *,
    skip: Optional[Iterable[str]] = None,
) -> List[Finding]:
    """Fail startup on any critical finding.

    Set `INTAKE_V1_ALLOW_UNSAFE=1` to downgrade criticals to logged warnings —
    useful for local frontend work against stubs, never correct in production.

    Returns the findings so a caller can surface them in `/health`.
    """
    findings = run_checks(skip=skip) if findings is None else findings

    for finding in findings:
        log = logger.error if finding.level == CRITICAL else logger.warning
        log("intake_v1 preflight [%s] %s — %s",
            finding.code, finding.message, finding.remedy)

    criticals = [f for f in findings if f.level == CRITICAL]
    if criticals and not config.ALLOW_UNSAFE:
        detail = "\n".join(f"  - [{f.code}] {f.message}\n    fix: {f.remedy}"
                           for f in criticals)
        raise RuntimeError(
            "intake_v1 preflight failed with "
            f"{len(criticals)} critical finding(s):\n{detail}\n"
            "Set INTAKE_V1_ALLOW_UNSAFE=1 to start anyway (never in production)."
        )

    if criticals:
        logger.error(
            "intake_v1: starting with %d unresolved critical finding(s) because "
            "INTAKE_V1_ALLOW_UNSAFE is set. Results may be fabricated.",
            len(criticals),
        )

    return findings


def health_report(skip: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """Preflight summary suitable for the /health endpoint."""
    findings = run_checks(skip=skip)
    criticals = [f for f in findings if f.level == CRITICAL]
    return {
        "safe": not criticals,
        "allow_unsafe": config.ALLOW_UNSAFE,
        "mode": config.MODE,
        "findings": [asdict(f) for f in findings],
    }
