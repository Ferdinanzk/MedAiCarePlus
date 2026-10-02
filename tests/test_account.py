import asyncio
import datetime
import io
import json
import sys
import threading
import types
import uuid
import zipfile

import pytest
from fastapi import HTTPException
from itsdangerous import TimestampSigner
from starlette.requests import Request

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app import config
from app.dependencies import get_current_user
from app.routers import api_account, api_auth
from app.services import deletion_ledger
from app.services.face_recognition_service import FaceRecognitionService


class _Transaction:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        self.conn.events.append("begin")
        self.snapshot = (self.conn.deleted, list(self.conn.ledger))
        return self

    async def __aexit__(self, exc_type, *args):
        if exc_type:
            self.conn.deleted, self.conn.ledger = self.snapshot
            self.conn.events.append("rollback")
        else:
            self.conn.events.append("commit")
        return False


class _Connection:
    def __init__(self, password_hash=None, fail_delete=False):
        self.row = {"u_id": 7, "name": "Pearl", "face_label": "pearl",
                    "password_hash": password_hash}
        self.fail_delete = fail_delete
        self.deleted = False
        self.ledger = []
        self.events = []
        self.queries = []

    def transaction(self, **kwargs):
        return _Transaction(self)

    async def fetchrow(self, query, *args):
        assert args == (7,)
        return None if self.deleted else self.row

    async def fetch(self, query, *args):
        self.queries.append((query, args))
        assert args == (7,)
        if 'FROM "user"' in query:
            return [self.row]
        return [{"u_id": 7, "created_at": datetime.datetime(2026, 9, 30, tzinfo=datetime.timezone.utc),
                 "event_id": uuid.UUID(int=1)}]

    async def execute(self, query, *args):
        assert self.events[-1] == "begin"
        if "INSERT INTO deletion_ledger" in query:
            self.ledger.append(args)
        elif 'DELETE FROM "user"' in query:
            assert args == (7,)
            if self.fail_delete:
                raise RuntimeError("database delete failed")
            self.deleted = True
        else:
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


def _request(token=None):
    headers = [(b"authorization", f"Bearer {token}".encode())] if token else []
    return Request({"type": "http", "headers": headers})


@pytest.fixture
def setup_account(monkeypatch, tmp_path):
    gallery = tmp_path / "gallery"
    gallery.mkdir()
    (gallery / "pearl-0.jpg").write_bytes(b"face")
    (gallery / "pearl-jane-0.jpg").write_bytes(b"other face")
    (gallery / "other-0.jpg").write_bytes(b"other face")
    monkeypatch.setattr(config, "FACE_GALLERY_DIR", gallery)
    monkeypatch.setattr(config, "DELETION_LEDGER_FILE", tmp_path / "ledger.jsonl")
    conn = _Connection()
    monkeypatch.setattr(api_account, "get_pool", lambda: _Pool(conn))
    monkeypatch.setattr(deletion_ledger, "refresh_gallery", lambda label: conn.events.append("refresh"))
    return conn, gallery


def test_export_contains_owned_rows_and_photos_without_password(setup_account):
    conn, gallery = setup_account
    conn.row["password_hash"] = "do-not-export"
    response = asyncio.run(api_account.export_account({"u_id": 7}))
    assert response.media_type == "application/zip"
    assert response.headers["cache-control"] == "no-store"
    with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
        assert set(archive.namelist()) == {"account.json", "face/pearl-0.jpg"}
        account = json.loads(archive.read("account.json"))
        assert set(account) == set(api_account.EXPORT_TABLES)
        assert "password_hash" not in account["user"][0]
        assert account["monitor_event"][0]["event_id"] == str(uuid.UUID(int=1))
        assert archive.read("face/pearl-0.jpg") == b"face"
    assert len(conn.queries) == len(api_account.EXPORT_TABLES)


def test_account_routes_use_identity_without_consent():
    for route in api_account.router.routes:
        assert [dep.call for dep in route.dependant.dependencies] == [get_current_user]


@pytest.mark.parametrize("password", [None, "wrong"])
def test_password_account_requires_correct_password(setup_account, password):
    conn, gallery = setup_account
    conn.row["password_hash"] = api_auth._hash_password("correct")
    token = api_auth._sign({"u_id": 7})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(api_account.delete_account(
            api_account.DeletePayload(password=password), _request(token), {"u_id": 7}))
    assert (exc.value.status_code, exc.value.detail) == (401, "reauth_required")
    assert not conn.deleted and not conn.ledger
    assert (gallery / "pearl-0.jpg").exists()


