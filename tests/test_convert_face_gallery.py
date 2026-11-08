"""Face vectors (8 Nov 2026): the one-time conversion of the old face photos into face_vector rows.

The real FacesDatabase reads the folder; the models are fakes whose descriptor depends on the photo's pixels, so no
OpenVINO model is loaded.
"""

import asyncio
import copy
import os
import sys
import types
from datetime import datetime, timezone

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

import numpy as np
import pytest

from app.services import face_recognition_service as frs
from app.services.face_recognition_service import FaceRecognitionService


@pytest.fixture
def convert_module():
    pytest.importorskip("openvino")
    pytest.importorskip("scipy")
    pytest.importorskip("cv2")
    frs._load_imports()
    from app.ops import convert_face_gallery
    return convert_face_gallery


def _vec(seed: int) -> np.ndarray:
    return np.random.default_rng(seed).standard_normal(256).astype(np.float32)


class _FakeLandmarks:
    def infer(self, inputs):
        image, rois = inputs
        return [object() for _ in rois]


class _FakeIdentifier:
    """The descriptor of a photo is _vec(its first pixel's blue value): one fixed vector per photo."""

    def __init__(self):
        self.images = []

    def start_async(self, image, rois, landmarks):
        (roi,) = rois   # the whole stored crop, no detector
        assert list(roi.size) == [image.shape[1], image.shape[0]]
        self.images.append(image)

    def get_descriptors(self):
        return [_vec(int(self.images[-1][0, 0, 0]))]


def _service():
    return types.SimpleNamespace(face_id=_FakeIdentifier(), lm_det=_FakeLandmarks())


def _photo(folder, name, seed):
    import cv2
    image = np.full((8, 8, 3), seed, np.uint8)
    ok, data = cv2.imencode(".png", image)
    assert ok
    (folder / name).write_bytes(data.tobytes())


OLD_ACCOUNT = datetime(2020, 1, 1, tzinfo=timezone.utc)   # created before any photo these tests write


class _Transaction:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        self.saved = copy.deepcopy(self.conn.vectors)
        self.conn.in_transaction = True
        return self

    async def __aexit__(self, kind, error, tb):
        self.conn.in_transaction = self.conn.locked = False
        if kind is None:
            self.conn.commits += 1
        else:
            self.conn.vectors = self.saved
            self.conn.rollbacks += 1
        return False


class _Conn:
    """"user" rows {u_id: (face_label, active)}, face_vector rows (vector_id, u_id, face_label, vector, source),
    the 'account' deletion_ledger rows (object_id, deleted_at) and each account's created_at (OLD_ACCOUNT when not
    given)."""

    def __init__(self, users, vectors=(), tamper=False, deleted=(), created=None):
        self.users = users
        self.vectors = list(vectors)
        self.tamper = tamper
        self.deleted = list(deleted)
        self.created = created or {}
        self.commits = self.rollbacks = 0
        self.closed = False
        self.in_transaction = False
        self.locked = False
        self.already_checks = []   # (in the transaction, after the lock) for each "already" check

    def transaction(self):
        return _Transaction(self)

    async def fetchval(self, sql, *args):
        if "deletion_ledger" in sql:
            assert "max(deleted_at)" in sql and "kind = 'account'" in sql and "lower(object_id) = $1" in sql
            times = [deleted_at for object_id, deleted_at in self.deleted if object_id.lower() == args[0]]
            return max(times) if times else None
        assert "source = 'photo'" in sql
        self.already_checks.append((self.in_transaction, self.locked))
        return any(row[4] == "photo" for row in self.vectors)

    async def fetchrow(self, sql, label):
        assert 'FROM "user"' in sql and "user_active = TRUE" in sql and "created_at" in sql
        for u_id, (face_label, active) in self.users.items():
            if face_label == label and active:
                return {"u_id": u_id, "created_at": self.created.get(u_id, OLD_ACCOUNT),
                        "vectors": sum(row[1] == u_id for row in self.vectors)}
        return None

    async def execute(self, sql, *args):
        if sql.startswith("LOCK TABLE"):
            assert sql == "LOCK TABLE face_vector IN SHARE ROW EXCLUSIVE MODE" and self.in_transaction
            self.locked = True
            return
        assert sql.startswith("INSERT INTO face_vector") and "'photo'" in sql
        u_id, label, vector = args
        if self.tamper:
            vector = _vec(999).astype("<f4").tobytes()
        self.vectors.append((len(self.vectors) + 1, u_id, label, vector, "photo"))

    async def fetch(self, sql, *args):
        assert 'JOIN "user" u' in sql
        return [{"face_label": self.users[u_id][0], "vector": vector}
                for _, u_id, _, vector, _ in sorted(self.vectors) if self.users[u_id][1]]

    async def close(self):
        self.closed = True


