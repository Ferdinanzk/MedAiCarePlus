"""The AI connections tables: read into the snapshot, copied from .env on the first start, changed in one
transaction together with the re-worded notices, and refusing to drop what a job uses."""

import asyncio
import json
import sys
import types

import pytest

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app import config
from app.services import ai_crypto, ai_profile, ai_settings, ai_store, legal_service


class Conn:
    """Answers fetch by table name and fetchval from a queue; keeps every statement with its arguments."""

    def __init__(self, tables=None, values=()):
        self.tables, self.values, self.sql = tables or {}, list(values), []

    async def fetch(self, query, *args):
        self.sql.append((query, args))
        return next((rows for table, rows in self.tables.items() if f"FROM {table}" in query), [])

    async def fetchval(self, query, *args):
        self.sql.append((query, args))
        return self.values.pop(0) if self.values else None

    async def execute(self, query, *args):
        self.sql.append((query, args))

    def transaction(self):
        sql = self.sql

        class Transaction:
            async def __aenter__(self):
                sql.append("BEGIN")

            async def __aexit__(self, exc_type, *rest):
                sql.append("ROLLBACK" if exc_type else "COMMIT")
                return False
        return Transaction()

    def statements(self, prefix):
        return [args for entry in self.sql if isinstance(entry, tuple)
                for query, args in [entry] if query.startswith(prefix)]


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(ai_settings, "_snapshot", None)
    monkeypatch.setattr(config, "SECRET_KEY", "the-secret")


def _env(monkeypatch, model="google/gemini-2.5-flash-lite", fallback="google/gemini-2.5-flash"):
    for key, value in {"OPENROUTER_API_KEY": "sk-or-v1-env-4321", "LLM_MODEL": model, "LLM_FALLBACK_MODEL": fallback,
                       "LLM_SERVICE": "OpenRouter", "LLM_PROVIDER": "Google", "LLM_PROVIDER_REGION": "global",
                       "LLM_RETENTION": "not kept"}.items():
        monkeypatch.setattr(config, key, value)


def test_load_reads_keys_and_profiles():
    rows = {
        "ai_connection": [
            {"connection_id": 1, "name": "OpenAI", "kind": "openai", "base_url": ai_profile.BASE_URLS["openai"],
             "api_key_enc": ai_crypto.encrypt("sk-proj-k-1234"), "api_key_last4": "1234", "service_name": "OpenAI"},
            {"connection_id": 2, "name": "Old", "kind": "openai", "base_url": ai_profile.BASE_URLS["openai"],
             "api_key_enc": ai_crypto.encrypt("sk-proj-k-5678", secret="an-older-secret"), "api_key_last4": "5678",
             "service_name": "OpenAI"},
            {"connection_id": 3, "name": "Empty", "kind": "openrouter", "base_url": ai_profile.BASE_URLS["openrouter"],
             "api_key_enc": None, "api_key_last4": None, "service_name": "OpenRouter"}],
        "ai_model": [{"model_ref": 11, "connection_id": 1, "model_id": "gpt-5.4-mini", "company": "OpenAI",
                      "region": "USA", "retention": "30 days", "tested_at": None, "test_ms": None, "last_error": None,
                      "profile": json.dumps({"token_param": "max_completion_tokens", "temperature": True,
                                             "reasoning_off": None})}],
        "ai_job": [{"job": "reply", "primary_ref": 11, "fallback_ref": None}]}
    snapshot = asyncio.run(ai_store.load(Conn(rows)))
    assert snapshot.connections[1].api_key == "sk-proj-k-1234" and snapshot.connections[1].key_readable
    # SECRET_KEY changed since this key was saved: kept, unreadable, and startup goes on (review focus 5)
    assert snapshot.connections[2].api_key == "" and snapshot.connections[2].key_readable is False
    assert snapshot.connections[3].api_key == "" and snapshot.connections[3].key_readable is True
    assert snapshot.models[11].profile["token_param"] == "max_completion_tokens"
    assert snapshot.jobs == {"reply": (11, None)}


