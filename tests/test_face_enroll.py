"""Face enrollment stores three face vectors in the database, and refuses a face the gallery already knows under
another label.

3 Oct 2026: one person enrolled under two accounts ("a" and "ab"). The gallery then held the face twice, and
the monitor's identity check for the robot's patient matched it to the other account, so Reachy never
verified the patient. POST /api/face/enroll goes through api_face.save_enrollment. 8 Nov 2026: registration
keeps the 256 numbers of each photo's main face (face_vector rows), never the photo; the legacy
/auth/register-photos is retired (410). No models are needed here: the face service is a fake, or the real
Intel matching code (FaceIdentifier.postprocess + FacesDatabase.match_faces) with fake detectors.
"""
import asyncio
import io
import json
import logging
import sys
import threading
import time
import types
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi import FastAPI, UploadFile
from fastapi.testclient import TestClient

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app import config
from app.routers import api_face, auth
from app.services import face_recognition_service as frs
from app.services import face_vectors
from app.services.face_recognition_service import FaceRecognitionService

USERS = {
    1: {"u_id": 1, "face_label": "a", "face_enrolled": True},
    7: {"u_id": 7, "face_label": "pearl"},
    8: {"u_id": 8, "face_label": "jane"},
    9: {"u_id": 9, "face_label": "gone", "active": False},
    34: {"u_id": 34, "face_label": "ab"},
}


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *args):
        return False


class _Transaction:
    """All or nothing, as asyncpg's: the face_vector rows go back to how they were when the block raises."""

    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        self.saved = ([dict(row) for row in self.conn.vectors], self.conn.next_id)
        self.conn.in_transaction = True

    async def __aexit__(self, kind, *args):
        self.conn.in_transaction = False
        if kind is not None:
            self.conn.vectors, self.conn.next_id = self.saved
        return False


class _Connection:
    """The "user" rows above and a face_vector table in memory."""

    def __init__(self):
        self.enrolled = []
        self.label_lookups = []
        self.vectors = []        # face_vector rows: vector_id, u_id, face_label, vector, source
        self.next_id = 1
        self.in_transaction = False

    def transaction(self):
        return _Transaction(self)

    def add_vectors(self, u_id, vectors, label=None):
        for vector in vectors:
            self.vectors.append({"vector_id": self.next_id, "u_id": u_id,
                                 "face_label": label or USERS[u_id]["face_label"], "vector": vector,
                                 "source": "enroll"})
            self.next_id += 1

    def rows_of(self, u_id):
        return [row for row in self.vectors if row["u_id"] == u_id]

    async def fetchrow(self, query, *args):
        assert 'FROM "user"' in query
        if "WHERE face_label = $1" in query:
            active_only = "user_active = TRUE" in query
            self.label_lookups.append((args[0], active_only))
            return next(({"u_id": user["u_id"]} for user in USERS.values()
                         if user["face_label"] == args[0] and (user.get("active", True) or not active_only)), None)
        assert "u_id=$1 AND user_active=TRUE" in query
        user = USERS.get(args[0])
        return {**user, "face_enrolled": user.get("face_enrolled", False)} if user and user.get("active", True) \
            else None

    async def fetchval(self, query, *args):
        if query.startswith("INSERT INTO face_vector"):
            assert self.in_transaction
            assert "(u_id, face_label, vector, source) VALUES ($1, $2, $3, 'enroll') RETURNING vector_id" in query
            u_id, label, vector = args
            assert isinstance(vector, bytes)
            self.add_vectors(u_id, [vector], label)
            return self.next_id - 1
        assert query == "SELECT count(*) FROM face_vector WHERE u_id = $1"
        return len(self.rows_of(args[0]))

    async def fetch(self, query, *args):
        # face_vectors.fetch_rows: each row under its owning active account's label, oldest first.
        assert query == face_vectors.ROWS_SQL
        return [{"face_label": USERS[row["u_id"]]["face_label"], "vector": row["vector"]}
                for row in sorted(self.vectors, key=lambda row: row["vector_id"])
                if USERS[row["u_id"]].get("active", True)]

    async def execute(self, query, *args):
        if "face_enrolled = TRUE" in query:
            self.enrolled.append(args[0])
            return
        assert query == "DELETE FROM face_vector WHERE u_id = $1"
        assert self.in_transaction
        self.vectors = [row for row in self.vectors if row["u_id"] != args[0]]


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


def _desc(seed):
    """A 256-number descriptor, as face-reidentification-retail-0095 gives (float32)."""
    return np.random.default_rng(seed).standard_normal(256).astype(np.float32)


class _FakeFaces:
    """Answers match_enrollment_face from a script, one answer per photo. load_gallery is what
    face_vectors.reload gives the service; there is no reload_gallery, so registration must not call it."""

    def __init__(self, answers):
        self.lock = threading.RLock()
        self.answers = list(answers)
        self.checked = 0
        self.own_labels = []
        self.reloads = 0
        self.loaded = []

    def match_enrollment_face(self, frame, own_label=None):
        self.checked += 1
        self.own_labels.append(own_label)
        return self.answers.pop(0)

    def load_gallery(self, gallery):
        self.reloads += 1
        self.loaded = [identity.label for identity in gallery.database]


