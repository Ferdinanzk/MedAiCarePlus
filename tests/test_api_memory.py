"""The patient's memory API: limited-mode view and delete, consent-gated add and correct, the lock and the ledger."""

import sys
import types
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app import config
from app.dependencies import get_current_user
from app.routers import api_memory
from app.services import consent_service, deletion_ledger, memory

NOW = datetime(2026, 10, 3, 2, 0, tzinfo=timezone.utc)
CORE_ONLY = {"core": {"granted": True, "terms_version": config.TERMS_VERSION}}
MEMORY_ON = {scope: {"granted": True, "terms_version": config.TERMS_VERSION} for scope in memory.MEMORY_SCOPES}
LOCK = 'FROM "user" WHERE u_id = $1 FOR UPDATE'
UPSERT = "ON CONFLICT (u_id, kind, subject) WHERE source = 'patient' DO UPDATE"


class Conn:
    """Records every query as (method, sql, args) and answers from canned rows."""

    def __init__(self, consent=None, facts=(), chats=(), rows=()):
        self.consent = dict(MEMORY_ON if consent is None else consent)   # what the locked re-read sees
        self.facts = [dict(f) for f in facts]                             # current_facts rows
        self.chats = [dict(c) for c in chats]                             # array_agg rows
        self.rows = [dict(r) for r in rows]                               # patient_memory rows a DELETE can hit
        self.queries = []
        self.events = []

    def transaction(self):
        conn = self

        class Tx:
            async def __aenter__(self):
                conn.events.append("begin")

            async def __aexit__(self, *exc):
                conn.events.append("commit" if exc[0] is None else "rollback")
                return False
        return Tx()

    def sql(self, needle):
        return [(sql, args) for _, sql, args in self.queries if needle in sql]

    async def execute(self, query, *args):
        self.queries.append(("execute", query, args))
        self.events.append(query)
        return "INSERT 0 1" if query.startswith("INSERT") else "SELECT 1"

    async def fetch(self, query, *args):
        self.queries.append(("fetch", query, args))
        self.events.append(query)
        if "FROM consent" in query:
            return [{"scope": scope, "consent_id": i, "kind": "memory", "created_at": NOW, **value}
                    for i, (scope, value) in enumerate(self.consent.items())]
        if "DISTINCT ON (kind, subject)" in query:
            return [dict(f) for f in self.facts]
        if "array_agg(conversation_id" in query:
            return [dict(c) for c in self.chats]
        if query.startswith("DELETE FROM patient_memory"):
            hit = [r for r in self.rows if len(args) == 1 or (r["kind"], r["subject"]) == (args[1], args[2])]
            self.rows = [r for r in self.rows if r not in hit]
            return hit
        raise AssertionError(query)

    async def fetchrow(self, query, *args):
        self.queries.append(("fetchrow", query, args))
        self.events.append(query)
        if "INSERT INTO patient_memory" in query:
            return {"kind": args[1], "subject": args[2], "text": args[3], "event_date": args[4],
                    "source": "patient", "created_at": NOW}
        raise AssertionError(query)


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *args):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


@pytest.fixture
def client_factory(monkeypatch):
    from app.main import app

    clients = []

    def make(state: dict, conn: Conn) -> TestClient:
        async def get_state(u_id):
            assert u_id == 7
            return {scope: dict(value) for scope, value in state.items()}

        monkeypatch.setattr(consent_service, "get_state", get_state)
        monkeypatch.setattr(api_memory, "get_pool", lambda: _Pool(conn))
        monkeypatch.setattr(deletion_ledger, "append_host_file",
                            lambda kind, u_id, object_id: conn.events.append(("host", kind, u_id, object_id)))
        monkeypatch.setattr(app, "dependency_overrides", {get_current_user: lambda: {"u_id": 7, "name": "Test"}})
        client = TestClient(app)
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.close()


def _fact(kind, subject, text, *, source="chat", event_date=None, created_at=NOW):
    return {"memory_id": uuid.uuid4(), "kind": kind, "subject": subject, "text": text, "event_date": event_date,
            "source": source, "followed_up_at": None, "created_at": created_at}