def test_the_first_start_copies_the_env_setup(monkeypatch):
    _env(monkeypatch)
    conn = Conn(values=[False, 1, 11, 12])   # the tables are empty, then the new ids
    assert asyncio.run(ai_store.seed_from_env(conn)) is True
    (connection,) = conn.statements("INSERT INTO ai_connection")
    assert connection[:3] == ("OpenRouter", "openrouter", "https://openrouter.ai/api/v1")
    assert ai_crypto.decrypt(connection[3]) == "sk-or-v1-env-4321" and connection[4] == "4321"
    models = conn.statements("INSERT INTO ai_model")
    assert [model[1] for model in models] == ["google/gemini-2.5-flash-lite", "google/gemini-2.5-flash"]
    assert models[0][2:5] == ("Google", "global", "not kept")
    assert json.loads(models[0][5]) == ai_profile.starting_profile("openrouter")
    assert conn.statements("INSERT INTO ai_job") == [(job, 11, 12) for job in ai_settings.JOBS]
    assert conn.statements("INSERT INTO ops_state") == [()]
    assert conn.sql[0] == "BEGIN" and conn.sql[-1] == "COMMIT"
    # the table is locked first, before the emptiness check, so two starts cannot both copy
    assert conn.sql[1][0].startswith("LOCK TABLE ai_connection") and "EXISTS" in conn.sql[2][0]


def test_an_empty_llm_model_uses_the_fallback_as_main_and_backup(monkeypatch):
    _env(monkeypatch, model="")
    conn = Conn(values=[False, 1, 11])
    asyncio.run(ai_store.seed_from_env(conn))
    (model,) = conn.statements("INSERT INTO ai_model")
    assert model[1] == "google/gemini-2.5-flash"
    assert conn.statements("INSERT INTO ai_job") == [(job, 11, 11) for job in ai_settings.JOBS]


def test_an_empty_env_key_seeds_no_key(monkeypatch):
    _env(monkeypatch)
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
    conn = Conn(values=[False, 1, 11, 12])
    asyncio.run(ai_store.seed_from_env(conn))
    (connection,) = conn.statements("INSERT INTO ai_connection")
    assert connection[3] is None and connection[4] is None


def test_equal_env_models_make_one_model_tried_twice(monkeypatch):
    _env(monkeypatch, model="google/gemini-2.5-flash", fallback="google/gemini-2.5-flash")
    conn = Conn(values=[False, 1, 11])
    asyncio.run(ai_store.seed_from_env(conn))
    assert len(conn.statements("INSERT INTO ai_model")) == 1
    assert conn.statements("INSERT INTO ai_job") == [(job, 11, 11) for job in ai_settings.JOBS]


def test_a_second_start_copies_nothing(monkeypatch):
    _env(monkeypatch)
    conn = Conn(values=[True])
    assert asyncio.run(ai_store.seed_from_env(conn)) is False
    assert conn.statements("INSERT") == []


def test_a_first_start_that_will_be_refused_copies_nothing(monkeypatch):
    """In prod with the robot, an incomplete .env stops the first start before the copy. Once copied, .env is no
    longer read, so fixing it would not help and every later start would exit too."""
    _env(monkeypatch)
    monkeypatch.setattr(config, "LLM_PROVIDER_REGION", "")
    monkeypatch.setattr(config, "REACHY_FEATURE_ENABLED", True)
    monkeypatch.setattr(config, "APP_ENV", "prod")
    conn = Conn(values=[False])
    with pytest.raises(SystemExit):
        asyncio.run(ai_store.seed_from_env(conn))
    assert conn.statements("INSERT") == [] and conn.sql[-1] == "ROLLBACK"
    monkeypatch.setattr(config, "LLM_PROVIDER_REGION", "global")   # .env fixed: the next start copies it
    conn = Conn(values=[False, 1, 11, 12])
    assert asyncio.run(ai_store.seed_from_env(conn)) is True and conn.sql[-1] == "COMMIT"


def test_a_change_reloads_the_snapshot_and_registers_the_notices(monkeypatch):
    new = ai_settings.Snapshot({}, {}, {}, None)
    registered = []

    async def load(conn):
        return new

    async def register(conn):
        registered.append(legal_service.load_documents.cache_info().currsize)

    monkeypatch.setattr(ai_store, "load", load)
    monkeypatch.setattr(legal_service, "register_documents", register)
    conn = Conn()

    async def run():
        async with ai_store.change(conn):
            await conn.execute("UPDATE ai_model SET region = $2 WHERE model_ref = $1", 11, "USA")

    asyncio.run(run())
    assert ai_settings.loaded() is new and registered == [0]   # the notices were re-rendered for the new setup
    assert conn.sql[0] == "BEGIN" and conn.sql[-1] == "COMMIT"