_UNSET = object()


def _found(label=None, box=(10, 20, 40, 50), distance=None, error=None, reason=None, descriptor=_UNSET):
    if label is not None and distance is None:
        distance = 0.12
    if reason is None:
        reason = "ok" if box else "no_face"
    if descriptor is _UNSET:
        # match_enrollment_face gives the descriptor only for a photo it accepts.
        descriptor = _desc(0) if reason == "ok" and box and label is None and error is None else None
    return {"box": list(box) if box else None, "label": label, "distance": distance, "error": error,
            "reason": reason, "descriptor": descriptor}


def _photo(seed=0):
    image = np.zeros((120, 100, 3), np.uint8)
    image[:, :, 0] = np.arange(100, dtype=np.uint8)
    image[:, :, 1] = seed * 40
    return cv2.imencode(".jpg", image)[1].tobytes()


def _uploads():
    return [UploadFile(file=io.BytesIO(_photo(i)), filename=f"face-{i}.jpg") for i in range(3)]


def _result(response):
    if isinstance(response, dict):
        return 200, response
    return response.status_code, json.loads(response.body)


def _enroll(u_id=7):
    return asyncio.run(api_face.enroll_face(photos=_uploads(), user={"u_id": u_id}))


ALREADY_REGISTERED = {"error": "face_already_registered", "detail": api_face.FACE_ALREADY_REGISTERED}
WITHOUT_ACCOUNT = {"error": "face_registered_without_account", "detail": api_face.FACE_WITHOUT_ACCOUNT}
RETIRED = {"error": "use_app_registration", "detail": "Please register in the app."}
GALLERY_UNREADABLE = {"error": "Face check unavailable. Please try again later."}
REAL_BUILD_GALLERY = face_vectors.build_gallery


@pytest.fixture
def gallery(monkeypatch, tmp_path):
    folder = tmp_path / "face_gallery"
    folder.mkdir()
    monkeypatch.setattr(config, "FACE_GALLERY_DIR", folder)
    monkeypatch.setattr(FaceRecognitionService, "_available", True)
    monkeypatch.setattr(api_face, "_enroll_lock", asyncio.Lock())
    conn = _Connection()
    monkeypatch.setattr(api_face, "get_pool", lambda: _Pool(conn))
    # face_vectors.reload runs as it is (rows read, then the service's load_gallery); only the FacesDatabase it
    # builds is a stand-in, so these tests need no OpenVINO.
    monkeypatch.setattr(face_vectors, "build_gallery", _Gallery.from_rows)
    return types.SimpleNamespace(folder=folder, conn=conn)


def _use(monkeypatch, service):
    monkeypatch.setattr(FaceRecognitionService, "get_instance", classmethod(lambda cls: service))


def _files(folder):
    return {path.name: path.read_bytes() for path in folder.iterdir()}


def _snapshot(conn):
    return [dict(row) for row in conn.vectors]


def test_face_of_another_account_is_refused_without_naming_it(gallery, monkeypatch):
    gallery.conn.add_vectors(1, [face_vectors.to_bytes(_desc(1))])
    before = _snapshot(gallery.conn)
    service = _FakeFaces([_found("a")])
    _use(monkeypatch, service)

    status, body = _result(_enroll())

    assert status == 409
    # A fixed sentence: the other account's label, name and email never reach the response.
    assert body == ALREADY_REGISTERED
    assert "face login" in body["detail"]
    # The caller's own label is left out of the match, and "a" was looked up as face login does.
    assert service.own_labels == ["pearl"]
    assert gallery.conn.label_lookups == [("a", True)]
    assert _snapshot(gallery.conn) == before and _files(gallery.folder) == {}
    assert service.reloads == 1 and gallery.conn.enrolled == []      # only the refresh before the checks


@pytest.mark.parametrize("label", ["ghost", "gone"])   # no account at all / a deactivated account
def test_face_in_the_gallery_without_an_active_account_says_so(gallery, monkeypatch, caplog, label):
    """Face login answers 404 for such a face, so 'sign in with face login' would be advice that cannot work."""
    service = _FakeFaces([_found(label)])
    _use(monkeypatch, service)

    with caplog.at_level(logging.WARNING, logger=api_face.logger.name):
        status, body = _result(_enroll())

    assert (status, body) == (409, WITHOUT_ACCOUNT)
    assert label not in json.dumps(body)
    # The operator needs the label to find the stale photos; it names no account.
    assert any(label in record.getMessage() for record in caplog.records)
    assert gallery.conn.vectors == [] and _files(gallery.folder) == {}
    assert service.reloads == 1 and gallery.conn.enrolled == []      # only the refresh before the checks


def test_any_one_of_the_three_photos_refuses_all(gallery, monkeypatch):
    service = _FakeFaces([_found(), _found(), _found("a", distance=0.29)])
    _use(monkeypatch, service)

    status, body = _result(_enroll())

    assert (status, body) == (409, ALREADY_REGISTERED)
    assert service.checked == 3
    assert gallery.conn.vectors == [] and _files(gallery.folder) == {}
    assert service.reloads == 1 and gallery.conn.enrolled == []      # only the refresh before the checks


