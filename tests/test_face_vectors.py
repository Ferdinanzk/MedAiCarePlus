"""Face vectors (8 Nov 2026): face login's gallery comes from the face_vector table, not the photo folder."""

import asyncio
import logging
import sys
import threading
import types
from pathlib import Path

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

import numpy as np
import pytest

from app import config
from app.services import face_recognition_service as frs
from app.services.face_login import FaceBox
from app.services.face_recognition_service import FaceRecognitionService

REPO = Path(__file__).resolve().parent.parent

STATEMENT = """-- Face vectors (8 Nov 2026): face login keeps the 256 numbers of each registration photo, never the photo.
CREATE TABLE IF NOT EXISTS face_vector (
    vector_id BIGSERIAL PRIMARY KEY,
    u_id INTEGER NOT NULL REFERENCES "user"(u_id) ON DELETE CASCADE,
    face_label VARCHAR(100) NOT NULL,
    vector BYTEA NOT NULL CHECK (octet_length(vector) = 1024),
    source VARCHAR(10) NOT NULL CHECK (source IN ('enroll', 'photo')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_face_vector_user ON face_vector(u_id, created_at DESC);"""

FRAME = np.zeros((240, 320, 3), dtype=np.uint8)


@pytest.fixture
def face_vectors():
    pytest.importorskip("openvino")
    pytest.importorskip("scipy")
    frs._load_imports()
    from app.services import face_vectors as module
    return module


def _vec(seed: int) -> np.ndarray:
    return np.random.default_rng(seed).standard_normal(256).astype(np.float32)


def test_vector_table_is_pinned():
    sql = (REPO / "sql" / "init.sql").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert sql.count(STATEMENT) == 1
    assert sql.index(STATEMENT) > sql.index('CREATE TABLE IF NOT EXISTS "user"')
    assert sql.count("CREATE TABLE IF NOT EXISTS face_vector") == 1


def test_round_trip_is_exact(face_vectors):
    desc = _vec(1)
    raw = face_vectors.to_bytes(desc)
    assert isinstance(raw, bytes) and len(raw) == 1024
    back = face_vectors.from_bytes(raw)
    assert back.dtype == np.float32 and back.shape == (256,)
    assert np.array_equal(back, desc)
    with pytest.raises(ValueError):
        face_vectors.from_bytes(raw[:1000])


def test_build_gallery_groups_labels_like_faces_database(face_vectors):
    a, b, c = _vec(1), _vec(2), _vec(3)
    rows = [("Pearl", face_vectors.to_bytes(a)), ("jane", face_vectors.to_bytes(b)),
            ("pearl", face_vectors.to_bytes(c))]
    gallery = face_vectors.build_gallery(rows)
    assert isinstance(gallery, frs.FacesDatabase)
    assert len(gallery) == 2
    assert gallery[0].label == "pearl" and gallery[1].label == "jane"
    assert isinstance(gallery[0], frs.FacesDatabase.Identity)
    assert [np.array_equal(x, y) for x, y in zip(gallery[0].descriptors, (a, c))] == [True, True]
    # match_enrollment_face takes a shallow copy and replaces .database on it.
    import copy
    view = copy.copy(gallery)
    view.database = [gallery[1]]
    assert len(view) == 1 and len(gallery) == 2


def test_gallery_matches_like_the_folder_gallery(face_vectors):
    rows = [("ferdi", _vec(10)), ("ferdi", _vec(11)), ("aisha", _vec(12)), ("ab", _vec(13))]
    folder = object.__new__(frs.FacesDatabase)   # as FacesDatabase builds it from <label>-<i>.jpg
    folder.database = []
    folder.fg_path = str(REPO / "nowhere")
    for i, (label, vec) in enumerate(rows):
        folder.add_item(vec, f"{label}-{i}")
    gallery = face_vectors.build_gallery([(label, face_vectors.to_bytes(vec)) for label, vec in rows])
    assert [i.label for i in gallery.database] == [i.label for i in folder.database]
    queries = [_vec(10) + 0.1 * _vec(99), _vec(12), _vec(50), _vec(13) * 2]
    assert gallery.match_faces(queries, "MIN_DIST") == folder.match_faces(queries, "MIN_DIST")