def test_list_works_in_limited_mode_and_reports_enabled(client_factory):
    chat_a, chat_b = uuid.uuid4(), uuid.uuid4()
    state = {}
    conn = Conn(facts=[_fact("person", "amy", "Amy 是孫女"),
                       _fact("name", "preferred_name", "王奶奶", source="patient")],
                chats=[{"kind": "person", "subject": "amy", "chats": [chat_b, chat_a]}])
    client = client_factory(state, conn)

    response = client.get("/api/memory")
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["items"] == [
        {"kind": "person", "subject": "amy", "text": "Amy 是孫女", "event_date": None, "source": "chat",
         "learned_at": NOW.isoformat(), "conversation_ids": [str(chat_b), str(chat_a)]},
        {"kind": "name", "subject": "preferred_name", "text": "王奶奶", "event_date": None, "source": "patient",
         "learned_at": NOW.isoformat(), "conversation_ids": []},
    ]

    state.update(CORE_ONLY)                 # core alone is not memory consent
    assert client.get("/api/memory").json()["enabled"] is False
    state.update(MEMORY_ON)
    assert client.get("/api/memory").json()["enabled"] is True


def test_add_requires_memory_consent(client_factory):
    state = {}
    conn = Conn(consent=CORE_ONLY)
    client = client_factory(state, conn)
    payload = {"kind": "like", "text": "喜歡散步"}

    response = client.post("/api/memory", json=payload)
    assert response.status_code == 403 and response.json() == {"detail": "consent_required"}

    state.update(CORE_ONLY)
    response = client.post("/api/memory", json=payload)
    assert response.status_code == 403 and response.json() == {"detail": "memory_consent_required"}
    assert conn.queries == []

    # Consent current at the request, withdrawn before the lock: the locked re-read wins and nothing is written.
    state.update(MEMORY_ON)
    response = client.post("/api/memory", json=payload)
    assert response.status_code == 403 and response.json() == {"detail": "memory_consent_required"}
    assert conn.sql(LOCK) and conn.sql("FROM consent")
    assert conn.sql("INSERT INTO patient_memory") == []


def test_add_validates_and_upserts_a_patient_row(client_factory):
    conn = Conn()
    client = client_factory(dict(MEMORY_ON), conn)

    response = client.post("/api/memory", json={"kind": "like", "text": "喜歡吃降血壓藥"})
    assert response.status_code == 422 and response.json() == {"detail": "invalid_fact"}
    assert conn.queries == []

    response = client.post("/api/memory", json={"kind": "name", "text": "王奶奶"})
    assert response.status_code == 200
    assert response.json() == {"kind": "name", "subject": "preferred_name", "text": "王奶奶", "event_date": None,
                               "source": "patient", "learned_at": NOW.isoformat(), "conversation_ids": []}
    sqls = [sql for _, sql, _ in conn.queries]
    lock = next(i for i, sql in enumerate(sqls) if LOCK in sql)
    consent = next(i for i, sql in enumerate(sqls) if "FROM consent" in sql)
    upsert = next(i for i, sql in enumerate(sqls) if UPSERT in sql)
    assert lock < consent < upsert
    assert conn.queries[upsert][2] == (7, "name", "preferred_name", "王奶奶", None)
    assert conn.events[0] == "begin" and conn.events[-1] == "commit"

    when = memory.local_today() + timedelta(days=2)
    response = client.post("/api/memory", json={"kind": "event", "subject": "grandson_visit",
                                                 "text": "孫子來訪", "event_date": when.isoformat()})
    assert response.status_code == 200
    assert response.json()["event_date"] == when.isoformat()
    assert conn.sql(UPSERT)[-1][1] == (7, "event", "grandson_visit", "孫子來訪", when)

    assert client.post("/api/memory", json={"kind": "event", "text": "孫子來訪"}).status_code == 422
    assert client.post("/api/memory", json={"kind": "mood", "text": "開心"}).status_code == 422