def _rows(conn):
    return [(u_id, label, np.frombuffer(vector, "<f4"), source) for _, u_id, label, vector, source in conn.vectors]


def test_conversion_writes_one_row_per_photo(convert_module, tmp_path):
    _photo(tmp_path, "Pearl-0.png", 11)
    _photo(tmp_path, "pearl-1.png", 12)
    _photo(tmp_path, "jane-0.png", 13)
    conn = _Conn({1: ("pearl", True), 2: ("jane", True)})

    result = asyncio.run(convert_module.convert(conn, _service(), tmp_path))

    assert result == {"converted": 3, "labels": 2, "skipped_labels": 0, "matched_other": 0, "already": False}
    rows = _rows(conn)
    assert sorted((u_id, label, source) for u_id, label, _, source in rows) == [
        (1, "pearl", "photo"), (1, "pearl", "photo"), (2, "jane", "photo")]
    by_user = {}
    for u_id, _, vector, _ in rows:
        by_user.setdefault(u_id, []).append(vector)
    assert sorted(v[0] for v in by_user[1]) == sorted([_vec(11)[0], _vec(12)[0]])
    assert np.array_equal(by_user[2][0], _vec(13))
    assert all(len(vector) == 1024 for *_, vector, _ in conn.vectors)
    assert conn.commits == 1 and conn.rollbacks == 0


def test_conversion_is_once_and_skips_unknown_labels(convert_module, tmp_path):
    _photo(tmp_path, "pearl-0.png", 11)
    _photo(tmp_path, "ghost-0.png", 21)       # a deleted user's photo an old backup brought back
    _photo(tmp_path, "old-0.png", 22)         # an inactive account
    _photo(tmp_path, ".pearl-ab12-0.png", 23)  # a dot file: FacesDatabase reads it as label ".pearl-ab12"
    _photo(tmp_path, "kim-0.png", 24)         # registered again since: its own vectors stay, never mixed
    kim = (1, 4, "kim", _vec(5).astype("<f4").tobytes(), "enroll")
    conn = _Conn({1: ("pearl", True), 2: ("old", False), 4: ("kim", True)}, vectors=[kim])

    first = asyncio.run(convert_module.convert(conn, _service(), tmp_path))
    assert first == {"converted": 1, "labels": 1, "skipped_labels": 4, "matched_other": 0, "already": False}
    assert [(u_id, label, source) for u_id, label, _, source in _rows(conn)] == [
        (4, "kim", "enroll"), (1, "pearl", "photo")]

    before = list(conn.vectors)
    second = asyncio.run(convert_module.convert(conn, _service(), tmp_path))
    assert second == {"converted": 0, "labels": 0, "skipped_labels": 0, "matched_other": 0, "already": True}
    assert conn.vectors == before


def test_a_mismatch_rolls_back(convert_module, tmp_path):
    _photo(tmp_path, "pearl-0.png", 11)
    _photo(tmp_path, "jane-0.png", 13)
    conn = _Conn({1: ("pearl", True), 2: ("jane", True)}, tamper=True)

    with pytest.raises(convert_module.ConversionMismatch) as caught:
        asyncio.run(convert_module.convert(conn, _service(), tmp_path))

    assert conn.vectors == [] and conn.rollbacks == 1 and conn.commits == 0
    assert "pearl" not in str(caught.value) and "jane" not in str(caught.value)
    assert "2" in str(caught.value)


