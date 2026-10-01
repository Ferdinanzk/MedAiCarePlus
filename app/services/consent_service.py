"""Append-only consent records and a short-lived per-user state cache."""

import time

from app import config
from app.database import get_pool
from app.services import legal_service

CACHE_TTL_SECONDS = 5.0
_cache: dict[int, tuple[float, dict | None]] = {}


class ConsentError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


async def fetch_state(conn, u_id: int) -> dict[str, dict]:
    rows = await conn.fetch(
        "SELECT DISTINCT ON (scope) scope, granted, terms_version, kind, consent_id, created_at "
        "FROM consent WHERE u_id=$1 ORDER BY scope, consent_id DESC",
        u_id,
    )
    return {row["scope"]: {key: row[key] for key in (
        "granted", "terms_version", "kind", "consent_id", "created_at")} for row in rows}


async def get_state(u_id: int) -> dict[str, dict]:
    now = time.monotonic()
    cached = _cache.get(u_id)
    if cached is not None and cached[1] is not None and now - cached[0] < CACHE_TTL_SECONDS:
        return {scope: dict(value) for scope, value in cached[1].items()}

    # A read started before invalidation must not repopulate the cache afterwards.
    pending = (now, None)
    _cache[u_id] = pending
    try:
        async with get_pool().acquire() as conn:
            state = await fetch_state(conn, u_id)
    except BaseException:
        if _cache.get(u_id) is pending:
            _cache.pop(u_id, None)
        raise
    if _cache.get(u_id) is pending:
        _cache[u_id] = (now, state)
    return {scope: dict(value) for scope, value in state.items()}


def invalidate(u_id: int) -> None:
    _cache.pop(u_id, None)


def is_current(state: dict, scope: str) -> bool:
    consent = state.get(scope, {})
    return consent.get("granted") is True and consent.get("terms_version") == config.TERMS_VERSION


async def record(conn, u_id: int, *, kind: str, terms_version: str, language: str,
                 document_sha256: str, scopes: dict[str, bool], source: str,
                 user_agent: str | None) -> None:
    """The caller owns the transaction and must invalidate again after commit."""
    if terms_version != config.TERMS_VERSION:
        raise ConsentError("stale_terms_version")
    if kind not in legal_service.KIND_SCOPES or not scopes or any(
        scope not in legal_service.KIND_SCOPES[kind] or type(granted) is not bool
        for scope, granted in scopes.items()
    ):
        raise ConsentError("invalid_scope")
    if source == "register" and (kind != "core" or scopes.get("core") is not True):
        raise ConsentError("core_required")
    doc = legal_service.get_document(kind, legal_service.normalise_language(language))
    if document_sha256 != doc.sha256:
        raise ConsentError("document_hash_mismatch")

    for scope, granted in scopes.items():
        await conn.execute(
            "INSERT INTO consent "
            "(u_id, kind, terms_version, language, document_sha256, scope, granted, source, user_agent) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
            u_id, kind, terms_version, doc.language, doc.sha256, scope, granted, source, user_agent,
        )
    invalidate(u_id)


def status_payload(state: dict) -> dict:
    scopes = {}
    for scope, kind in legal_service.SCOPE_KIND.items():
        scopes[scope] = {
            "granted": False, "terms_version": None, "kind": kind,
            "consent_id": None, "created_at": None,
            **state.get(scope, {}), "current": is_current(state, scope),
        }
    return {
        "terms_version": config.TERMS_VERSION,
        "core_current": is_current(state, "core"),
        "robot_current": all(is_current(state, scope) for scope in legal_service.KIND_SCOPES["robot"]),
        "scopes": scopes,
    }