def test_empty_table_gives_an_empty_gallery(face_vectors):
    gallery = face_vectors.build_gallery([])
    assert len(gallery) == 0
    service = object.__new__(FaceRecognitionService)
    service.lock = threading.RLock()
    service.face_id = types.SimpleNamespace(faces_database=gallery)
    assert service.match_box(FRAME, FaceBox(100, 50, 100, 100, 0.9)) == (None, None)


def test_a_bad_row_is_skipped(face_vectors, caplog):
    good = face_vectors.to_bytes(_vec(1))
    with caplog.at_level(logging.WARNING):
        gallery = face_vectors.build_gallery([("secretlabel", b"\0" * 1000), ("pearl", good)])
    assert len(gallery) == 1 and gallery[0].label == "pearl"
    assert "1 row" in caplog.text
    assert "secretlabel" not in caplog.text and "pearl" not in caplog.text


class _Conn:
    def __init__(self, rows, labels=()):
        self.rows = rows
        self.labels = labels
        self.sql = []

    async def fetch(self, sql, *args):
        self.sql.append(" ".join(sql.split()))
        if "DISTINCT" in sql:
            return [{"label": label} for label in self.labels]
        return [{"face_label": label, "vector": vector} for label, vector in self.rows]


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False
        return Ctx()


def test_reload_fetches_active_rows_and_loads_them(face_vectors, monkeypatch):
    conn = _Conn([("pearl", face_vectors.to_bytes(_vec(1))), ("jane", face_vectors.to_bytes(_vec(2))),
                  ("pearl", face_vectors.to_bytes(_vec(3)))])
    loaded = []
    service = types.SimpleNamespace(load_gallery=loaded.append)
    monkeypatch.setattr(FaceRecognitionService, "get_instance", classmethod(lambda cls: service))
    assert asyncio.run(face_vectors.reload(_Pool(conn))) == 2
    assert conn.sql == ['SELECT f.face_label, f.vector FROM face_vector f JOIN "user" u USING (u_id) '
                        'WHERE u.user_active = TRUE ORDER BY vector_id']
    (gallery,) = loaded
    assert [i.label for i in gallery.database] == ["pearl", "jane"]


def test_photos_without_vectors_counts_only_unconverted_labels(face_vectors, tmp_path):
    for name in ("pearl-0.jpg", "pearl-1.jpg", "jane-0.jpg", "Jane-1.png", "kim-0.jpg", ".kim-ab12-0.jpg",
                 ".gitkeep", "notes.txt"):
        (tmp_path / name).write_bytes(b"x")
    conn = _Conn([], labels=("pearl",))
    assert asyncio.run(face_vectors.photos_without_vectors(_Pool(conn), tmp_path)) == 3
    assert asyncio.run(face_vectors.photos_without_vectors(_Pool(conn), tmp_path / "missing")) == 0


def test_load_gallery_sets_the_gallery_under_the_lock(face_vectors, monkeypatch):
    monkeypatch.setattr(FaceRecognitionService, "_available", True)
    service = object.__new__(FaceRecognitionService)
    service.lock = threading.RLock()
    seen = []

    def set_faces_database(gallery):
        seen.append((gallery, service.lock._is_owned()))
    service.face_id = types.SimpleNamespace(set_faces_database=set_faces_database)
    gallery = face_vectors.build_gallery([])
    service.load_gallery(gallery)
    assert seen == [(gallery, True)]