def test_the_already_check_runs_under_the_lock(convert_module, tmp_path):
    _photo(tmp_path, "pearl-0.png", 11)
    conn = _Conn({1: ("pearl", True)})
    asyncio.run(convert_module.convert(conn, _service(), tmp_path))
    asyncio.run(convert_module.convert(conn, _service(), tmp_path))
    # Each run asks once, inside its transaction and after LOCK TABLE, so two runs at once never both write.
    assert conn.already_checks == [(True, True), (True, True)]
    assert len(conn.vectors) == 1 and conn.commits == 2


def _date(folder, name, *when):
    stamp = datetime(*when, tzinfo=timezone.utc).timestamp()
    os.utime(folder / name, (stamp, stamp))


def test_photos_taken_before_a_deletion_of_the_label_are_skipped(convert_module, tmp_path):
    # A deleted account's photos left behind (a failed file delete, or an old backup restored) must never become
    # the face of a newer account that reuses the label. Only the ledger rule fires here: the account's created_at
    # (2020) is older than every photo, so the account's own time rule would let them through.
    _photo(tmp_path, "pearl-0.png", 11)
    _photo(tmp_path, "pearl-1.png", 12)
    _photo(tmp_path, "jane-0.png", 13)
    _date(tmp_path, "pearl-0.png", 2024, 3, 1)   # the label's oldest photo predates the latest deletion
    deleted = [("Pearl", datetime(2023, 1, 1, tzinfo=timezone.utc)),   # an older deletion of the same label
               ("Pearl", datetime(2024, 6, 1, tzinfo=timezone.utc)),
               ("janet", datetime(2030, 1, 1, tzinfo=timezone.utc))]   # another label: no effect on jane
    conn = _Conn({5: ("pearl", True), 2: ("jane", True)}, deleted=deleted)

    result = asyncio.run(convert_module.convert(conn, _service(), tmp_path))

    assert result == {"converted": 1, "labels": 1, "skipped_labels": 1, "matched_other": 0, "already": False}
    assert [(u_id, label) for u_id, label, _, _ in _rows(conn)] == [(2, "jane")]


def test_a_deletion_before_the_photos_does_not_skip_the_new_owner(convert_module, tmp_path):
    # As on the live folder: an earlier account with the same label was deleted 18 days before the current one was
    # created, and the current account's photos were taken 44 s after it was created. They are its own.
    _photo(tmp_path, "pearl-0.png", 11)
    _photo(tmp_path, "pearl-1.png", 12)
    for name in ("pearl-0.png", "pearl-1.png"):
        _date(tmp_path, name, 2025, 2, 1)
    conn = _Conn({5: ("pearl", True)}, created={5: datetime(2025, 1, 31, 23, 59, 16, tzinfo=timezone.utc)},
                 deleted=[("Pearl", datetime(2025, 1, 13, tzinfo=timezone.utc))])

    result = asyncio.run(convert_module.convert(conn, _service(), tmp_path))

    assert result == {"converted": 2, "labels": 1, "skipped_labels": 0, "matched_other": 0, "already": False}
    assert [(u_id, label) for u_id, label, _, _ in _rows(conn)] == [(5, "pearl"), (5, "pearl")]


def test_photos_older_than_the_account_are_skipped(convert_module, tmp_path):
    _photo(tmp_path, "pearl-0.png", 11)
    _photo(tmp_path, "pearl-1.png", 12)
    _photo(tmp_path, "jane-0.png", 13)
    _photo(tmp_path, "kim-0.png", 14)
    old = datetime(2024, 5, 1, tzinfo=timezone.utc).timestamp()
    os.utime(tmp_path / "pearl-1.png", (old, old))   # one of pearl's photos predates pearl's account
    before = {path.name: path.stat().st_mtime_ns for path in tmp_path.iterdir()}
    conn = _Conn({1: ("pearl", True), 2: ("jane", True), 3: ("kim", True)},
                 created={1: datetime(2025, 1, 1, tzinfo=timezone.utc), 3: None})   # kim: no created_at

    result = asyncio.run(convert_module.convert(conn, _service(), tmp_path))

    assert result == {"converted": 1, "labels": 1, "skipped_labels": 2, "matched_other": 0, "already": False}
    assert [(u_id, label) for u_id, label, _, _ in _rows(conn)] == [(2, "jane")]
    assert {path.name: path.stat().st_mtime_ns for path in tmp_path.iterdir()} == before   # only read


