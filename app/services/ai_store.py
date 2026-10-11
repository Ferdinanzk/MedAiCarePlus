"""The AI connections tables (sql/init.sql): reading them into the snapshot, copying .env in on the first start, and
the writes the card makes. Every change runs inside change(), which reloads the snapshot and registers the
re-worded notices in the same transaction (consent rows reference legal_document by hash)."""

import dataclasses
import json
import logging
from contextlib import asynccontextmanager

from app import config, startup_checks
from app.database import get_pool
from app.services import ai_crypto, ai_profile, ai_settings, legal_service

log = logging.getLogger(__name__)
_IN_USE = "SELECT EXISTS (SELECT 1 FROM ai_job j JOIN ai_model m ON m.model_ref IN (j.primary_ref, j.fallback_ref) "


class InUse(Exception):
    """A job still uses this connection or model, as its main model or its backup."""


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


async def load(conn) -> ai_settings.Snapshot:
    connections = {}
    for row in await conn.fetch("SELECT connection_id, name, kind, base_url, api_key_enc, api_key_last4, "
                                "service_name FROM ai_connection ORDER BY connection_id"):
        key = ai_crypto.decrypt(row["api_key_enc"]) if row["api_key_enc"] else ""
        connections[row["connection_id"]] = ai_settings.Connection(
            row["connection_id"], row["name"], row["kind"], row["base_url"], key or "", row["api_key_last4"],
            key is not None, row["service_name"])
    models = {row["model_ref"]: ai_settings.Model(
                  row["model_ref"], row["connection_id"], row["model_id"], row["company"], row["region"],
                  row["retention"], _json(row["profile"]), row["tested_at"], row["test_ms"], row["last_error"])
              for row in await conn.fetch("SELECT model_ref, connection_id, model_id, company, region, retention, "
                                          "profile, tested_at, test_ms, last_error FROM ai_model ORDER BY model_ref")}
    jobs, thinking = {}, {job: "off" for job in ai_settings.JOBS}
    for row in await conn.fetch("SELECT job, primary_ref, fallback_ref, thinking FROM ai_job"):
        jobs[row["job"]] = (row["primary_ref"], row["fallback_ref"])
        thinking[row["job"]] = row["thinking"]
    seeded_at = await conn.fetchval("SELECT updated_at FROM ops_state WHERE key = 'ai_seeded_from_env'")
    return ai_settings.Snapshot(connections, models, jobs, seeded_at, thinking)


async def seed_from_env(conn) -> bool:
    """Copy the .env setup into empty tables (spec §6), so day one works exactly as before; True when it did. The
    table is locked in one transaction, so two starts cannot both copy.

    A setup this start will refuse (startup_checks.check_ai_settings: prod with the robot feature) is refused here,
    before anything is copied: once copied, .env is no longer read, so fixing it and starting again would not help."""
    async with conn.transaction():
        await conn.execute("LOCK TABLE ai_connection IN EXCLUSIVE MODE")
        if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM ai_connection)"):
            return False
        startup_checks.check_ai_settings(ai_settings.env_snapshot())
        connection_id = await add_connection(
            conn, name="OpenRouter", kind="openrouter", base_url=ai_profile.BASE_URLS["openrouter"],
            api_key=config.OPENROUTER_API_KEY.strip(), service_name=config.LLM_SERVICE or "OpenRouter")
        fallback = config.LLM_FALLBACK_MODEL
        main = config.LLM_MODEL or fallback
        refs = {}
        for model_id in dict.fromkeys([main, fallback]):
            refs[model_id] = await add_model(
                conn, connection_id=connection_id, model_id=model_id, company=config.LLM_PROVIDER,
                region=config.LLM_PROVIDER_REGION, retention=config.LLM_RETENTION,
                profile=ai_profile.starting_profile("openrouter"))
        for job in ai_settings.JOBS:
            await conn.execute("INSERT INTO ai_job (job, primary_ref, fallback_ref) VALUES ($1, $2, $3)",
                               job, refs[main], refs[fallback])
        await conn.execute("INSERT INTO ops_state (key) VALUES ('ai_seeded_from_env') ON CONFLICT (key) DO NOTHING")
    return True


async def fill_openrouter_keys_from_env(api_key: str | None) -> int:
    """Every start, after the first-start copy: each OpenRouter connection with no key gets .env's
    OPENROUTER_API_KEY (spec C2), in one change(); returns how many. The first start copies whatever .env held then,
    so a key added to .env later never reached the card. A connection that has a key (even one this SECRET_KEY
    can't read) is never touched: the card stays the place to change keys. The key is never logged."""
    api_key = (api_key or "").strip()
    if not api_key:
        return 0
    async with get_pool().acquire() as conn:
        rows = await conn.fetch("SELECT connection_id, kind, api_key_enc FROM ai_connection ORDER BY connection_id")
        keyless = [row["connection_id"] for row in rows if row["kind"] == "openrouter" and row["api_key_enc"] is None]
        if not keyless:
            return 0
        async with change(conn):
            for connection_id in keyless:
                await conn.execute("UPDATE ai_connection SET api_key_enc = $2, api_key_last4 = $3, updated_at = NOW() "
                                   "WHERE connection_id = $1 AND api_key_enc IS NULL", connection_id,
                                   ai_crypto.encrypt(api_key), ai_crypto.last4(api_key))
    log.info("filled the key of %d OpenRouter connection(s) from .env", len(keyless))
    return len(keyless)


