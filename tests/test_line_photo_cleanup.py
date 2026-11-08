import asyncio
import json
import sys
import types

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app.jobs import conversation_retention_job
from app.ops import replay_ledger
from app.routers import api_account
from app.services import deletion_ledger


class _Recorder:
    def __init__(self):
        self.queries = []

    async def execute(self, query, *args):
        self.queries.append(query)


def test_export_includes_line_drafts():
    assert "line_photo_draft" in api_account.EXPORT_TABLES
    assert "line_photo_draft" not in api_account.EXPORT_QUERIES   # it has a u_id column


def test_retention_deletes_expired_drafts():
    conn = _Recorder()
    asyncio.run(conversation_retention_job.run_retention(conn))
    assert any(q.startswith("DELETE FROM line_photo_draft") and "expires_at < NOW()" in q for q in conn.queries)


def test_restore_replay_deletes_expired_drafts(tmp_path):
    from tests.test_replay_ledger import _Connection
    conn = _Connection()
    seen = []
    original = conn.execute

    async def spy(query, *args):
        seen.append(query)
        return await original(query, *args)

    conn.execute = spy
    path = tmp_path / "ledger.jsonl"
    path.write_text(json.dumps({"kind": "account", "u_id": 7, "object_id": "pearl"}) + "\n", encoding="utf-8")
    asyncio.run(replay_ledger.replay_file(conn, path))
    assert any(q.startswith("DELETE FROM line_photo_draft") for q in seen)
