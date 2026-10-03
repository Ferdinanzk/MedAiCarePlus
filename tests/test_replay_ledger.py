import asyncio
import json
import sys
import types

import pytest

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app import config
from app.ops import replay_ledger
from app.services import deletion_ledger


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _Connection:
    def __init__(self):
        self.users = {7, 8}
        self.ledger = set()
        self.marked = True

    def transaction(self):
        return _Transaction()

    async def execute(self, query, *args):
        if "INSERT INTO deletion_ledger" in query:
            assert "WHERE NOT EXISTS" in query
            self.ledger.add(args)
        elif 'DELETE FROM "user"' in query:
            self.users.discard(args[0])
        elif "DELETE FROM ops_state" in query:
            self.marked = False
        elif query.startswith(("DELETE FROM conversation_turn", "DELETE FROM patient_memory")):
            assert self.marked   # retention purges run before the restore marker is cleared
        else:
            raise AssertionError(query)


def test_replay_is_idempotent_and_preserves_other_accounts(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "FACE_GALLERY_DIR", tmp_path)
    (tmp_path / "pearl-0.jpg").write_bytes(b"deleted")
    (tmp_path / "other-0.jpg").write_bytes(b"retained")
    conn = _Connection()
    entries = [{"kind": "account", "u_id": 7, "object_id": "pearl"}]
    assert asyncio.run(deletion_ledger.replay(conn, entries)) == 1
    assert asyncio.run(deletion_ledger.replay(conn, entries)) == 1
    assert conn.users == {8}
    assert conn.ledger == {("account", 7, "pearl")}
    assert not (tmp_path / "pearl-0.jpg").exists()
    assert (tmp_path / "other-0.jpg").exists()


def test_replay_file_clears_marker_only_after_cleanup(monkeypatch, tmp_path):
    path = tmp_path / "ledger.jsonl"
    path.write_text(json.dumps({"kind": "account", "u_id": 7, "object_id": "pearl"}) + "\n", encoding="utf-8")
    conn = _Connection()

    def cleanup(label):
        assert conn.marked
        assert conn.users == {8}

    monkeypatch.setattr(deletion_ledger, "delete_gallery_files", cleanup)
    assert asyncio.run(replay_ledger.replay_file(conn, path)) == 1
    assert not conn.marked


def test_replay_cleanup_failure_keeps_restore_marker(monkeypatch, tmp_path):
    path = tmp_path / "ledger.jsonl"
    path.write_text(json.dumps({"kind": "account", "u_id": 7, "object_id": "pearl"}), encoding="utf-8")
    conn = _Connection()

    def fail(label):
        raise OSError("cannot delete restored photo")

    monkeypatch.setattr(deletion_ledger, "delete_gallery_files", fail)
    with pytest.raises(OSError, match="restored photo"):
        asyncio.run(replay_ledger.replay_file(conn, path))
    assert conn.marked


@pytest.mark.parametrize("contents", [None, '{"kind":', '{"kind":"unknown","u_id":7}'])
def test_missing_malformed_or_unknown_ledger_keeps_marker(tmp_path, contents):
    path = tmp_path / "ledger.jsonl"
    if contents is not None:
        path.write_text(contents, encoding="utf-8")
    conn = _Connection()
    with pytest.raises((FileNotFoundError, ValueError)):
        asyncio.run(replay_ledger.replay_file(conn, path))
    assert conn.marked


def test_empty_existing_ledger_allows_restore(tmp_path):
    path = tmp_path / "ledger.jsonl"
    path.write_text("", encoding="utf-8")
    conn = _Connection()
    assert asyncio.run(replay_ledger.replay_file(conn, path)) == 0
    assert not conn.marked