def test_enrolling_stores_three_vectors_and_no_files(gallery, monkeypatch):
    descriptors = [_desc(11), _desc(12), _desc(13)]
    service = _FakeFaces([_found(descriptor=d) for d in descriptors])
    _use(monkeypatch, service)

    status, body = _result(_enroll())

    assert status == 200
    assert _files(gallery.folder) == {}          # registration no longer writes to the photo folder
    rows = gallery.conn.rows_of(7)
    assert len(rows) == 3 and gallery.conn.vectors == rows
    assert [len(row["vector"]) for row in rows] == [1024] * 3
    assert [row["source"] for row in rows] == ["enroll"] * 3
    assert [row["face_label"] for row in rows] == ["pearl"] * 3
    # Each row is that photo's descriptor, in photo order, as 256 little-endian float32 numbers.
    assert [row["vector"] for row in rows] == [face_vectors.to_bytes(d) for d in descriptors]
    assert [np.array_equal(face_vectors.from_bytes(row["vector"]), d) for row, d in zip(rows, descriptors)] \
        == [True] * 3
    # Login sees the new face at once, and the account is marked enrolled.
    assert service.reloads == 2 and service.loaded == ["pearl"]      # before the checks, and after the rows
    assert gallery.conn.enrolled == [7]


def test_the_answer_carries_no_paths_or_numbers(gallery, monkeypatch):
    service = _FakeFaces([_found(descriptor=_desc(i)) for i in range(3)])
    _use(monkeypatch, service)

    status, body = _result(_enroll())

    assert status == 200
    # Three entries (Onboarding needs saved.length === 3): the new rows' ids as text, nothing else.
    assert body == {"saved": [str(row["vector_id"]) for row in gallery.conn.rows_of(7)], "face_label": "pearl"}
    assert all(isinstance(entry, str) and entry.isdigit() for entry in body["saved"])
    text = json.dumps(body)
    assert "/" not in text and "\\" not in text and "face_gallery" not in text and ".jpg" not in text
    assert "descriptor" not in text and "vector" not in text


def test_reenrolling_replaces_your_vectors(gallery, monkeypatch):
    old = [face_vectors.to_bytes(_desc(90 + i)) for i in range(3)]
    gallery.conn.add_vectors(7, old)
    gallery.conn.add_vectors(1, [face_vectors.to_bytes(_desc(1))])
    someone_else = _snapshot(gallery.conn)[3]
    old_ids = {row["vector_id"] for row in gallery.conn.rows_of(7)}
    # The service leaves pearl's own vectors out of the match, so it finds no other label.
    service = _FakeFaces([_found(descriptor=_desc(20 + i)) for i in range(3)])
    _use(monkeypatch, service)

    status, body = _result(_enroll())

    assert status == 200 and body["face_label"] == "pearl" and len(body["saved"]) == 3
    assert service.own_labels == ["pearl"] * 3
    rows = gallery.conn.rows_of(7)
    assert len(rows) == 3                                       # replaced, never added to
    assert [row["vector"] for row in rows] == [face_vectors.to_bytes(_desc(20 + i)) for i in range(3)]
    assert not old_ids & {row["vector_id"] for row in rows}
    assert gallery.conn.rows_of(1) == [someone_else]            # another account's vector is untouched
    assert service.reloads == 2 and sorted(service.loaded) == ["a", "pearl"]
    assert gallery.conn.enrolled == [7]


def test_first_ever_registration(gallery, monkeypatch):
    """An empty gallery: nobody to compare with, yet each photo's descriptor is computed and stored."""
    descriptor = _vector(0.3).astype(np.float32)
    face_id = _Identifier([], descriptor)
    service = _service(monkeypatch, [_Roi(10, 20, 60, 70)], face_id)
    _use(monkeypatch, service)

    status, body = _result(_enroll())

    assert status == 200 and len(body["saved"]) == 3
    assert len(face_id.started) == 3                    # one descriptor per photo, on the main face's box
    rows = gallery.conn.rows_of(7)
    assert [row["vector"] for row in rows] == [face_vectors.to_bytes(descriptor)] * 3
    # The service now holds the gallery built from the rows.
    assert [identity.label for identity in face_id.faces_database.database] == ["pearl"]
    assert gallery.conn.enrolled == [7]


@pytest.mark.parametrize("answer, status", [
    (_found("a"), 409),
    (_found(box=None), 400),
    (_found(box=None, reason="two_faces"), 400),
    (_found(box=None, reason="come_closer"), 400),
    (_found(box=None, error="inference failed"), 503),
    (_found(descriptor=None), 503),                 # a face that could not be checked is never stored
])
def test_any_refused_photo_stores_nothing(gallery, monkeypatch, answer, status):
    gallery.conn.add_vectors(7, [face_vectors.to_bytes(_desc(90 + i)) for i in range(3)])
    before = _snapshot(gallery.conn)
    service = _FakeFaces([_found(), _found(), answer])
    _use(monkeypatch, service)

    got_status, _ = _result(_enroll())

    assert got_status == status
    assert service.checked == 3
    assert _snapshot(gallery.conn) == before            # your old vectors stay, nothing new is written
    assert _files(gallery.folder) == {}
    assert service.reloads == 1 and gallery.conn.enrolled == []      # only the refresh before the checks


