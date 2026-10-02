"""Landmarks for camera frames the Reachy streams to the server (POST /api/device/monitor/frame).

The robot's Raspberry Pi can't run the face/hand/pose models fast enough for the 12 fps recording gate, so it
sends JPEGs and the server runs the same ONNX models here. Models are pinned by the same SHA-256 the robot app
uses (medcare_reachy/models.py).
"""

import hashlib
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.config import LANDMARK_MODEL_DIR
from app.services.landmarks.engine import VisionEngine, load_models

log = logging.getLogger(__name__)

MODEL_SHA256 = {
    "face_detector.onnx": "d97a63b82d1751e6dd219b842f55acc28c9656865611caeb88d7bc48f49919c4",
    "face_landmarks_detector.onnx": "1d97e6326845df6c239b5c2662c9bec62eb761839def949e1aa0fccb8ed1b47a",
    "hand_detector.onnx": "61c78d31e5c6c37a0ebde4c2f382db50d5116bfae0bd4be629f8a7d2aafc0f40",
    "hand_landmarks_detector.onnx": "5ed334609f59b97a43eed7d993ce305bb49134dc4c8918ecb7471099fc51071a",
    "pose_detector.onnx": "47fd5599d6fa17608f03e0eb0ae230baa6e597d7e8a2c8199fe00abea55a701f",
    "pose_landmarks_detector.onnx": "cd7c2b67e1275239b80b17eab9cd861422ef6ac291370baf512c3022409a248d",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


class LandmarkService:
    _instance = None
    _available = False

    def __init__(self, models_dir: Path = LANDMARK_MODEL_DIR):
        self.error = None
        self.models = None
        # Three trackers per frame; a few sessions may overlap briefly when one replaces another.
        self.executor = ThreadPoolExecutor(max_workers=min(12, os.cpu_count() or 4), thread_name_prefix="landmarks")
        try:
            models_dir = Path(models_dir)
            for name, digest in MODEL_SHA256.items():
                path = models_dir / name
                if not path.is_file():
                    raise FileNotFoundError(f"Landmark model {name} is missing in {models_dir}")
                if _sha256(path) != digest:
                    raise ValueError(f"Landmark model {name} failed its integrity check")
            self.models = load_models(models_dir, threads=2)
            LandmarkService._available = True
        except Exception as exc:
            log.exception("landmark models unavailable")
            self.error = str(exc)
            LandmarkService._available = False

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def new_engine(self) -> VisionEngine:
        if not LandmarkService._available:
            raise RuntimeError(self.error or "Landmark models unavailable")
        return VisionEngine(self.models, self.executor)
