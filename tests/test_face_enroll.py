"""Face enrollment refuses a face the gallery already knows under another label.

3 Oct 2026: one person enrolled under two accounts ("a" and "ab"). The gallery then held the face twice, and
the monitor's identity check for the robot's patient matched it to the other account, so Reachy never
verified the patient. Both enrollment routes (POST /api/face/enroll and the legacy /auth/register-photos)
go through api_face.save_enrollment. No models are needed here: the face service is a fake, or the real
Intel matching code (FaceIdentifier.postprocess + FacesDatabase.match_faces) with fake detectors.
"""
import asyncio
import io
import json
import logging
import sys
import threading
import types
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi import UploadFile

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app import config
from app.routers import api_face, auth
from app.services.face_recognition_service import FaceRecognitionService

USERS = {
    1: {"u_id": 1, "face_label": "a"},
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


class _Connection:
    def __init__(self):
        self.enrolled = []
        self.label_lookups = []

    async def fetchrow(self, query, *args):
        assert 'FROM "user"' in query
        if "WHERE face_label = $1" in query:
            active_only = "user_active = TRUE" in query
            self.label_lookups.append((args[0], active_only))
            return next(({"u_id": user["u_id"]} for user in USERS.values()
                         if user["face_label"] == args[0] and (user.get("active", True) or not active_only)), None)
        assert "u_id=$1 AND user_active=TRUE" in query
        user = USERS.get(args[0])
        return user if user and user.get("active", True) else None

    async def execute(self, query, *args):
        assert "face_enrolled = TRUE" in query
        self.enrolled.append(args[0])


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


class _FakeFaces:
    """Answers match_enrollment_face from a script, one answer per photo."""

    def __init__(self, answers):
        self.lock = threading.RLock()
        self.answers = list(answers)
        self.checked = 0
        self.own_labels = []
        self.reloads = 0

    def match_enrollment_face(self, frame, own_label=None):
        self.checked += 1
        self.own_labels.append(own_label)
        return self.answers.pop(0)

    def reload_gallery(self):
        self.reloads += 1


def _found(label=None, box=(10, 20, 40, 50), distance=None, error=None):
    if label is not None and distance is None:
        distance = 0.12
    return {"box": list(box) if box else None, "label": label, "distance": distance, "error": error}


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


def _legacy(face_label):
    return asyncio.run(auth.register_photos(face_label=face_label, photos=_uploads()))


ALREADY_REGISTERED = {"error": "face_already_registered", "detail": api_face.FACE_ALREADY_REGISTERED}
WITHOUT_ACCOUNT = {"error": "face_registered_without_account", "detail": api_face.FACE_WITHOUT_ACCOUNT}
LABEL_TAKEN = {"error": "face_label_taken", "detail": api_face.FACE_LABEL_TAKEN}


@pytest.fixture
def gallery(monkeypatch, tmp_path):
    folder = tmp_path / "face_gallery"
    folder.mkdir()
    monkeypatch.setattr(config, "FACE_GALLERY_DIR", folder)
    monkeypatch.setattr(FaceRecognitionService, "_available", True)
    monkeypatch.setattr(api_face, "_enroll_lock", asyncio.Lock())
    conn = _Connection()
    monkeypatch.setattr(api_face, "get_pool", lambda: _Pool(conn))
    return types.SimpleNamespace(folder=folder, conn=conn)


def _use(monkeypatch, service):
    monkeypatch.setattr(FaceRecognitionService, "get_instance", classmethod(lambda cls: service))


def _files(folder):
    return {path.name: path.read_bytes() for path in folder.iterdir()}


def test_face_of_another_account_is_refused_without_naming_it(gallery, monkeypatch):
    (gallery.folder / "a-0.jpg").write_bytes(b"the other account")
    before = _files(gallery.folder)
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
    assert _files(gallery.folder) == before          # nothing written, not even a temp file
    assert service.reloads == 0 and gallery.conn.enrolled == []


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
    assert _files(gallery.folder) == {}
    assert service.reloads == 0 and gallery.conn.enrolled == []


def test_any_one_of_the_three_photos_refuses_all(gallery, monkeypatch):
    service = _FakeFaces([_found(), _found(), _found("a", distance=0.29)])
    _use(monkeypatch, service)

    status, body = _result(_enroll())

    assert (status, body) == (409, ALREADY_REGISTERED)
    assert service.checked == 3
    assert _files(gallery.folder) == {}
    assert service.reloads == 0 and gallery.conn.enrolled == []


def test_reenrolling_your_own_face_replaces_your_photos(gallery, monkeypatch):
    for i in range(3):
        (gallery.folder / f"pearl-{i}.jpg").write_bytes(b"old photo")
    (gallery.folder / "a-0.jpg").write_bytes(b"someone else")
    # The service leaves pearl's own photos out of the match, so it finds no other label.
    service = _FakeFaces([_found(), _found(), _found()])
    _use(monkeypatch, service)

    status, body = _result(_enroll())

    assert status == 200 and body["face_label"] == "pearl" and len(body["saved"]) == 3
    assert service.own_labels == ["pearl"] * 3
    files = _files(gallery.folder)
    assert sorted(files) == ["a-0.jpg", "pearl-0.jpg", "pearl-1.jpg", "pearl-2.jpg"]
    assert all(files[f"pearl-{i}.jpg"] != b"old photo" for i in range(3))
    assert files["a-0.jpg"] == b"someone else"
    assert service.reloads == 1 and gallery.conn.enrolled == [7]


def test_unknown_face_is_enrolled_as_the_detected_crop(gallery, monkeypatch):
    service = _FakeFaces([_found(box=(10, 20, 40, 50)), _found(box=(0, 0, 30, 60)), _found(box=(90, 100, 40, 50))])
    _use(monkeypatch, service)

    status, body = _result(_enroll())

    assert status == 200
    shapes = [cv2.imread(str(gallery.folder / f"pearl-{i}.jpg")).shape[:2] for i in range(3)]
    assert shapes == [(50, 40), (60, 30), (20, 10)]  # the last box runs off the 100x120 photo
    assert service.reloads == 1 and gallery.conn.enrolled == [7]


@pytest.mark.parametrize("answer, status, error", [
    (_found(box=None), 400, "No face detected in photo 1"),
    (_found(box=None, error="inference failed"), 503, "Face detection failed for photo 1"),
])
def test_photo_without_a_usable_face_saves_nothing(gallery, monkeypatch, answer, status, error):
    service = _FakeFaces([_found(), answer, _found()])
    _use(monkeypatch, service)

    got_status, body = _result(_enroll())

    assert (got_status, body) == (status, {"error": error})
    assert _files(gallery.folder) == {}
    assert service.reloads == 0 and gallery.conn.enrolled == []


def test_two_accounts_enrolling_one_face_at_once_cannot_both_pass(gallery, monkeypatch):
    class _OneFace(_FakeFaces):
        """Every photo is the same person: matched to any other label the loaded gallery holds."""

        def __init__(self):
            super().__init__([])
            self.loaded = []

        def match_enrollment_face(self, frame, own_label=None):
            self.checked += 1
            others = [label for label in self.loaded if label != own_label.lower()]
            return _found(others[0] if others else None)

        def reload_gallery(self):
            self.reloads += 1
            self.loaded = sorted({path.name.rsplit("-", 1)[0] for path in gallery.folder.glob("*.jpg")})

    service = _OneFace()
    _use(monkeypatch, service)

    async def both():
        return await asyncio.gather(api_face.enroll_face(photos=_uploads(), user={"u_id": 7}),
                                    api_face.enroll_face(photos=_uploads(), user={"u_id": 8}))

    results = dict(zip((7, 8), (_result(response) for response in asyncio.run(both()))))

    # Either may win (the uploads are read in threads); exactly one does.
    winners = [u_id for u_id, (status, _) in results.items() if status == 200]
    assert len(winners) == 1
    loser = 15 - winners[0]
    assert results[loser] == (409, ALREADY_REGISTERED)
    label = USERS[winners[0]]["face_label"]
    assert sorted(_files(gallery.folder)) == [f"{label}-{i}.jpg" for i in range(3)]
    assert gallery.conn.enrolled == winners


# --- The legacy route POST /auth/register-photos (no login, client-chosen label) -----------------------

def test_legacy_route_refuses_a_face_of_another_account(gallery, monkeypatch):
    service = _FakeFaces([_found(), _found("a")])
    _use(monkeypatch, service)

    status, body = _result(_legacy(" Newbie "))

    assert (status, body) == (409, ALREADY_REGISTERED)
    assert service.own_labels == ["newbie", "newbie"]
    assert _files(gallery.folder) == {} and service.reloads == 0


@pytest.mark.parametrize("existing_file, label", [
    ("pearl-0.jpg", "pearl"),   # someone's gallery photos
    ("kim.png", "kim"),         # FacesDatabase also reads .png and names without -<n>
    (None, "jane"),             # an account whose face is not enrolled yet
    (None, "gone"),             # a deactivated account: face_label is still UNIQUE
])
def test_legacy_route_never_writes_under_a_label_in_use(gallery, monkeypatch, existing_file, label):
    if existing_file:
        (gallery.folder / existing_file).write_bytes(b"someone's photo")
    before = _files(gallery.folder)
    service = _FakeFaces([_found()] * 3)
    _use(monkeypatch, service)

    status, body = _result(_legacy(label.upper()))

    assert (status, body) == (409, LABEL_TAKEN)
    assert _files(gallery.folder) == before
    assert service.checked == 0 and service.reloads == 0


def test_legacy_route_stores_a_new_face_as_crops_and_reloads(gallery, monkeypatch):
    service = _FakeFaces([_found(), _found(), _found()])
    _use(monkeypatch, service)

    status, body = _result(_legacy("newbie"))

    assert status == 200 and body["face_label"] == "newbie" and len(body["saved"]) == 3
    shapes = [cv2.imread(str(gallery.folder / f"newbie-{i}.jpg")).shape[:2] for i in range(3)]
    assert shapes == [(50, 40)] * 3
    # Login sees the face at once; face_enrolled is not touched (/auth/register creates the account after).
    assert service.reloads == 1 and gallery.conn.enrolled == []


# --- FaceRecognitionService.match_enrollment_face, with fake models -----------------------------------

class _Roi:
    def __init__(self, x, y, w, h):
        self.position = np.array([x, y], dtype=float)
        self.size = np.array([w, h], dtype=float)


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

    def start_async(self, frame, rois, landmarks):
        self.started.append(rois)

    def get_descriptors(self):
        return [] if self.descriptor is None else [self.descriptor]


def _service(monkeypatch, rois, face_id):
    monkeypatch.setattr(FaceRecognitionService, "_available", True)
    service = object.__new__(FaceRecognitionService)
    service.lock = threading.RLock()
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

    assert found == {"box": [100, 50, 200, 240], "label": "ab", "distance": pytest.approx(0.05), "error": None}
    assert service.lm_det.calls == [[first]] and face_id.started == [[first]]
    assert service.face_det.lock_held == [True]


def test_own_photos_are_left_out_even_when_closest(monkeypatch):
    """The 3 Oct gallery: the face is closest to "ab", yet within the threshold of "a" too."""
    identities = [("a", _vector(0.05)), ("ab", _vector(0.0))]

    as_ab = _service(monkeypatch, [_Roi(0, 0, 50, 60)], _Identifier(identities, _vector(0.0)))
    found = as_ab.match_enrollment_face(FRAME, "AB")   # the caller's label, compared without case
    assert found["label"] == "a" and found["distance"] == pytest.approx(0.05)

    as_a = _service(monkeypatch, [_Roi(0, 0, 50, 60)], _Identifier(identities, _vector(0.0)))
    found = as_a.match_enrollment_face(FRAME, "a")
    assert found["label"] == "ab" and found["distance"] == pytest.approx(0.0, abs=1e-9)


def test_unknown_face_own_label_only_and_empty_gallery_match_nobody(monkeypatch):
    unknown = _service(monkeypatch, [_Roi(1, 2, 30, 40)], _Identifier([("a", _vector(0.0))], _vector(0.42)))
    found = unknown.match_enrollment_face(FRAME, "pearl")
    assert found == {"box": [1, 2, 30, 40], "label": None, "distance": pytest.approx(0.42), "error": None}

    for identities in ([("pearl", _vector(0.0))], []):
        face_id = _Identifier(identities, _vector(0.0))
        service = _service(monkeypatch, [_Roi(1, 2, 30, 40)], face_id)
        assert service.match_enrollment_face(FRAME, "pearl") == {"box": [1, 2, 30, 40], "label": None,
                                                                 "distance": None, "error": None}
        assert face_id.started == []   # nobody else to compare with: no descriptor needed


def test_no_face_model_failure_and_missing_descriptor(monkeypatch):
    service = _service(monkeypatch, [], _Identifier([("a", _vector(0.0))], _vector(0.1)))
    assert service.match_enrollment_face(FRAME, "pearl") == {"box": None, "label": None, "distance": None,
                                                             "error": None}

    def broken(inputs):
        raise RuntimeError("inference failed")
    service.face_det.infer = broken
    assert service.match_enrollment_face(FRAME, "pearl")["error"] == "inference failed"

    # A face that could not be checked is never passed as unknown.
    no_descriptor = _service(monkeypatch, [_Roi(1, 2, 30, 40)], _Identifier([("a", _vector(0.0))], None))
    found = no_descriptor.match_enrollment_face(FRAME, "pearl")
    assert found["error"] and found["box"] is None and found["label"] is None


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
    service = _service(monkeypatch, [_Roi(0, 0, 50, 60)], face_id)

    found = service.match_enrollment_face(FRAME, "pearl")

    assert found["label"] == label
    assert found["distance"] == pytest.approx(distance)


def test_min_dist_picks_the_closest_label(monkeypatch, intel_matching):
    face_id = _real_face_id(intel_matching, [("a", [_vector(0.2)]), ("ab", [_vector(0.05)])], _vector(0.0))
    service = _service(monkeypatch, [_Roi(0, 0, 50, 60)], face_id)

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
        service = _service(monkeypatch, [_Roi(0, 0, 50, 60)], face_id)

        found = service.match_enrollment_face(FRAME, "pearl")
        results, _ = face_id.postprocess()   # what identify_frame (login) computes
        login = face_id.get_identity_label(results[0].id)

        assert found["label"] == (None if login == face_id.UNKNOWN_ID_LABEL else login)
        assert found["distance"] == pytest.approx(results[0].distance)
        outcomes.add(found["label"] is None)
    assert outcomes == {True, False}   # both matches and unknown faces were exercised


def test_endpoint_refuses_the_incident_with_real_matching(gallery, monkeypatch, intel_matching):
    """A's photos are in the gallery; the same face enrolling for a new account is refused."""
    face_id = _real_face_id(intel_matching, [("a", [_vector(0.0)])], _vector(0.05))
    service = _service(monkeypatch, [_Roi(10, 20, 40, 50)], face_id)
    service.reload_gallery = lambda: None
    _use(monkeypatch, service)

    assert _result(_enroll(u_id=8)) == (409, ALREADY_REGISTERED)
    assert _files(gallery.folder) == {}

    # The owner re-enrolling their own label is still allowed.
    status, body = _result(_enroll(u_id=1))
    assert status == 200 and body["face_label"] == "a"
    assert gallery.conn.enrolled == [1]


def test_endpoint_refuses_a_duplicate_the_caller_is_closer_to(gallery, monkeypatch, intel_matching):
    """The 3 Oct gallery held the face as "a" and "ab". "ab" re-enrolling must not pass because its own old
    photos are closer: login takes the face for either, and the duplicate would stay. Neither account can
    enroll it until the duplicate is cleaned up by hand (as was done on 3 Oct)."""
    face_id = _real_face_id(intel_matching, [("a", [_vector(0.05)]), ("ab", [_vector(0.0)])], _vector(0.0))
    service = _service(monkeypatch, [_Roi(10, 20, 40, 50)], face_id)
    service.reload_gallery = lambda: None
    _use(monkeypatch, service)

    assert _result(_enroll(u_id=34)) == (409, ALREADY_REGISTERED)
    assert _result(_enroll(u_id=1)) == (409, ALREADY_REGISTERED)
    assert _files(gallery.folder) == {} and gallery.conn.enrolled == []