@pytest.mark.parametrize("answer, status, error", [
    (_found(box=None), 400, "No face detected in photo 1"),
    (_found(box=None, error="inference failed"), 503, "Face detection failed for photo 1"),
    (_found(box=None, reason="two_faces"), 400, "Two faces in photo 1"),
    (_found(box=None, reason="come_closer"), 400, "Face too small in photo 1"),
])
def test_photo_without_a_usable_face_saves_nothing(gallery, monkeypatch, answer, status, error):
    service = _FakeFaces([_found(), answer, _found()])
    _use(monkeypatch, service)

    got_status, body = _result(_enroll())

    assert (got_status, body) == (status, {"error": error})
    assert gallery.conn.vectors == [] and _files(gallery.folder) == {}
    assert service.reloads == 1 and gallery.conn.enrolled == []      # only the refresh before the checks


class _OneFace(_FakeFaces):
    """Every photo is the same person: matched to any other label the loaded gallery holds."""

    def __init__(self, load_seconds=0.0):
        super().__init__([])
        self.load_seconds = load_seconds

    def match_enrollment_face(self, frame, own_label=None):
        self.checked += 1
        others = [label for label in self.loaded if label != own_label.lower()]
        return _found(others[0] if others else None)

    def load_gallery(self, gallery):
        time.sleep(self.load_seconds)
        super().load_gallery(gallery)


def test_registration_refreshes_the_gallery_before_checking(gallery, monkeypatch):
    """jane's vectors are in the database but not in this process's gallery (another process stored them, or a
    reload after their commit failed). The same face registering for pearl is still refused."""
    gallery.conn.add_vectors(8, [face_vectors.to_bytes(_desc(i)) for i in range(3)])
    before = _snapshot(gallery.conn)
    service = _OneFace()
    assert service.loaded == []          # the in-memory gallery does not know jane
    _use(monkeypatch, service)

    assert _result(_enroll(u_id=7)) == (409, ALREADY_REGISTERED)
    assert service.loaded == ["jane"] and service.reloads == 1
    assert _snapshot(gallery.conn) == before and gallery.conn.enrolled == []


def test_a_failed_refresh_refuses_and_stores_nothing(gallery, monkeypatch, caplog):
    """Without the stored vectors the duplicate check cannot be trusted: fail closed, before any photo is checked."""
    gallery.conn.add_vectors(7, [face_vectors.to_bytes(_desc(90 + i)) for i in range(3)])
    before = (_snapshot(gallery.conn), gallery.conn.next_id)
    service = _FakeFaces([_found()] * 3)
    _use(monkeypatch, service)

    async def broken(pool=None):
        raise RuntimeError("connection refused by db-host-secret-7")
    monkeypatch.setattr(face_vectors, "reload", broken)

    with caplog.at_level(logging.DEBUG):
        status, body = _result(_enroll())

    assert (status, body) == (503, GALLERY_UNREADABLE)
    assert service.checked == 0                                   # no photo was checked
    assert (_snapshot(gallery.conn), gallery.conn.next_id) == before   # no DELETE, no INSERT
    assert gallery.conn.enrolled == []
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "db-host-secret-7" not in logged and "db-host-secret-7" not in json.dumps(body)
    assert "RuntimeError" in logged                               # the exception's type only


def test_a_failed_reload_after_the_commit_still_answers_success(gallery, monkeypatch, caplog):
    """The vectors are stored; a reload that fails after the commit only logs its type. The registration answers
    its normal success and the account is marked enrolled (the next registration's pre-check reload refreshes the
    gallery)."""
    service = _FakeFaces([_found(descriptor=_desc(11 + i)) for i in range(3)])
    _use(monkeypatch, service)
    real_reload = face_vectors.reload
    calls = []

    async def second_fails(pool=None):
        calls.append(pool)
        if len(calls) == 2:
            raise RuntimeError("db-host-secret-9")
        return await real_reload(pool)
    monkeypatch.setattr(face_vectors, "reload", second_fails)

    with caplog.at_level(logging.DEBUG):
        status, body = _result(_enroll())

    assert status == 200 and len(body["saved"]) == 3
    assert len(calls) == 2
    assert len(gallery.conn.rows_of(7)) == 3 and gallery.conn.enrolled == [7]
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("RuntimeError" in message for message in warnings)
    assert "db-host-secret-9" not in "\n".join(r.getMessage() for r in caplog.records)