def test_delete_commits_before_files_and_host_ledger(setup_account, monkeypatch):
    conn, gallery = setup_account
    conn.row["password_hash"] = api_auth._hash_password("correct")
    original_cleanup = deletion_ledger.delete_gallery_files

    def cleanup(label):
        assert conn.events[-1] == "commit"
        assert conn.deleted and conn.ledger == [("account", 7, "pearl")]
        original_cleanup(label)

    monkeypatch.setattr(deletion_ledger, "delete_gallery_files", cleanup)
    result = asyncio.run(api_account.delete_account(
        api_account.DeletePayload(password="correct"), _request(), {"u_id": 7}))
    assert result == {"deleted": True}
    assert not (gallery / "pearl-0.jpg").exists()
    assert (gallery / "pearl-jane-0.jpg").exists()
    assert conn.events[-1] == "refresh"
    entry = json.loads(config.DELETION_LEDGER_FILE.read_text(encoding="utf-8"))
    assert (entry["kind"], entry["u_id"], entry["object_id"]) == ("account", 7, "pearl")


@pytest.mark.parametrize("age, token_uid, succeeds", [(599, 7, True), (601, 7, False), (0, 8, False)])
def test_face_account_requires_recent_matching_bearer(setup_account, monkeypatch, age, token_uid, succeeds):
    conn, gallery = setup_account
    now = TimestampSigner("test").get_timestamp()
    with monkeypatch.context() as patch:
        patch.setattr(TimestampSigner, "get_timestamp", lambda self: now - age)
        token = api_auth._sign({"u_id": token_uid})
    if succeeds:
        assert asyncio.run(api_account.delete_account(
            api_account.DeletePayload(), _request(token), {"u_id": 7})) == {"deleted": True}
    else:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(api_account.delete_account(
                api_account.DeletePayload(), _request(token), {"u_id": 7}))
        assert exc.value.detail == "reauth_required"
        assert not conn.deleted


def test_database_failure_preserves_files_and_host_ledger(setup_account):
    conn, gallery = setup_account
    conn.fail_delete = True
    with pytest.raises(RuntimeError, match="database delete failed"):
        asyncio.run(api_account.delete_account(
            api_account.DeletePayload(), _request(api_auth._sign({"u_id": 7})), {"u_id": 7}))
    assert not conn.deleted and not conn.ledger
    assert conn.events[-1] == "rollback"
    assert (gallery / "pearl-0.jpg").exists()
    assert not config.DELETION_LEDGER_FILE.exists()


def test_gallery_failure_still_records_host_deletion(setup_account, monkeypatch, caplog):
    conn, gallery = setup_account

    def fail(label):
        raise OSError("disk read-only")

    monkeypatch.setattr(deletion_ledger, "delete_gallery_files", fail)
    assert asyncio.run(api_account.delete_account(
        api_account.DeletePayload(), _request(api_auth._sign({"u_id": 7})), {"u_id": 7})) == {"deleted": True}
    assert config.DELETION_LEDGER_FILE.exists()
    assert conn.events[-1] == "refresh"
    assert "gallery cleanup failed" in caplog.text


def test_host_ledger_failure_never_fails_delete(setup_account, monkeypatch, caplog):
    conn, gallery = setup_account
    monkeypatch.setattr(config, "DELETION_LEDGER_FILE", gallery)  # Cannot append to a directory.
    assert asyncio.run(api_account.delete_account(
        api_account.DeletePayload(), _request(api_auth._sign({"u_id": 7})), {"u_id": 7})) == {"deleted": True}
    assert conn.deleted
    assert "Could not append host deletion ledger" in caplog.text


def test_refresh_removes_identity_even_when_files_remain(monkeypatch):
    identities = [types.SimpleNamespace(label="pearl"), types.SimpleNamespace(label="other")]
    database = types.SimpleNamespace(database=identities)
    calls = []
    service = types.SimpleNamespace(
        lock=threading.RLock(), face_id=types.SimpleNamespace(faces_database=database),
        reload_gallery=lambda: calls.append("reload"))
    monkeypatch.setattr(FaceRecognitionService, "get_instance", classmethod(lambda cls: service))
    monkeypatch.setattr(FaceRecognitionService, "_available", True)
    deletion_ledger.refresh_gallery("pearl")
    assert calls == ["reload"]
    assert [identity.label for identity in database.database] == ["other"]
