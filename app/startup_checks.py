"""Fail-fast configuration and restore checks."""

import logging
from collections.abc import Mapping

from app import config

logger = logging.getLogger(__name__)


def run_startup_checks(env: Mapping[str, str] | None = None) -> list[str]:
    def value(key, default=""):
        return env.get(key, default) if env is not None else getattr(config, key, default)

    problems = []
    secret = str(value("SECRET_KEY", "change-me-in-production-32chars!!")).strip()
    if not secret or secret == "change-me-in-production-32chars!!":
        problems.append("SECRET_KEY must be changed from its default")
    required = ["LINE_CHANNEL_SECRET", "OPERATOR_NAME", "OPERATOR_CONTACT", "TUNNEL_PROVIDER"]
    if str(value("REACHY_FEATURE_ENABLED")).strip().lower() in ("1", "true", "yes"):
        required += ["LLM_PROVIDER", "LLM_PROVIDER_REGION", "LLM_RETENTION",
                     "RISK_CLASSIFIER_API_KEY"]
    for key in required:
        if not str(value(key)).strip():
            problems.append(f"{key} is required")
    for problem in problems:
        logger.warning("Startup check: %s", problem)
    if problems and str(value("APP_ENV", "dev")).strip().lower() == "prod":
        raise SystemExit(1)
    return problems


async def check_restore_state(conn) -> None:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM ops_state WHERE key='restore_in_progress')"):
        logger.error("Restore is incomplete; replay the deletion ledger before starting the app")
        raise SystemExit(1)