def test_two_accounts_enrolling_one_face_at_once_cannot_both_pass(gallery, monkeypatch):
    # A slow load: were the lock released before the winner's load ends, the other registration's check could run
    # against a gallery without the winner's vectors and pass too.
    service = _OneFace(load_seconds=0.3)
    _use(monkeypatch, service)

    async def both():
        return await asyncio.gather(api_face.enroll_face(photos=_uploads(), user={"u_id": 7}),
                                    api_face.enroll_face(photos=_uploads(), user={"u_id": 8}))

    results = dict(zip((7, 8), (_result(response) for response in asyncio.run(both()))))

    # Either may win (the uploads are read in threads); exactly one does. The winner's rows reach the gallery
    # before the lock is released, so the second registration sees them.
    winners = [u_id for u_id, (status, _) in results.items() if status == 200]
    assert len(winners) == 1
    loser = 15 - winners[0]
    assert results[loser] == (409, ALREADY_REGISTERED)
    assert {row["u_id"] for row in gallery.conn.vectors} == {winners[0]}
    assert len(gallery.conn.vectors) == 3
    assert service.loaded == [USERS[winners[0]]["face_label"]]
    assert gallery.conn.enrolled == winners
    assert _files(gallery.folder) == {}


def test_enrollment_status_counts_rows(gallery):
    gallery.conn.add_vectors(7, [face_vectors.to_bytes(_desc(i)) for i in range(3)])
    (gallery.folder / "jane-0.jpg").write_bytes(b"an old photo")   # the folder no longer counts

    def status(u_id):
        return asyncio.run(api_face.enrollment_status(user={"u_id": u_id}))

    assert status(7) == {"enrolled": True, "count": 3}
    assert status(8) == {"enrolled": False, "count": 0}
    assert status(1) == {"enrolled": True, "count": 0}     # face_enrolled still counts
    assert status(9) == {"enrolled": False, "count": 0}    # no active account


def test_legacy_register_photos_is_retired(gallery, monkeypatch):
    service = _FakeFaces([_found()] * 3)
    _use(monkeypatch, service)
    app = FastAPI()
    app.include_router(auth.router, prefix="/auth")
    files = [("photos", (f"newbie-{i}.jpg", _photo(i), "image/jpeg")) for i in range(3)]

    with TestClient(app) as client:
        answer = client.post("/auth/register-photos", data={"face_label": "newbie"}, files=files)
        empty = client.post("/auth/register-photos")

    assert (answer.status_code, answer.json()) == (410, RETIRED)
    assert (empty.status_code, empty.json()) == (410, RETIRED)
    assert service.checked == 0 and service.reloads == 0
    assert gallery.conn.vectors == [] and _files(gallery.folder) == {}
    # The older page shows the answer's sentence instead of going on to /auth/register.
    script = (Path(__file__).resolve().parents[1] / "static" / "js" / "photo_capture.js").read_text(encoding="utf-8")
    assert "resp.status === 410" in script and "data.detail" in script


def test_descriptor_never_logged(gallery, monkeypatch, caplog):
    marker = np.full(256, 0.987654321, np.float32)
    service = _FakeFaces([_found(descriptor=marker)] * 3 + [_found(descriptor=marker), _found("ghost")])
    _use(monkeypatch, service)

    with caplog.at_level(logging.DEBUG):
        assert _result(_enroll())[0] == 200
        assert _result(_enroll(u_id=8))[0] == 409

    raw = face_vectors.to_bytes(marker)
    for record in caplog.records:
        text = record.getMessage()
        assert "0.98765" not in text and "9876" not in text
        assert raw.hex()[:16] not in text and repr(raw[:8]) not in text
        assert not any(isinstance(arg, (bytes, np.ndarray)) for arg in (record.args or ()))

# --- FaceRecognitionService.match_enrollment_face, with fake models -----------------------------------

class _Roi:
    def __init__(self, x, y, w, h, confidence=0.9):
        self.position = np.array([x, y], dtype=float)
        self.size = np.array([w, h], dtype=float)
        self.confidence = confidence

    @classmethod
    def from_output(cls, output):
        return cls(output[3], output[4], output[5], output[6], output[2])


class _Detector:
    def __init__(self, service, rois):
        self.service = service
        self.rois = rois
        self.lock_held = []

    def infer(self, inputs):
        # Another thread must not be able to take the service lock while this runs.
        result = []

        def probe():
            taken = self.service.lock.acquire(blocking=False)
            if taken:
                self.service.lock.release()
            result.append(taken)

        thread = threading.Thread(target=probe)
        thread.start()
        thread.join()
        self.lock_held.append(not result[0])
        return self.rois


class _Landmarks:
    def __init__(self):
        self.calls = []

    def infer(self, inputs):
        frame, rois = inputs
        self.calls.append(rois)
        return [np.zeros((5, 2)) for _ in rois]


def _vector(distance):
    """A unit descriptor at this FacesDatabase distance ((1 - cosine) / 2) from the first axis."""
    cos = 1 - 2 * distance
    vector = np.zeros(256)
    vector[0] = cos
    vector[1] = np.sqrt(1 - cos ** 2)
    return vector