@asynccontextmanager
async def change(conn):
    """One change on the card: the caller's writes, then the new snapshot and the re-worded notices registered, all in
    one transaction (consent rows reference legal_document by hash). On any failure the previous snapshot stays."""
    previous = ai_settings.loaded()
    try:
        async with conn.transaction():
            yield
            ai_settings.set_current(await load(conn))
            legal_service.load_documents.cache_clear()
            await legal_service.register_documents(conn)
    except BaseException:
        ai_settings.set_current(previous)
        legal_service.load_documents.cache_clear()
        raise


async def add_connection(conn, *, name: str, kind: str, base_url: str, api_key: str, service_name: str) -> int:
    return await conn.fetchval(
        "INSERT INTO ai_connection (name, kind, base_url, api_key_enc, api_key_last4, service_name) "
        "VALUES ($1, $2, $3, $4, $5, $6) RETURNING connection_id",
        name, kind, base_url, ai_crypto.encrypt(api_key) if api_key else None,
        ai_crypto.last4(api_key) if api_key else None, service_name)


async def update_connection(conn, connection_id: int, *, name: str | None = None, base_url: str | None = None,
                            api_key: str | None = None, service_name: str | None = None) -> None:
    fields = {"name": name, "base_url": base_url, "service_name": service_name}
    if api_key:
        fields.update(api_key_enc=ai_crypto.encrypt(api_key), api_key_last4=ai_crypto.last4(api_key))
    sets = [(column, value) for column, value in fields.items() if value is not None]
    if not sets:
        return
    assignments = ", ".join(f"{column} = ${index}" for index, (column, _) in enumerate(sets, start=2))
    await conn.execute(f"UPDATE ai_connection SET {assignments}, updated_at = NOW() WHERE connection_id = $1",
                       connection_id, *[value for _, value in sets])


async def delete_connection(conn, connection_id: int) -> None:
    if await conn.fetchval(_IN_USE + "WHERE m.connection_id = $1)", connection_id):
        raise InUse()
    await conn.execute("DELETE FROM ai_model WHERE connection_id = $1", connection_id)
    await conn.execute("DELETE FROM ai_connection WHERE connection_id = $1", connection_id)


async def add_model(conn, *, connection_id: int, model_id: str, company: str, region: str, retention: str,
                    profile: dict | None = None) -> int:
    return await conn.fetchval(
        "INSERT INTO ai_model (connection_id, model_id, company, region, retention, profile) "
        "VALUES ($1, $2, $3, $4, $5, $6::jsonb) RETURNING model_ref",
        connection_id, model_id, company, region, retention, json.dumps(profile) if profile is not None else None)


async def update_model(conn, model_ref: int, *, company: str, region: str, retention: str) -> None:
    await conn.execute("UPDATE ai_model SET company = $2, region = $3, retention = $4 WHERE model_ref = $1",
                       model_ref, company, region, retention)


async def delete_model(conn, model_ref: int) -> None:
    if await conn.fetchval(_IN_USE + "WHERE m.model_ref = $1)", model_ref):
        raise InUse()
    await conn.execute("DELETE FROM ai_model WHERE model_ref = $1", model_ref)


async def save_test(conn, model_ref: int, result: dict) -> None:
    """The Test button's outcome: a found profile and its time, or the reason it failed (the profile stays)."""
    if result["ok"]:
        await conn.execute("UPDATE ai_model SET profile = $2::jsonb, tested_at = NOW(), test_ms = $3, "
                           "last_error = NULL WHERE model_ref = $1", model_ref, json.dumps(result["profile"]),
                           result["ms"])
    else:
        await conn.execute("UPDATE ai_model SET last_error = $2 WHERE model_ref = $1", model_ref, result["error"])


async def set_jobs(conn, jobs: dict[str, tuple[int, int | None]], thinking: dict[str, str] | None = None) -> None:
    """Each job's main and backup model and its thinking level; a job missing from `thinking` is set to "off"."""
    thinking = thinking or {}
    for job, (primary_ref, fallback_ref) in jobs.items():
        await conn.execute(
            "INSERT INTO ai_job (job, primary_ref, fallback_ref, thinking) VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (job) DO UPDATE SET primary_ref = EXCLUDED.primary_ref, "
            "fallback_ref = EXCLUDED.fallback_ref, thinking = EXCLUDED.thinking, updated_at = NOW()",
            job, primary_ref, fallback_ref, thinking.get(job, "off"))


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
