"""The AI connections tables (sql/init.sql): reading them into the snapshot, copying .env in on the first start, and
the writes the card makes. Every change runs inside change(), which reloads the snapshot and registers the
re-worded notices in the same transaction (consent rows reference legal_document by hash)."""

import dataclasses
import logging

from app.database import get_pool
from app.services import ai_settings

log = logging.getLogger(__name__)


async def note_call(model_ref: int, error: str | None) -> None:
    """A real call's outcome for the card ("last call failed"): written only when it changes, and kept in the
    snapshot so the next call compares against it. Never raises: it must not cost a reply."""
    try:
        async with get_pool().acquire() as conn:
            await conn.execute("UPDATE ai_model SET last_error = $2 WHERE model_ref = $1 "
                               "AND last_error IS DISTINCT FROM $2", model_ref, error)
        snapshot = ai_settings.loaded()
        if snapshot is not None and model_ref in snapshot.models:
            ai_settings.set_current(ai_settings.with_model(
                snapshot, dataclasses.replace(snapshot.models[model_ref], last_error=error)))
    except Exception:
        log.exception("could not record the last call of AI model %s", model_ref)