def test_a_face_already_registered_to_another_account_is_skipped(convert_module, tmp_path):
    # Between the rebuild and the conversion the gallery holds only 'enroll' rows, so jane could register pearl's
    # face. Converting pearl's photos would then put one face under two labels (the 3 Oct incident). Any one of a
    # label's photos within login's threshold of another account's vectors skips the whole label.
    _photo(tmp_path, "pearl-0.png", 11)   # the face jane registered
    _photo(tmp_path, "pearl-1.png", 12)   # far from every stored vector
    _photo(tmp_path, "kim-0.png", 14)
    jane = (1, 2, "jane", _vec(11).astype("<f4").tobytes(), "enroll")
    gone = (2, 3, "gone", _vec(14).astype("<f4").tobytes(), "enroll")   # an inactive account: not in the gallery
    conn = _Conn({1: ("pearl", True), 2: ("jane", True), 3: ("gone", False), 4: ("kim", True)},
                 vectors=[jane, gone])

    result = asyncio.run(convert_module.convert(conn, _service(), tmp_path))

    assert result == {"converted": 1, "labels": 1, "skipped_labels": 0, "matched_other": 1, "already": False}
    assert [(u_id, label, source) for u_id, label, _, source in _rows(conn)] == [
        (2, "jane", "enroll"), (3, "gone", "enroll"), (4, "kim", "photo")]
    assert conn.commits == 1 and conn.rollbacks == 0


def test_two_folder_labels_with_the_same_face_are_both_checked(convert_module, tmp_path):
    # Two folder labels with the same face: the first one converted (in the folder's reading order, os.listdir,
    # as FacesDatabase reads it) is kept, and the second is checked against it and skipped.
    _photo(tmp_path, "anna-0.png", 11)
    _photo(tmp_path, "bob-0.png", 11)
    _photo(tmp_path, "kim-0.png", 14)
    first = next(name.split("-")[0] for name in os.listdir(tmp_path) if name.split("-")[0] in ("anna", "bob"))
    conn = _Conn({1: ("anna", True), 2: ("bob", True), 3: ("kim", True)})

    result = asyncio.run(convert_module.convert(conn, _service(), tmp_path))

    assert result == {"converted": 2, "labels": 2, "skipped_labels": 0, "matched_other": 1, "already": False}
    assert sorted(label for _, label, _, _ in _rows(conn)) == sorted([first, "kim"])


def test_the_face_check_uses_logins_threshold(convert_module, tmp_path, monkeypatch):
    # distance <= FACE_MATCH_THRESHOLD is a match, as login's FaceIdentifier.postprocess: a vector exactly at the
    # threshold skips the label, one just above it does not.
    from app import config
    _photo(tmp_path, "pearl-0.png", 11)
    rival = (1, 2, "jane", _vec(99).astype("<f4").tobytes(), "enroll")
    distance = frs.FacesDatabase.Identity.cosine_dist(_vec(11), _vec(99))
    for threshold, skipped in ((distance, 1), (distance - 1e-6, 0)):
        monkeypatch.setattr(config, "FACE_MATCH_THRESHOLD", threshold)
        conn = _Conn({1: ("pearl", True), 2: ("jane", True)}, vectors=[rival])
        result = asyncio.run(convert_module.convert(conn, _service(), tmp_path))
        assert result["matched_other"] == skipped and result["labels"] == 1 - skipped


def _snapshot(folder):
    return {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in sorted(folder.iterdir())}


