import asyncio
import datetime
import io
import json
import logging
import pathlib
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
        self.queries_vector = []
        self.vector_summary = {"count": 3, "first": datetime.datetime(2026, 11, 8, 9, 0, tzinfo=datetime.timezone.utc),
                               "last": datetime.datetime(2026, 11, 8, 9, 5, tzinfo=datetime.timezone.utc)}

    def transaction(self, **kwargs):
        return _Transaction(self)

    async def fetchrow(self, query, *args):
        assert args == (7,)
        if "FROM face_vector" in query:
            self.queries_vector.append(query)
            return self.vector_summary
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
        assert set(account) == set(api_account.EXPORT_TABLES) | {"face_vectors"}
        assert "password_hash" not in account["user"][0]
        assert account["monitor_event"][0]["event_id"] == str(uuid.UUID(int=1))
        assert archive.read("face/pearl-0.jpg") == b"face"
    assert len(conn.queries) == len(api_account.EXPORT_TABLES)


def test_export_summarises_vectors_without_numbers(setup_account):
    conn, gallery = setup_account
    response = asyncio.run(api_account.export_account({"u_id": 7}))
    assert "face_vector" not in api_account.EXPORT_TABLES
    with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
        text = archive.read("account.json").decode("utf-8")
        account = json.loads(text)
    assert account["face_vectors"] == {"count": 3, "first": "2026-11-08T09:00:00+00:00",
                                       "last": "2026-11-08T09:05:00+00:00"}
    assert "vector" not in text.replace("face_vectors", "")
    assert len(conn.queries_vector) == 1
    assert "count(*)" in conn.queries_vector[0] and "vector," not in conn.queries_vector[0]


def test_export_vector_summary_for_an_account_without_vectors(setup_account):
    conn, gallery = setup_account
    conn.vector_summary = {"count": 0, "first": None, "last": None}
    response = asyncio.run(api_account.export_account({"u_id": 7}))
    with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
        assert json.loads(archive.read("account.json"))["face_vectors"] == {"count": 0, "first": None, "last": None}


def test_export_still_includes_remaining_photo_files(setup_account):
    conn, gallery = setup_account
    response = asyncio.run(api_account.export_account({"u_id": 7}))
    with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
        assert "face/pearl-0.jpg" in archive.namelist()
        assert "face/pearl-jane-0.jpg" not in archive.namelist()
        assert archive.read("face/pearl-0.jpg") == b"face"


def test_account_delete_cascades_vectors(setup_account):
    """No extra SQL: the one DELETE FROM "user" carries the face_vector rows (ON DELETE CASCADE)."""
    conn, gallery = setup_account
    executed = []
    original = conn.execute

    async def spy(query, *args):
        executed.append(query)
        return await original(query, *args)

    conn.execute = spy
    asyncio.run(api_account.delete_account(api_account.DeletePayload(), _request(api_auth._sign({"u_id": 7})), {"u_id": 7}))
    assert [q for q in executed if "face_vector" in q] == []
    assert sum('DELETE FROM "user"' in q for q in executed) == 1
    init_sql = (pathlib.Path(__file__).resolve().parents[1] / "sql" / "init.sql").read_text(encoding="utf-8")
    block = init_sql.split("CREATE TABLE IF NOT EXISTS face_vector", 1)[1].split(");", 1)[0]
    assert "ON DELETE CASCADE" in block


def test_deletion_reloads_the_gallery_from_the_database_after_commit(setup_account, monkeypatch):
    """Ruling R3: a registration reload that read rows before the deletion committed must not bring the identity back."""
    from app.services import face_vectors
    conn, gallery = setup_account
    calls = []

    async def reload(pool=None):
        calls.append(conn.events[-1])
        return 0

    monkeypatch.setattr(face_vectors, "reload", reload)
    assert asyncio.run(api_account.delete_account(
        api_account.DeletePayload(), _request(api_auth._sign({"u_id": 7})), {"u_id": 7})) == {"deleted": True}
    assert calls == ["refresh"]  # after the commit and after the in-memory drop


def test_deletion_reload_holds_the_enrollment_lock(setup_account, monkeypatch):
    """Minor 1 of the final review: a registration reload that read rows before the deletion committed must finish
    before the deletion's reload starts, so the deleted label cannot come back after it."""
    import asyncio as _asyncio
    from app.routers import api_face
    from app.services import face_vectors
    lock = _asyncio.Lock()
    monkeypatch.setattr(api_face, "_enroll_lock", lock)
    held = []

    async def reload(pool=None):
        held.append(lock.locked())
        return 0

    monkeypatch.setattr(face_vectors, "reload", reload)
    assert asyncio.run(api_account.delete_account(
        api_account.DeletePayload(), _request(api_auth._sign({"u_id": 7})), {"u_id": 7})) == {"deleted": True}
    assert held == [True] and not lock.locked()