class _Gallery:
    """FacesDatabase's shape: identities with a label and descriptors, and MIN_DIST match_faces."""

    def __init__(self, identities):
        self.database = [types.SimpleNamespace(label=label, descriptors=[vector]) for label, vector in identities]

    def __len__(self):
        return len(self.database)

    def __getitem__(self, index):
        return self.database[index]

    @classmethod
    def from_rows(cls, rows):
        """face_vectors.build_gallery's grouping (one identity per lower-case label, row order) without OpenVINO."""
        gallery = cls([])
        by_label = {}
        for label, raw in rows:
            label = label.lower()
            if label not in by_label:
                by_label[label] = types.SimpleNamespace(label=label, descriptors=[])
                gallery.database.append(by_label[label])
            by_label[label].descriptors.append(face_vectors.from_bytes(raw))
        return gallery

    def match_faces(self, descriptors, match_algo):
        assert match_algo == "MIN_DIST"
        matches = []
        for desc in descriptors:
            distances = [min((1 - np.dot(desc, known) / (np.linalg.norm(desc) * np.linalg.norm(known))) / 2
                             for known in identity.descriptors) for identity in self.database]
            index = int(np.argmin(distances))
            matches.append((index, distances[index]))
        return matches


class _Identifier:
    def __init__(self, identities, descriptor):
        self.faces_database = _Gallery(identities)
        self.match_threshold = config.FACE_MATCH_THRESHOLD
        self.match_algo = "MIN_DIST"
        self.descriptor = descriptor
        self.started = []

    def clear(self):
        pass

    def set_faces_database(self, database):
        self.faces_database = database

    def start_async(self, frame, rois, landmarks):
        self.started.append(rois)

    def get_descriptors(self):
        return [] if self.descriptor is None else [self.descriptor]


def _without_descriptor(found):
    """The answer's other keys (a numpy array cannot be compared inside a dict)."""
    return {key: value for key, value in found.items() if key != "descriptor"}


def _service(monkeypatch, rois, face_id):
    monkeypatch.setattr(FaceRecognitionService, "_available", True)
    service = object.__new__(FaceRecognitionService)
    service.lock = threading.RLock()
    monkeypatch.setattr(frs, "FaceDetector", type("FD", (), {"Result": _Roi.from_output}))
    service.face_det = _Detector(service, rois)
    service.lm_det = _Landmarks()
    service.face_id = face_id
    return service


FRAME = np.zeros((480, 640, 3), np.uint8)


def test_matches_only_the_face_enrollment_stores_under_the_service_lock(monkeypatch):
    first, second = _Roi(100.7, 50.2, 200.9, 240.4), _Roi(400, 60, 120, 150)
    face_id = _Identifier([("a", _vector(0.2)), ("ab", _vector(0.05))], _vector(0.0))
    service = _service(monkeypatch, [first, second], face_id)

    found = service.match_enrollment_face(FRAME, "pearl")

    assert found == {"box": [100, 50, 200, 240], "label": "ab", "distance": pytest.approx(0.05), "error": None,
                     "reason": "ok", "descriptor": None}   # refused: no descriptor to store
    assert len(service.lm_det.calls) == 1 and len(service.lm_det.calls[0]) == 1
    (roi,) = service.lm_det.calls[0]
    assert list(roi.position) == pytest.approx([100.7, 50.2]) and face_id.started == [[roi]]
    assert service.face_det.lock_held == [True]


def test_own_photos_are_left_out_even_when_closest(monkeypatch):
    """The 3 Oct gallery: the face is closest to "ab", yet within the threshold of "a" too."""
    identities = [("a", _vector(0.05)), ("ab", _vector(0.0))]

    as_ab = _service(monkeypatch, [_Roi(0, 0, 200, 200)], _Identifier(identities, _vector(0.0)))
    found = as_ab.match_enrollment_face(FRAME, "AB")   # the caller's label, compared without case
    assert found["label"] == "a" and found["distance"] == pytest.approx(0.05)

    as_a = _service(monkeypatch, [_Roi(0, 0, 200, 200)], _Identifier(identities, _vector(0.0)))
    found = as_a.match_enrollment_face(FRAME, "a")
    assert found["label"] == "ab" and found["distance"] == pytest.approx(0.0, abs=1e-9)


def test_unknown_face_own_label_only_and_empty_gallery_match_nobody(monkeypatch):
    unknown = _service(monkeypatch, [_Roi(1, 2, 200, 200)], _Identifier([("a", _vector(0.0))], _vector(0.42)))
    found = unknown.match_enrollment_face(FRAME, "pearl")
    assert _without_descriptor(found) == {"box": [1, 2, 200, 200], "label": None, "distance": pytest.approx(0.42),
                                          "error": None, "reason": "ok"}
    assert np.array_equal(found["descriptor"], _vector(0.42))

    for identities in ([("pearl", _vector(0.0))], []):
        descriptor = _vector(0.1)
        face_id = _Identifier(identities, descriptor)
        service = _service(monkeypatch, [_Roi(1, 2, 200, 200)], face_id)
        found = service.match_enrollment_face(FRAME, "pearl")
        assert _without_descriptor(found) == {"box": [1, 2, 200, 200], "label": None, "distance": None,
                                              "error": None, "reason": "ok"}
        # Nobody else to compare with, yet the descriptor is computed on the main face's box, to be stored.
        assert np.array_equal(found["descriptor"], descriptor)
        (roi,) = service.lm_det.calls[0]
        assert face_id.started == [[roi]] and list(roi.position) == [1, 2] and list(roi.size) == [200, 200]


