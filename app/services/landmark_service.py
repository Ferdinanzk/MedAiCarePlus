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

from app.config import (DEPTH_EVERY, DEPTH_INPUT_SIZE, DEPTH_MODEL_PATH, LANDMARK_MODEL_DIR, OBJECT_EVERY,
                        OBJECT_MODEL_PATH, OBJECT_SCORE)
from app.services.landmarks.depth import DepthModel
from app.services.landmarks.objects import ObjectDetector
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
# Hand depth against the face (services/landmarks/depth.py): onnx-community/depth-anything-v2-small, the fp32
# onnx/model.onnx (Apache-2.0), 99,060,839 B. Optional: without the file the engine runs as before and no packet
# carries hand_depth. models/depth/README.md says where it comes from.
DEPTH_MODEL_SHA256 = "afb6a5c28f3b6bf1618c6e43f02073ef9dfdc70e937502d51603e57b0a1df10c"
# What the hand holds (services/landmarks/objects.py): Megvii YOLOX 0.1.1rc0 (Apache-2.0), COCO. Optional too;
# either file is accepted. models/objects/README.md says where they come from.
OBJECT_MODEL_SHA256 = {
    "yolox_s.onnx": "c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063",
    "yolox_tiny.onnx": "427cc366d34e27ff7a03e2899b5e3671425c262ea2291f88bb942bc1cc70b0f7",
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

    def __init__(self, models_dir: Path = LANDMARK_MODEL_DIR, depth_path: Path | None = DEPTH_MODEL_PATH,
                 objects_path: Path | None = OBJECT_MODEL_PATH):
        self.error = None
        self.models = None
        self.depth = None          # DepthModel, when its file is present and intact
        self.depth_error = None
        self.objects = None        # ObjectDetector, the same
        self.objects_error = None
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
        try:
            depth_path = None if depth_path is None else Path(depth_path)
            if depth_path is None or not depth_path.is_file():
                self.depth_error = f"Depth model missing at {depth_path}"
                log.info("hand depth off: %s", self.depth_error)
            elif _sha256(depth_path) != DEPTH_MODEL_SHA256:
                raise ValueError(f"Depth model {depth_path.name} failed its integrity check")
            elif self.models is not None:
                self.depth = DepthModel(depth_path, long_side=DEPTH_INPUT_SIZE, threads=4)
                log.info("hand depth on: %s at %d px, every %d frames", depth_path.name, DEPTH_INPUT_SIZE, DEPTH_EVERY)
        except Exception as exc:
            log.exception("depth model unavailable")
            self.depth_error = str(exc)
        try:
            objects_path = None if objects_path is None else Path(objects_path)
            if objects_path is None or not objects_path.is_file():
                self.objects_error = f"Object model missing at {objects_path}"
                log.info("hand objects off: %s", self.objects_error)
            elif _sha256(objects_path) != OBJECT_MODEL_SHA256.get(objects_path.name):
                raise ValueError(f"Object model {objects_path.name} failed its integrity check")
            elif self.models is not None:
                self.objects = ObjectDetector(objects_path, threads=4, score=OBJECT_SCORE)
                log.info("hand objects on: %s at %d px, score %.2f, every %d frames", objects_path.name,
                         self.objects.size, OBJECT_SCORE, OBJECT_EVERY)
        except Exception as exc:
            log.exception("object model unavailable")
            self.objects_error = str(exc)

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def new_engine(self) -> VisionEngine:
        if not LandmarkService._available:
            raise RuntimeError(self.error or "Landmark models unavailable")
        return VisionEngine(self.models, self.executor, depth=self.depth, depth_every=DEPTH_EVERY,
                            objects=self.objects, objects_every=OBJECT_EVERY)