def test_deletion_survives_a_failing_reload_and_logs_only_the_type(setup_account, monkeypatch, caplog):
    from app.services import face_vectors
    conn, gallery = setup_account

    async def reload(pool=None):
        raise RuntimeError("secret-vector-bytes")

    monkeypatch.setattr(face_vectors, "reload", reload)
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(api_account.delete_account(
            api_account.DeletePayload(), _request(api_auth._sign({"u_id": 7})), {"u_id": 7})) == {"deleted": True}
    assert "RuntimeError" in caplog.text
    assert "secret-vector-bytes" not in caplog.text
    assert conn.deleted


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


def test_refresh_drops_the_identity_without_reading_the_folder(monkeypatch):
    identities = [types.SimpleNamespace(label="pearl"), types.SimpleNamespace(label="other")]
    database = types.SimpleNamespace(database=identities)
    service = types.SimpleNamespace(
        lock=threading.RLock(), face_id=types.SimpleNamespace(faces_database=database))
    monkeypatch.setattr(FaceRecognitionService, "get_instance", classmethod(lambda cls: service))
    monkeypatch.setattr(FaceRecognitionService, "_available", True)
    deletion_ledger.refresh_gallery("pearl")
    assert [identity.label for identity in database.database] == ["other"]


def test_export_strips_the_device_token_hash():
    from app.routers import api_account
    rows = {"reachy_device": [{"device_id": "d", "u_id": 7, "token_hash": "abc"}], "user": [{"password_hash": "x"}],
            "dose_video_link": [{"link_id": "l", "token_sha256": "def"}]}
    api_account._strip_secrets(rows)
    assert "token_hash" not in rows["reachy_device"][0] and "password_hash" not in rows["user"][0]
    assert rows["dose_video_link"] == [{"link_id": "l"}]


def test_export_strips_the_robots_control_key():
    """Quick fix 9: the heartbeat keeps the robot app's control address and key in status_detail; the key switches the
    robot's microphone, so it never leaves the server. asyncpg hands JSONB back as text."""
    from app.routers import api_account
    key = "K" * 43
    detail = {"vision_fps": None, "microphone_volume": 0, "control": {"url": "http://192.168.137.104:8042", "key": key}}
    rows = {"reachy_device": [{"device_id": "a", "status_detail": json.dumps(detail)},
                              {"device_id": "b", "status_detail": dict(detail)},
                              {"device_id": "c", "status_detail": None},
                              {"device_id": "d", "status_detail": "[]"}]}
    api_account._strip_secrets(rows)
    assert key not in json.dumps(rows)
    assert json.loads(rows["reachy_device"][0]["status_detail"]) == {"vision_fps": None, "microphone_volume": 0}
    assert rows["reachy_device"][1]["status_detail"] == {"vision_fps": None, "microphone_volume": 0}
    assert [row["status_detail"] for row in rows["reachy_device"][2:]] == [None, "[]"]


def test_export_strips_the_reply_token_from_outbox_payloads():
    """Final review F5: rows written before the dispatcher dropped the spent token still hold one."""
    from app.routers import api_account
    rows = {"notification_outbox": [
        {"outbox_id": 1, "payload": json.dumps({"messages": [], "reply_token": "tok", "reply_until": "t"})},
        {"outbox_id": 2, "payload": {"messages": [], "reply_token": "tok", "reply_until": "t"}},
        {"outbox_id": 3, "payload": json.dumps({"messages": []})},
        {"outbox_id": 4, "payload": None}]}
    api_account._strip_secrets(rows)
    assert "tok" not in json.dumps(rows)
    assert json.loads(rows["notification_outbox"][0]["payload"]) == {"messages": [], "reply_until": "t"}
    assert rows["notification_outbox"][1]["payload"] == {"messages": [], "reply_until": "t"}
    assert json.loads(rows["notification_outbox"][2]["payload"]) == {"messages": []}


def test_export_reads_dose_video_links_through_their_video():
    query = api_account.EXPORT_QUERIES["dose_video_link"]
    assert "JOIN dose_video v" in query and query.endswith("WHERE v.u_id=$1")
    assert set(api_account.EXPORT_QUERIES) <= set(api_account.EXPORT_TABLES)
