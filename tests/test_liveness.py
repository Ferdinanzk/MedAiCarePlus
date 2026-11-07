"""Passive liveness (services/liveness_service.py): MiniFASNetV2 of Silent-Face-Anti-Spoofing on onnxruntime.

Synthetic frames only: never a face image in the repo.
"""
import hashlib
import logging
import re

import cv2
import numpy as np
import pytest

from app import config
from app.services.face_login import FaceBox
from app.services.liveness_service import MODEL_SHA256, LivenessService, crop_box, preprocess

SOURCE_MD = config.FACE_LIVENESS_MODEL.parent / "SOURCE.md"


def synthetic_face_frame() -> np.ndarray:
    """A 640x480 BGR frame: grey background, a skin-coloured oval with two eyes and a mouth in the middle."""
    frame = np.full((480, 640, 3), 120, np.uint8)
    cv2.ellipse(frame, (320, 240), (70, 90), 0, 0, 360, (140, 170, 210), -1)
    for x in (295, 345):
        cv2.circle(frame, (x, 220), 8, (40, 40, 40), -1)
    cv2.ellipse(frame, (320, 285), (25, 8), 0, 0, 180, (60, 60, 150), -1)
    return frame


def test_missing_model_is_unavailable_and_scores_none(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(config, "FACE_LIVENESS_MODEL", tmp_path / "absent.onnx")
    monkeypatch.setattr(LivenessService, "_instance", None)
    service = LivenessService.get_instance()
    assert service.available is False
    assert LivenessService.get_instance() is service
    box = FaceBox(260, 150, 120, 180, 0.9)
    with caplog.at_level(logging.WARNING, logger="app.services.liveness_service"):
        assert service.score(synthetic_face_frame(), box) is None
        assert service.score(synthetic_face_frame(), box) is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1   # one WARNING per failure kind, not one per frame
    assert "model_missing" in warnings[0].getMessage()


def test_wrong_file_fails_its_integrity_check(tmp_path):
    fake = tmp_path / "MiniFASNetV2.onnx"
    fake.write_bytes(b"not a model")
    service = LivenessService(fake)
    assert service.available is False
    assert service.score(synthetic_face_frame(), FaceBox(260, 150, 120, 180, 0.9)) is None


def test_sha256_matches_source_md():
    path = config.FACE_LIVENESS_MODEL
    if not path.is_file():
        pytest.skip("the liveness model file is absent")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    pinned = re.findall(r"\b[0-9a-f]{64}\b", SOURCE_MD.read_text(encoding="utf-8"))
    assert digest in pinned
    assert digest == MODEL_SHA256


def test_score_is_a_probability_on_a_synthetic_face():
    service = LivenessService.get_instance()
    assert service.available is True
    score = service.score(synthetic_face_frame(), FaceBox(250, 150, 140, 180, 0.95))
    assert isinstance(score, float)
    assert 0.0 <= score <= 1.0


def test_tiny_box_at_the_edge_gives_a_number_or_none_never_raises():
    service = LivenessService.get_instance()
    frame = synthetic_face_frame()
    for box in (FaceBox(638.5, 478.5, 1.5, 1.5, 0.6), FaceBox(-40, -30, 45, 35, 0.6), FaceBox(0, 0, 0, 0, 0.6),
                FaceBox(700, 500, 20, 20, 0.6), FaceBox(float("nan"), 10, 20, 20, 0.6),
                FaceBox(100, 100, -5, 20, 0.6)):
        score = service.score(frame, box)
        assert score is None or 0.0 <= score <= 1.0
    assert service.score(np.zeros((10, 10), np.uint8), FaceBox(2, 2, 5, 5, 0.6)) is None   # not a BGR frame


def test_crop_box_matches_the_original_get_new_box():
    """Expected values from Silent-Face-Anti-Spoofing src/generate_patches.py CropImage._get_new_box (commit b6d5f04)
    at scale 2.7: the box scaled around its centre, shifted (not shrunk) back inside the frame."""
    cases = {(260, 180, 120, 120): (158, 78, 482, 402), (-30, 200, 100, 100): (0, 115, 270, 385),
             (600, 440, 60, 60): (477, 317, 639, 479), (100, 0, 400, 470): (96, 0, 503, 479),
             (0, 0, 2, 2): (0, 0, 5, 5), (638.5, 478.5, 1.5, 1.5): (634, 474, 639, 479)}
    for (x, y, w, h), expected in cases.items():
        assert crop_box(640, 480, FaceBox(x, y, w, h, 0.9)) == expected


def test_preprocess_keeps_bgr_order_and_raw_0_255_values():
    frame = np.zeros((480, 640, 3), np.uint8)
    frame[:, :] = (255, 0, 10)   # B, G, R
    tensor = preprocess(frame, FaceBox(260, 180, 120, 120, 0.9))
    assert tensor.shape == (1, 3, 80, 80)
    assert tensor.dtype == np.float32
    assert np.all(tensor[0, 0] == 255.0) and np.all(tensor[0, 1] == 0.0) and np.all(tensor[0, 2] == 10.0)


def test_startup_line_names_the_mode(tmp_path, monkeypatch):
    from app import startup_report
    monkeypatch.setattr(config, "FACE_LIVENESS_MODE", "log")
    ok, detail = startup_report.liveness(LivenessService.get_instance())
    assert ok is True and "MiniFASNetV2.onnx loaded" in detail and "mode log" in detail
    ok, detail = startup_report.liveness(LivenessService(tmp_path / "absent.onnx"))
    assert ok is False and "model_missing" in detail and "mode log" in detail


def test_health_reports_liveness(monkeypatch):
    import asyncio
    from app import main
    monkeypatch.setattr(LivenessService, "_instance", None)
    assert asyncio.run(main.health())["liveness"] is True