def test_delete_fact_with_slash_and_cjk_subject(client_factory):
    first, second = uuid.uuid4(), uuid.uuid4()
    conn = Conn(rows=[{"memory_id": first, "kind": "person", "subject": "媽媽/爸爸"},
                      {"memory_id": second, "kind": "person", "subject": "媽媽/爸爸"},
                      {"memory_id": uuid.uuid4(), "kind": "like", "subject": "walking"}])
    client = client_factory({}, conn)        # limited mode: deleting needs no consent

    response = client.delete(f"/api/memory/fact?kind=person&subject={quote('媽媽/爸爸', safe='')}")
    assert response.status_code == 200 and response.json() == {"deleted": 2}
    (delete_sql, delete_args), = conn.sql("DELETE FROM patient_memory")
    assert delete_args == (7, "person", "媽媽/爸爸")
    assert [args for _, args in conn.sql("INSERT INTO patient_memory_deleted")] == [(7, "person", "媽媽/爸爸")]
    assert [args for _, args in conn.sql("INSERT INTO deletion_ledger")] == [
        ("memory", 7, str(first)), ("memory", 7, str(second))]
    assert conn.sql(LOCK) and conn.queries[0][1].endswith("FOR UPDATE")
    commit = conn.events.index("commit")
    assert conn.events[commit + 1:] == [("host", "memory", 7, str(first)), ("host", "memory", 7, str(second))]

    conn.events.clear()
    response = client.delete(f"/api/memory/fact?kind=person&subject={quote('媽媽/爸爸', safe='')}")
    assert response.status_code == 404 and response.json() == {"detail": "Fact not found"}
    assert not any(isinstance(event, tuple) for event in conn.events)


def test_delete_all_needs_confirm(client_factory):
    ids = [uuid.uuid4() for _ in range(3)]
    conn = Conn(rows=[{"memory_id": ids[0], "kind": "like", "subject": "walking"},
                      {"memory_id": ids[1], "kind": "like", "subject": "walking"},
                      {"memory_id": ids[2], "kind": "person", "subject": "amy"}])
    client = client_factory({}, conn)

    assert client.delete("/api/memory").status_code == 400
    assert client.delete("/api/memory?confirm=yes").status_code == 400
    assert conn.queries == []

    response = client.delete("/api/memory?confirm=all")
    assert response.status_code == 200 and response.json() == {"deleted": 3}
    assert [args for _, args in conn.sql("DELETE FROM patient_memory")] == [(7,)]
    assert sorted(args for _, args in conn.sql("INSERT INTO patient_memory_deleted")) == [
        (7, "like", "walking"), (7, "person", "amy")]
    assert len(conn.sql("INSERT INTO deletion_ledger")) == 3
    assert [event for event in conn.events if isinstance(event, tuple)] == [
        ("host", "memory", 7, str(memory_id)) for memory_id in ids]


def test_patch_corrects_only_an_existing_fact(client_factory):
    conn = Conn(facts=[_fact("like", "walking", "喜歡散步")])
    client = client_factory(dict(MEMORY_ON), conn)

    response = client.patch("/api/memory/fact?kind=like&subject=gardening", json={"text": "喜歡種花"})
    assert response.status_code == 404
    assert conn.sql("INSERT INTO patient_memory") == []

    response = client.patch("/api/memory/fact?kind=like&subject=walking", json={"text": "喜歡傍晚散步"})
    assert response.status_code == 200
    assert response.json()["source"] == "patient" and response.json()["subject"] == "walking"
    assert conn.sql(UPSERT)[-1][1] == (7, "like", "walking", "喜歡傍晚散步", None)


def test_every_route_filters_by_the_authenticated_user(client_factory):
    conn = Conn(facts=[_fact("like", "walking", "喜歡散步")],
                chats=[{"kind": "like", "subject": "walking", "chats": [uuid.uuid4()]}],
                rows=[{"memory_id": uuid.uuid4(), "kind": "like", "subject": "walking"},
                      {"memory_id": uuid.uuid4(), "kind": "person", "subject": "amy"}])
    client = client_factory(dict(MEMORY_ON), conn)

    assert client.get("/api/memory").status_code == 200
    assert client.post("/api/memory", json={"kind": "routine", "text": "每天早上散步"}).status_code == 200
    assert client.patch("/api/memory/fact?kind=like&subject=walking", json={"text": "喜歡傍晚散步"}).status_code == 200
    assert client.delete("/api/memory/fact?kind=like&subject=walking").status_code == 200
    assert client.delete("/api/memory?confirm=all").status_code == 200

    assert len(conn.queries) > 10
    for _, sql, args in conn.queries:
        # deletion_ledger.record(conn, kind, u_id, object_id) puts the user second.
        u_id = args[1] if "INSERT INTO deletion_ledger" in sql else args[0]
        assert u_id == 7, (sql, args)
    assert all(event[2] == 7 for event in conn.events if isinstance(event, tuple))