def test_no_face_model_failure_and_missing_descriptor(monkeypatch):
    service = _service(monkeypatch, [], _Identifier([("a", _vector(0.0))], _vector(0.1)))
    assert service.match_enrollment_face(FRAME, "pearl") == {"box": None, "label": None, "distance": None,
                                                             "error": None, "reason": "no_face",
                                                             "descriptor": None}

    def broken(inputs):
        raise RuntimeError("inference failed")
    service.face_det.infer = broken
    found = service.match_enrollment_face(FRAME, "pearl")
    assert found["error"] == "inference failed" and found["descriptor"] is None

    # A face that could not be checked is never passed as unknown, whether or not the gallery has other labels.
    for identities in ([("a", _vector(0.0))], []):
        no_descriptor = _service(monkeypatch, [_Roi(1, 2, 200, 200)], _Identifier(identities, None))
        found = no_descriptor.match_enrollment_face(FRAME, "pearl")
        assert found["error"] and found["box"] is None and found["label"] is None and found["descriptor"] is None


# --- The real matching code: MIN_DIST and FACE_MATCH_THRESHOLD as login uses them --------------------

@pytest.fixture
def intel_matching():
    pytest.importorskip("openvino")
    pytest.importorskip("scipy")
    path = str(Path(__file__).resolve().parents[1] / "models" / "face_recognition")
    sys.path.insert(0, path)
    try:
        from face_identifier import FaceIdentifier
        from faces_database import FacesDatabase
    except ImportError as exc:
        pytest.skip(f"face recognition code not importable: {exc}")
    finally:
        sys.path.remove(path)
    return FaceIdentifier, FacesDatabase


def _real_face_id(intel_matching, identities, descriptor):
    """identities: [(label, [descriptor, ...])], as FacesDatabase builds them from <label>-<i>.jpg."""
    FaceIdentifier, FacesDatabase = intel_matching
    database = object.__new__(FacesDatabase)
    database.database = [FacesDatabase.Identity(label, list(vectors)) for label, vectors in identities]
    face_id = object.__new__(FaceIdentifier)
    face_id.match_threshold = config.FACE_MATCH_THRESHOLD   # as FaceRecognitionService deploys it
    face_id.match_algo = "MIN_DIST"
    face_id.faces_database = database
    face_id.clear = lambda: None
    face_id.start_async = lambda *args: None
    face_id.get_descriptors = lambda: [descriptor]
    return face_id


@pytest.mark.parametrize("offset, label", [
    (None, "a"),    # distance 0.10: login would sign this face in as "a"
    (-0.01, "a"),   # just within FACE_MATCH_THRESHOLD
    (0.01, None),   # just beyond it: login says Unknown, so a new account may enroll this face
])
def test_threshold_is_logins(monkeypatch, intel_matching, offset, label):
    distance = 0.10 if offset is None else config.FACE_MATCH_THRESHOLD + offset
    face_id = _real_face_id(intel_matching, [("a", [_vector(0.0)])], _vector(distance))
    service = _service(monkeypatch, [_Roi(0, 0, 200, 200)], face_id)

    found = service.match_enrollment_face(FRAME, "pearl")

    assert found["label"] == label
    assert found["distance"] == pytest.approx(distance)


def test_min_dist_picks_the_closest_label(monkeypatch, intel_matching):
    face_id = _real_face_id(intel_matching, [("a", [_vector(0.2)]), ("ab", [_vector(0.05)])], _vector(0.0))
    service = _service(monkeypatch, [_Roi(0, 0, 200, 200)], face_id)

    assert service.match_enrollment_face(FRAME, "pearl")["label"] == "ab"


def test_same_answer_as_logins_postprocess(monkeypatch, intel_matching):
    """With no label of the caller's in the gallery, the check is exactly face login's answer."""
    rng = np.random.default_rng(3)
    outcomes = set()
    for _ in range(60):
        # Distances spread across the threshold: cos ~ 1 / sqrt((1 + q^2) (1 + s^2)) for noise scales q, s.
        base = rng.normal(size=256)
        identities = [(f"p{k}", [base + rng.normal(size=256) * rng.uniform(0.2, 2.5)
                                 for _ in range(int(rng.integers(1, 3)))]) for k in range(int(rng.integers(1, 4)))]
        descriptor = base + rng.normal(size=256) * rng.uniform(0.3, 3.0)
        face_id = _real_face_id(intel_matching, identities, descriptor)
        service = _service(monkeypatch, [_Roi(0, 0, 200, 200)], face_id)

        found = service.match_enrollment_face(FRAME, "pearl")
        results, _ = face_id.postprocess()   # FaceIdentifier's own matching rule
        login = face_id.get_identity_label(results[0].id)

        assert found["label"] == (None if login == face_id.UNKNOWN_ID_LABEL else login)
        assert found["distance"] == pytest.approx(results[0].distance)
        outcomes.add(found["label"] is None)
    assert outcomes == {True, False}   # both matches and unknown faces were exercised


def _real_gallery_from_rows(monkeypatch):
    """The real face_vectors.build_gallery (a FacesDatabase) instead of the fixture's stand-in."""
    pytest.importorskip("openvino")
    frs._load_imports()
    monkeypatch.setattr(face_vectors, "build_gallery", REAL_BUILD_GALLERY)


