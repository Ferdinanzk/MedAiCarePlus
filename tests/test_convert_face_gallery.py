"""Face vectors (8 Nov 2026): the one-time conversion of the old face photos into face_vector rows.

The real FacesDatabase reads the folder; the models are fakes whose descriptor depends on the photo's pixels, so no
OpenVINO model is loaded.
"""

import asyncio
import copy
import sys
import types

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


class _Transaction:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        self.saved = copy.deepcopy(self.conn.vectors)
        return self

    async def __aexit__(self, kind, error, tb):
        if kind is None:
            self.conn.commits += 1
        else:
            self.conn.vectors = self.saved
            self.conn.rollbacks += 1
        return False


class _Conn:
    """"user" rows {u_id: (face_label, active)} and face_vector rows (vector_id, u_id, face_label, vector, source)."""

    def __init__(self, users, vectors=(), tamper=False):
        self.users = users
        self.vectors = list(vectors)
        self.tamper = tamper
        self.commits = self.rollbacks = 0
        self.closed = False

    def transaction(self):
        return _Transaction(self)

    async def fetchval(self, sql, *args):
        assert "source = 'photo'" in sql
        return any(row[4] == "photo" for row in self.vectors)

    async def fetchrow(self, sql, label):
        assert 'FROM "user"' in sql and "user_active = TRUE" in sql
        for u_id, (face_label, active) in self.users.items():
            if face_label == label and active:
                return {"u_id": u_id, "vectors": sum(row[1] == u_id for row in self.vectors)}
        return None

    async def execute(self, sql, u_id, label, vector):
        assert sql.startswith("INSERT INTO face_vector") and "'photo'" in sql
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

    assert result == {"converted": 3, "labels": 2, "skipped_labels": 0, "already": False}
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
    assert first == {"converted": 1, "labels": 1, "skipped_labels": 4, "already": False}
    assert [(u_id, label, source) for u_id, label, _, source in _rows(conn)] == [
        (4, "kim", "enroll"), (1, "pearl", "photo")]

    before = list(conn.vectors)
    second = asyncio.run(convert_module.convert(conn, _service(), tmp_path))
    assert second == {"converted": 0, "labels": 0, "skipped_labels": 0, "already": True}
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