def test_a_failed_change_keeps_the_previous_snapshot(monkeypatch):
    previous = ai_settings.Snapshot({}, {}, {"reply": (1, None)}, None)
    monkeypatch.setattr(ai_settings, "_snapshot", previous)
    conn = Conn()

    async def run():
        async with ai_store.change(conn):
            raise ai_store.InUse()

    with pytest.raises(ai_store.InUse):
        asyncio.run(run())
    assert ai_settings.loaded() is previous and conn.sql[-1] == "ROLLBACK"


def test_a_change_that_fails_after_the_reload_restores_the_previous_snapshot(monkeypatch):
    previous = ai_settings.Snapshot({}, {}, {"reply": (1, None)}, None)
    new = ai_settings.Snapshot({}, {}, {"reply": (2, None)}, None)
    monkeypatch.setattr(ai_settings, "_snapshot", previous)

    async def load(conn):
        return new

    async def register(conn):
        raise RuntimeError("registering the notices failed")

    monkeypatch.setattr(ai_store, "load", load)
    monkeypatch.setattr(legal_service, "register_documents", register)
    conn = Conn()

    async def run():
        async with ai_store.change(conn):
            await conn.execute("UPDATE ai_model SET region = $2 WHERE model_ref = $1", 11, "USA")

    with pytest.raises(RuntimeError):
        asyncio.run(run())
    assert ai_settings.loaded() is previous and conn.sql[-1] == "ROLLBACK"
    assert legal_service.load_documents.cache_info().currsize == 0


def test_what_a_job_uses_cannot_be_deleted():
    with pytest.raises(ai_store.InUse):
        asyncio.run(ai_store.delete_model(Conn(values=[True]), 21))
    conn = Conn(values=[True])
    with pytest.raises(ai_store.InUse):
        asyncio.run(ai_store.delete_connection(conn, 2))
    assert "fallback_ref" in conn.sql[0][0]   # a backup counts as in use too (review focus 4)
    conn = Conn(values=[False])
    asyncio.run(ai_store.delete_connection(conn, 2))
    assert [query.split(" WHERE")[0] for query, _ in conn.sql[1:]] == ["DELETE FROM ai_model",
                                                                        "DELETE FROM ai_connection"]


def test_an_update_sets_only_the_given_fields_and_encrypts_a_new_key():
    conn = Conn()
    asyncio.run(ai_store.update_connection(conn, 2, api_key="sk-proj-new-7777"))
    ((query, args),) = conn.sql
    assert query.startswith("UPDATE ai_connection SET api_key_enc = $2, api_key_last4 = $3,")
    assert args[0] == 2 and ai_crypto.decrypt(args[1]) == "sk-proj-new-7777" and args[2] == "7777"


def test_a_call_result_is_written_and_kept_in_memory(monkeypatch):
    conn = Conn()

    class Pool:
        def acquire(self):
            class Ctx:
                async def __aenter__(self):
                    return conn

                async def __aexit__(self, *exc):
                    return False
            return Ctx()

    monkeypatch.setattr(ai_store, "get_pool", lambda: Pool())
    monkeypatch.setattr(ai_settings, "_snapshot",
                        ai_settings.Snapshot({}, {11: ai_settings.Model(11, 1, "gpt-5.4-mini")}, {}, None))
    asyncio.run(ai_store.note_call(11, "HTTP 503"))
    assert ai_settings.current().models[11].last_error == "HTTP 503"
    assert "last_error IS DISTINCT FROM" in conn.sql[0][0]


def test_a_note_never_raises_without_a_database(monkeypatch):
    def no_pool():
        raise RuntimeError("Database pool not initialised")

    monkeypatch.setattr(ai_store, "get_pool", no_pool)
    monkeypatch.setattr(ai_settings, "_snapshot",
                        ai_settings.Snapshot({}, {11: ai_settings.Model(11, 1, "gpt-5.4-mini")}, {}, None))
    asyncio.run(ai_store.note_call(11, "HTTP 503"))
    assert ai_settings.current().models[11].last_error is None   # memory not updated when the write failed