def test_folder_is_untouched(convert_module, tmp_path):
    _photo(tmp_path, "pearl-0.png", 11)
    _photo(tmp_path, "ghost-0.png", 21)
    (tmp_path / "notes.txt").write_text("keep")
    before = _snapshot(tmp_path)

    with pytest.raises(convert_module.ConversionMismatch):
        asyncio.run(convert_module.convert(_Conn({1: ("pearl", True)}, tamper=True), _service(), tmp_path))
    asyncio.run(convert_module.convert(_Conn({1: ("pearl", True)}), _service(), tmp_path))

    assert _snapshot(tmp_path) == before


def _cli(convert_module, monkeypatch, conn, available=True):
    connected = []

    async def connect(url):
        connected.append(url)
        return conn

    monkeypatch.setattr(convert_module.asyncpg, "connect", connect, raising=False)
    monkeypatch.setattr(FaceRecognitionService, "_available", available)
    monkeypatch.setattr(FaceRecognitionService, "get_instance", classmethod(lambda cls: _service()))
    return connected


def test_cli_prints_counts_only(convert_module, monkeypatch, tmp_path, capsys):
    _photo(tmp_path, "pearl-0.png", 11)
    _photo(tmp_path, "pearl-1.png", 12)
    _photo(tmp_path, "ghost-0.png", 21)
    conn = _Conn({1: ("pearl", True)})
    connected = _cli(convert_module, monkeypatch, conn)
    monkeypatch.setattr(sys, "argv", ["convert_face_gallery", "--folder", str(tmp_path)])

    convert_module.main()

    out = capsys.readouterr().out
    assert connected and conn.closed
    assert out.strip().splitlines() == [
        "Converted 2 photo vector(s) for 1 account(s); 1 photo label(s) skipped; the photo folder was not changed."]
    assert "pearl" not in out and "ghost" not in out

    # A label whose face is already another account's: one more count-only line, never the labels.
    capsys.readouterr()
    jane = (1, 2, "jane", _vec(11).astype("<f4").tobytes(), "enroll")
    matched = _Conn({1: ("pearl", True), 2: ("jane", True)}, vectors=[jane])
    _cli(convert_module, monkeypatch, matched)
    convert_module.main()
    out = capsys.readouterr().out
    assert out.strip().splitlines() == [
        "Converted 0 photo vector(s) for 0 account(s); 1 photo label(s) skipped; the photo folder was not changed.",
        "1 label(s) skipped: the face matches another account."]
    assert "pearl" not in out and "jane" not in out
    _cli(convert_module, monkeypatch, conn)

    capsys.readouterr()
    convert_module.main()   # the second run writes nothing
    out = capsys.readouterr().out
    assert out.strip().splitlines() == ["Already converted: nothing written; the photo folder was not changed."]
    assert len(conn.vectors) == 2

    # A mismatch and missing models end with a non-zero exit and a sentence without labels or numbers of vectors.
    tampered = _Conn({1: ("pearl", True)}, tamper=True)
    _cli(convert_module, monkeypatch, tampered)
    with pytest.raises(SystemExit) as stopped:
        convert_module.main()
    assert stopped.value.code == 1 and tampered.closed and tampered.vectors == []
    _cli(convert_module, monkeypatch, _Conn({}), available=False)
    with pytest.raises(SystemExit) as stopped:
        convert_module.main()
    assert stopped.value.code == 1
    out = capsys.readouterr()
    assert "pearl" not in out.out + out.err

    # A database error (asyncpg.PostgresError, or anything else): only its class name, never its text.
    class PostgresError(Exception):
        pass

    class Failing(_Conn):
        async def fetchrow(self, sql, label):
            raise PostgresError(f'duplicate key value violates "user" face_label ({label})')

    failing = Failing({1: ("pearl", True)})
    _cli(convert_module, monkeypatch, failing)
    with pytest.raises(SystemExit) as stopped:
        convert_module.main()
    assert stopped.value.code == 1 and failing.closed and failing.vectors == []
    out = capsys.readouterr()
    assert out.out.strip().splitlines() == [
        "Conversion failed (PostgresError); the photo folder was not changed."]
    assert "pearl" not in out.out + out.err and "duplicate" not in out.out + out.err
