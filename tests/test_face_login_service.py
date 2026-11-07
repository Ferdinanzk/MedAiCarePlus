import logging
import threading

import numpy as np
import pytest

from app.services import face_recognition_service as frs
from app.services.face_login import FaceBox
from app.services.face_recognition_service import FaceRecognitionService

FRAME = np.zeros((240, 320, 3), dtype=np.uint8)
BIG = FaceBox(100, 50, 100, 100, 0.9)
SMALL = FaceBox(10, 10, 40, 40, 0.9)


class FakeLiveness:
    def __init__(self, score):
        self._score = score
        self.boxes = []

    def score(self, frame, box):
        self.boxes.append(box)
        return self._score


def set_liveness(monkeypatch, score):
    fake = FakeLiveness(score)
    monkeypatch.setattr(frs.LivenessService, "get_instance", classmethod(lambda cls: fake))
    return fake


@pytest.fixture
def svc(monkeypatch):
    monkeypatch.setattr(FaceRecognitionService, "_available", True)
    service = object.__new__(FaceRecognitionService)
    service.lock = threading.RLock()
    service.matched = []
    service.boxes = [BIG]
    service.result = ("ferdi", 0.1)
    monkeypatch.setattr(FaceRecognitionService, "detect", lambda self, f: self.boxes)

    def match_box(self, f, box):
        self.matched.append(box)
        return self.result
    monkeypatch.setattr(FaceRecognitionService, "match_box", match_box)
    set_liveness(monkeypatch, 0.9)
    return service


def test_only_the_main_face_is_matched(svc):
    svc.boxes = [SMALL, BIG]
    svc.result = (None, 0.8)
    verdict = svc.judge_frame(FRAME)
    assert verdict.code == "unknown"
    assert svc.matched == [BIG]


def test_edge_face_is_still_judged(svc):
    edge = FaceBox(-20, 150, 120, 120, 0.9)   # partly outside the 320x240 frame
    svc.boxes = [edge]
    verdict = svc.judge_frame(FRAME)
    assert verdict.code == "match"
    assert svc.matched == [edge]


def test_empty_gallery_gives_unknown(svc):
    svc.result = (None, None)
    verdict = svc.judge_frame(FRAME)
    assert verdict.code == "unknown" and verdict.label is None and verdict.distance is None
    assert not svc.login([FRAME] * 5).identified


def test_come_closer_and_two_faces_are_not_matched(svc, monkeypatch):
    live = set_liveness(monkeypatch, 0.9)
    svc.boxes = [FaceBox(0, 0, 20, 20, 0.9)]
    assert svc.judge_frame(FRAME).code == "come_closer"
    svc.boxes = [BIG, FaceBox(0, 0, 95, 95, 0.9)]
    assert svc.judge_frame(FRAME).code == "two_faces"
    assert svc.matched == []
    assert len(live.boxes) == 2


def test_liveness_error_by_mode(svc, monkeypatch):
    set_liveness(monkeypatch, None)
    monkeypatch.setattr(frs, "FACE_LIVENESS_MODE", "block")
    assert svc.login([FRAME] * 5).reason == "not_live"
    monkeypatch.setattr(frs, "FACE_LIVENESS_MODE", "log")
    decision = svc.login([FRAME] * 5)
    assert decision.identified and decision.reason == "match"


def test_login_logs_one_line_without_names(svc, caplog):
    with caplog.at_level(logging.INFO, logger=frs.log.name):
        svc.login([FRAME] * 5)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("face login:")]
    assert lines == ["face login: reason=match frames=5 matches=5 liveness=[0.9, 0.9, 0.9, 0.9, 0.9] mode=log"]
    assert "ferdi" not in caplog.text


def test_model_unavailable_gives_no_face_and_error(svc, monkeypatch):
    monkeypatch.setattr(FaceRecognitionService, "_available", False)
    assert svc.judge_frame(FRAME).code == "no_face"
    decision = svc.login([FRAME] * 5)
    assert not decision.identified and decision.reason == "no_face"



def test_a_frame_that_is_not_an_image_is_no_face(svc):
    assert svc.judge_frame(None).code == "no_face"
    decision = svc.login([FRAME, FRAME, None, FRAME, FRAME])
    assert decision.identified and decision.frames == 5
    decision = svc.login([FRAME, FRAME, None, None, None])
    assert not decision.identified and decision.reason == "no_face"


# --- match_box itself, with fake face_id / lm_det / gallery (no models) ---

class FakeResult:
    def __init__(self, output):
        self.confidence = output[2]
        self.position = np.array((output[3], output[4]))
        self.size = np.array((output[5], output[6]))


class FakeGallery:
    def __init__(self, labels, distance):
        self.labels = labels
        self.distance = distance

    def __len__(self):
        return len(self.labels)

    def __getitem__(self, i):
        return type("Identity", (), {"label": self.labels[i]})()

    def match_faces(self, descriptors, algo):
        return [(0, self.distance)]


class FakeFaceId:
    match_threshold = 0.3
    match_algo = "MIN_DIST"

    def __init__(self, gallery, descriptors):
        self.faces_database = gallery
        self.descriptors = descriptors

    def clear(self):
        pass

    def start_async(self, frame, rois, landmarks):
        pass

    def get_descriptors(self):
        return self.descriptors


class FakeLandmarks:
    def __init__(self):
        self.rois = None

    def infer(self, args):
        self.rois = args[1]
        return ["lm"]


def real_match_service(monkeypatch, gallery, descriptors=("d",)):
    monkeypatch.setattr(frs, "FaceDetector", type("FD", (), {"Result": FakeResult}))
    service = object.__new__(FaceRecognitionService)
    service.lock = threading.RLock()
    service.face_id = FakeFaceId(gallery, list(descriptors))
    service.lm_det = FakeLandmarks()
    return service


def test_match_box_gives_landmarks_the_box_as_a_roi(monkeypatch):
    service = real_match_service(monkeypatch, FakeGallery(["ferdi"], 0.1))
    assert service.match_box(FRAME, BIG) == ("ferdi", 0.1)
    (roi,) = service.lm_det.rois
    assert list(roi.position) == [100, 50] and list(roi.size) == [100, 100]
    assert roi.confidence == 0.9


def test_match_box_threshold_boundary(monkeypatch):
    at = real_match_service(monkeypatch, FakeGallery(["ferdi"], 0.3))
    assert at.match_box(FRAME, BIG) == ("ferdi", 0.3)
    above = real_match_service(monkeypatch, FakeGallery(["ferdi"], 0.3001))
    assert above.match_box(FRAME, BIG) == (None, 0.3001)


def test_match_box_empty_descriptors_and_empty_gallery(monkeypatch):
    none_computed = real_match_service(monkeypatch, FakeGallery(["ferdi"], 0.1), descriptors=())
    assert none_computed.match_box(FRAME, BIG) == (None, None)
    empty = real_match_service(monkeypatch, FakeGallery([], 0.1))
    assert empty.match_box(FRAME, BIG) == (None, None)
    assert empty.lm_det.rois is None   # returned before any model ran