def test_service_starts_empty_and_lifespan_loads_rows(face_vectors, monkeypatch, caplog):
    # The constructor no longer reads the folder: FacesDatabase.__init__ (the folder reader) must not run.
    class NoFolder(frs.FacesDatabase):
        def __init__(self, *args, **kwargs):
            raise AssertionError("the photo folder was read")

    class Model:
        def __init__(self, *args, **kwargs):
            self.faces_database = None

        def deploy(self, *args, **kwargs):
            pass

        def set_faces_database(self, database):
            self.faces_database = database

    monkeypatch.setattr(frs, "_load_imports", lambda: None)
    monkeypatch.setattr(frs, "Core", lambda: None)
    for name in ("FaceDetector", "LandmarksDetector", "FaceIdentifier"):
        monkeypatch.setattr(frs, name, Model)
    monkeypatch.setattr(frs, "FacesDatabase", NoFolder)
    monkeypatch.setattr(FaceRecognitionService, "_available", False)
    service = FaceRecognitionService()
    assert FaceRecognitionService._available is True
    assert isinstance(service.face_id.faces_database, NoFolder) and len(service.face_id.faces_database) == 0
    service.reload_gallery()   # a logged no-op until registration writes rows (Task 2)
    assert len(service.face_id.faces_database) == 0

    # The lifespan loads the rows after FaceRecognitionService.get_instance(), then warns about unconverted photos.
    from app import main
    calls = []

    async def nothing(*args, **kwargs):
        return None

    async def reload(pool=None):
        calls.append("reload")
        return 2

    async def unconverted(pool=None, folder=None):
        calls.append("folder")
        return 3

    _stub_lifespan(monkeypatch, main, nothing)
    monkeypatch.setattr(main, "FaceRecognitionService",
                        types.SimpleNamespace(get_instance=lambda: calls.append("face")))
    monkeypatch.setattr(main.face_vectors, "reload", reload)
    monkeypatch.setattr(main.face_vectors, "photos_without_vectors", unconverted)

    async def start_and_stop():
        async with main.lifespan(main.app):
            pass

    with caplog.at_level(logging.INFO):
        asyncio.run(start_and_stop())
    assert calls == ["face", "reload", "folder"]
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING" and "conversion" in r.getMessage()]
    assert len(warnings) == 1 and "3 face photo" in warnings[0]
    assert any("2 label" in r.getMessage() for r in caplog.records if r.levelname == "INFO")


def test_a_failed_gallery_load_never_stops_the_start(face_vectors, monkeypatch, caplog):
    from app import main

    async def nothing(*args, **kwargs):
        return None

    async def broken(pool=None):
        raise RuntimeError("dbSECRET")

    _stub_lifespan(monkeypatch, main, nothing)
    monkeypatch.setattr(main, "FaceRecognitionService", types.SimpleNamespace(get_instance=lambda: None))
    monkeypatch.setattr(main.face_vectors, "reload", broken)
    monkeypatch.setattr(main.face_vectors, "photos_without_vectors", broken)

    async def start_and_stop():
        async with main.lifespan(main.app):
            pass

    with caplog.at_level(logging.WARNING):
        asyncio.run(start_and_stop())
    assert "the face vectors were not loaded (RuntimeError)" in caplog.text
    assert "the photo folder check failed (RuntimeError)" in caplog.text
    assert "dbSECRET" not in caplog.text


def _stub_lifespan(monkeypatch, main, nothing):
    """Everything else the lifespan calls, as tests/test_ai_startup.py stubs it."""
    from app.services import ai_settings, ai_store

    class Pool:
        def acquire(self):
            return _Pool("conn").acquire()

    monkeypatch.setattr(ai_settings, "_snapshot", None)
    monkeypatch.setattr(main, "run_startup_checks", lambda: [])
    monkeypatch.setattr(main, "init_pool", nothing)
    monkeypatch.setattr(main, "close_pool", nothing)
    monkeypatch.setattr(main, "get_pool", lambda: Pool())
    monkeypatch.setattr(main, "check_restore_state", nothing)
    monkeypatch.setattr(main, "check_ai_settings", lambda snapshot: [])
    monkeypatch.setattr(main, "register_documents", nothing)
    monkeypatch.setattr(ai_store, "seed_from_env", nothing)
    monkeypatch.setattr(ai_store, "fill_openrouter_keys_from_env", nothing)

    from test_ai_startup import _snapshot

    async def load(conn):
        return _snapshot()

    monkeypatch.setattr(ai_store, "load", load)
    monkeypatch.setattr(main.line_pause, "load", nothing)
    monkeypatch.setattr(main.line_photo, "sweep_stale", nothing)
    for service in ("EmotionService", "OCRService", "LineService", "IntakeDetectionService", "LandmarkService"):
        monkeypatch.setattr(main, service, types.SimpleNamespace(get_instance=lambda: None))
    for name in ("start_scheduler", "stop_scheduler"):
        monkeypatch.setattr(main, name, lambda: None)
    for name in ("start_dispatcher", "stop_dispatcher"):
        monkeypatch.setattr(main, name, nothing)
    monkeypatch.setattr(main.reminders, "reload", nothing)
    monkeypatch.setattr(main.startup_report, "finish_in_background", lambda *a, **k: None)
