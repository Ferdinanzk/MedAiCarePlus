"""Restore-safe ledger: chat, memory and consent withdrawals replay by id and time."""

import asyncio
import json
import sys
import types
import uuid
from datetime import datetime, timezone

import pytest

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dependencies import get_current_user
from app.jobs import conversation_retention_job
from app.ops import replay_ledger
from app.routers import api_consent
from app.services import consent_service, deletion_ledger


class Conn:
    """Records every statement as (query, args, in_transaction)."""

    def __init__(self):
        self.queries = []
        self.in_transaction = False
        self.committed = False

    def transaction(self):
        conn = self

        class Tx:
            async def __aenter__(self):
                assert not conn.in_transaction
                conn.in_transaction = True

            async def __aexit__(self, exc_type, *args):
                conn.in_transaction = False
                conn.committed = exc_type is None
                return False
        return Tx()

    async def execute(self, query, *args):
        self.queries.append((" ".join(query.split()), args, self.in_transaction))
        return "INSERT 0 1"


class Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class Acquire:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *args):
                return False
        return Acquire()


def test_replay_deletes_conversations_and_memory_by_id_and_owner():
    chat, fact = str(uuid.uuid4()), str(uuid.uuid4())
    conn = Conn()
    entries = [{"kind": "conversation", "u_id": 7, "object_id": chat},
               {"kind": "memory", "u_id": 7, "object_id": fact}]
    assert asyncio.run(deletion_ledger.replay(conn, entries)) == 2
    assert [(query.split(" ")[0], args) for query, args, _ in conn.queries] == [
        ("INSERT", ("conversation", 7, chat)),
        ("DELETE", (chat, 7)),
        ("INSERT", ("memory", 7, fact)),
        ("DELETE", (fact, 7)),
    ]
    audit = conn.queries[0][0]
    assert audit.startswith("INSERT INTO deletion_ledger") and "WHERE NOT EXISTS" in audit
    # Untyped, Postgres deduces text (kind=$1) and varchar (insert target) for $1 and refuses to prepare it.
    assert "SELECT $1::text, $2, $3" in audit
    assert conn.queries[1][0] == "DELETE FROM conversation WHERE conversation_id = $1::uuid AND u_id = $2"
    assert conn.queries[3][0] == "DELETE FROM patient_memory WHERE memory_id = $1::uuid AND u_id = $2"
    assert all(in_transaction for _, _, in_transaction in conn.queries)


@pytest.mark.parametrize("entry", [
    {"kind": "conversation", "u_id": 7, "object_id": "not-a-uuid"},
    {"kind": "memory", "u_id": 7, "object_id": None},
    {"kind": "memory", "u_id": 7, "object_id": 12},
    {"kind": "whatever", "u_id": 7, "object_id": str(uuid.uuid4())},
    {"kind": "conversation", "u_id": "7", "object_id": str(uuid.uuid4())},
])
def test_replay_rejects_bad_uuid_or_unknown_kind(entry):
    conn = Conn()
    with pytest.raises(ValueError):
        asyncio.run(deletion_ledger.replay(conn, [entry]))
    assert conn.queries == []


def test_replay_withdraws_a_consent_only_if_the_restored_grant_is_older():
    conn = Conn()
    entry = {"kind": "consent", "u_id": 7, "object_id": "conversation_memory",
             "deleted_at": "2026-10-03T01:00:00+00:00"}
    assert asyncio.run(deletion_ledger.replay(conn, [entry])) == 1
    (audit, audit_args, _), (withdraw, args, in_transaction) = conn.queries
    assert audit.startswith("INSERT INTO deletion_ledger") and audit_args == ("consent", 7, "conversation_memory")
    assert withdraw.startswith("INSERT INTO consent")
    assert "SELECT u_id, kind, terms_version, language, document_sha256, scope, FALSE" in withdraw
    assert "WHERE consent_id = (SELECT max(consent_id) FROM consent WHERE u_id = $1 AND scope = $2)" in withdraw
    assert withdraw.endswith("AND granted AND created_at < $3")
    assert args == (7, "conversation_memory", datetime(2026, 10, 3, 1, tzinfo=timezone.utc))
    assert in_transaction

    for bad in ({**entry, "object_id": "everything"}, {**entry, "deleted_at": None}):
        conn = Conn()
        with pytest.raises(ValueError):
            asyncio.run(deletion_ledger.replay(conn, [bad]))
        assert conn.queries == []


def test_replay_file_runs_retention_before_clearing_the_marker(monkeypatch, tmp_path):
    path = tmp_path / "ledger.jsonl"
    path.write_text(json.dumps({"kind": "conversation", "u_id": 7, "object_id": str(uuid.uuid4())}) + "\n",
                    encoding="utf-8")
    conn = Conn()

    async def retention(c):
        assert c is conn
        conn.queries.append(("RETENTION", (), False))

    monkeypatch.setattr(conversation_retention_job, "run_retention", retention)
    assert asyncio.run(replay_ledger.replay_file(conn, path)) == 1
    order = [query for query, _, _ in conn.queries]
    assert order.index("RETENTION") < order.index("DELETE FROM ops_state WHERE key='restore_in_progress'")
    assert order[-1] == "DELETE FROM ops_state WHERE key='restore_in_progress'"


def test_withdrawn_scopes_are_ledgered(monkeypatch):
    conn = Conn()
    host = []
    monkeypatch.setattr(api_consent, "get_pool", lambda: Pool(conn))
    monkeypatch.setattr(deletion_ledger, "append_host_file",
                        lambda *entry: host.append((entry, conn.in_transaction, conn.committed)))

    async def record(*args, **kwargs):
        return None

    async def state(u_id):
        return {}

    monkeypatch.setattr(consent_service, "record", record)
    monkeypatch.setattr(consent_service, "get_state", state)
    app = FastAPI()
    app.include_router(api_consent.router)
    app.dependency_overrides[get_current_user] = lambda: {"u_id": 7, "name": "P"}
    client = TestClient(app)

    def post(kind, scopes):
        return client.post("/api/consent", json={
            "kind": kind, "terms_version": "2026-10", "language": "en", "document_sha256": "a" * 64,
            "scopes": scopes, "source": "settings"})

    assert post("memory", {"conversation_memory": False}).status_code == 200
    assert post("robot", {"cloud_voice": False, "conversation_analysis": True}).status_code == 200
    ledgered = [(args, in_transaction) for query, args, in_transaction in conn.queries
                if query.startswith("INSERT INTO deletion_ledger")]
    assert ledgered == [(("consent", 7, "conversation_memory"), True), (("consent", 7, "cloud_voice"), True)]
    assert host == [(("consent", 7, "conversation_memory"), False, True),
                    (("consent", 7, "cloud_voice"), False, True)]

    # A grant alone is not ledgered; a rejected withdrawal writes nothing to the host file.
    conn.queries.clear()
    host.clear()
    assert post("memory", {"conversation_memory": True}).status_code == 200

    async def reject(*args, **kwargs):
        raise consent_service.ConsentError("stale_terms_version")

    monkeypatch.setattr(consent_service, "record", reject)
    assert post("memory", {"conversation_memory": False}).status_code == 409
    assert not any(query.startswith("INSERT INTO deletion_ledger") for query, _, _ in conn.queries)
    assert host == []