def test_endpoint_refuses_the_incident_with_real_matching(gallery, monkeypatch, intel_matching):
    """A's vectors are stored; the same face enrolling for a new account is refused."""
    _real_gallery_from_rows(monkeypatch)
    gallery.conn.add_vectors(1, [face_vectors.to_bytes(_vector(0.0))])
    face_id = _real_face_id(intel_matching, [], _vector(0.05))
    service = _service(monkeypatch, [_Roi(10, 20, 200, 200)], face_id)
    _use(monkeypatch, service)

    assert _result(_enroll(u_id=8)) == (409, ALREADY_REGISTERED)
    assert gallery.conn.rows_of(8) == [] and len(gallery.conn.vectors) == 1 and _files(gallery.folder) == {}
    assert isinstance(face_id.faces_database, frs.FacesDatabase)   # matched against the gallery built from rows

    # The owner re-enrolling their own label is still allowed.
    status, body = _result(_enroll(u_id=1))
    assert status == 200 and body["face_label"] == "a"
    assert [row["vector"] for row in gallery.conn.rows_of(1)] == [face_vectors.to_bytes(_vector(0.05))] * 3
    assert gallery.conn.enrolled == [1]


def test_endpoint_refuses_a_duplicate_the_caller_is_closer_to(gallery, monkeypatch, intel_matching):
    """The 3 Oct gallery held the face as "a" and "ab". "ab" re-enrolling must not pass because its own old
    photos are closer: login takes the face for either, and the duplicate would stay. Neither account can
    enroll it until the duplicate is cleaned up by hand (as was done on 3 Oct)."""
    _real_gallery_from_rows(monkeypatch)
    gallery.conn.add_vectors(1, [face_vectors.to_bytes(_vector(0.05))])
    gallery.conn.add_vectors(34, [face_vectors.to_bytes(_vector(0.0))])
    before = _snapshot(gallery.conn)
    face_id = _real_face_id(intel_matching, [], _vector(0.0))
    service = _service(monkeypatch, [_Roi(10, 20, 200, 200)], face_id)
    _use(monkeypatch, service)

    assert _result(_enroll(u_id=34)) == (409, ALREADY_REGISTERED)
    assert _result(_enroll(u_id=1)) == (409, ALREADY_REGISTERED)
    assert isinstance(face_id.faces_database, frs.FacesDatabase)   # matched against the gallery built from rows
    assert _snapshot(gallery.conn) == before and _files(gallery.folder) == {} and gallery.conn.enrolled == []


# --- The main-face rule on registration photos ---------------------------------------------------------

def test_two_similar_size_faces_are_refused_and_nothing_is_stored(monkeypatch):
    face_id = _Identifier([("a", _vector(0.0))], _vector(0.0))
    service = _service(monkeypatch, [_Roi(0, 0, 200, 200), _Roi(300, 0, 180, 180)], face_id)

    found = service.match_enrollment_face(FRAME, "pearl")

    assert found == {"box": None, "label": None, "distance": None, "error": None, "reason": "two_faces",
                     "descriptor": None}
    assert face_id.started == []


def test_the_big_face_is_stored_when_a_small_one_is_also_there(monkeypatch):
    face_id = _Identifier([("a", _vector(0.2))], _vector(0.0))
    # The small face comes first and is the more confident one: rois[0] is not the face stored.
    small, big = _Roi(500, 10, 80, 80, confidence=0.99), _Roi(100, 50, 250, 250, confidence=0.7)
    service = _service(monkeypatch, [small, big], face_id)

    found = service.match_enrollment_face(FRAME, "pearl")

    assert found["reason"] == "ok" and found["box"] == [100, 50, 250, 250]
    (roi,) = service.lm_det.calls[0]
    assert list(roi.position) == [100, 50] and list(roi.size) == [250, 250]
    assert face_id.started == [[roi]]   # the descriptor is computed for the big face only


def test_a_too_small_face_is_refused(monkeypatch):
    face_id = _Identifier([("a", _vector(0.0))], _vector(0.0))
    service = _service(monkeypatch, [_Roi(10, 10, 50, 60)], face_id)

    found = service.match_enrollment_face(FRAME, "pearl")

    assert found["reason"] == "come_closer" and found["box"] is None and found["error"] is None
    assert found["descriptor"] is None and face_id.started == []


def test_only_the_main_face_is_matched_against_other_labels(monkeypatch):
    """The small second face would match "a"; the big one is new, so the photo passes."""
    class _PerRoi(_Identifier):
        def start_async(self, frame, rois, landmarks):
            super().start_async(frame, rois, landmarks)
            self.descriptor = _vector(0.0) if rois[0].size[0] < 100 else _vector(0.45)

    face_id = _PerRoi([("a", _vector(0.0))], _vector(0.45))
    service = _service(monkeypatch, [_Roi(500, 10, 80, 80), _Roi(100, 50, 250, 250)], face_id)

    found = service.match_enrollment_face(FRAME, "pearl")

    assert found["label"] is None and found["reason"] == "ok"
    assert np.array_equal(found["descriptor"], _vector(0.45))   # the stored vector is the main face's
