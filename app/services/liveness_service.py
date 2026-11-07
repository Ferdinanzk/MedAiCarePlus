"""Passive liveness for face login: is the main face in a frame a real face, or a photo on a phone or paper?

MiniFASNetV2 of minivision-ai's Silent-Face-Anti-Spoofing (Apache-2.0) as ONNX, pinned by SHA-256; models/liveness/
SOURCE.md has where it came from and the preprocessing, taken from the source code (src/generate_patches.py,
src/anti_spoof_predict.py, src/data_io/functional.py, test.py at commit b6d5f04):

- the crop: the face box scaled 2.7 times around its centre (less when the frame is too small), shifted back inside
  the frame without shrinking (`crop_box`, the source's `CropImage._get_new_box`), resized to 80x80 with cv2.resize;
- the pixels: BGR as OpenCV reads them, float32 0-255, no mean, no scaling, NCHW;
- the output: 3 logits, softmax; index 1 is "real".

`score()` returns the real-class probability, or None when the model is missing or a frame cannot be scored, with
one WARNING per failure kind for the process (never image data). One onnxruntime session on CPU per process, made on
first use (or by `warm_up()` in the lifespan).
"""

import hashlib
import logging
import math
import threading
from pathlib import Path

import cv2
import numpy as np

from app import config
from app.services.face_login import FaceBox

log = logging.getLogger(__name__)

MODEL_SHA256 = "b32929adc2d9c34b9486f8c4c7bc97c1b69bc0ea9befefc380e4faae4e463907"
CROP_SCALE = 2.7
INPUT_SIZE = 80
REAL_INDEX = 1


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def crop_box(frame_w: int, frame_h: int, box: FaceBox, scale: float = CROP_SCALE) -> tuple[int, int, int, int]:
    """The source's CropImage._get_new_box: (x1, y1, x2, y2), both corners inclusive. A box past the frame's edge
    (FaceBox allows it) gives a crop shifted inside; the last clamp only guards a box wholly outside the frame."""
    scale = min((frame_h - 1) / box.h, (frame_w - 1) / box.w, scale)
    new_w, new_h = box.w * scale, box.h * scale
    cx, cy = box.x + box.w / 2, box.y + box.h / 2
    x1, y1, x2, y2 = cx - new_w / 2, cy - new_h / 2, cx + new_w / 2, cy + new_h / 2
    if x1 < 0:
        x2 -= x1
        x1 = 0
    if y1 < 0:
        y2 -= y1
        y1 = 0
    if x2 > frame_w - 1:
        x1 -= x2 - frame_w + 1
        x2 = frame_w - 1
    if y2 > frame_h - 1:
        y1 -= y2 - frame_h + 1
        y2 = frame_h - 1
    x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
    return (min(max(x1, 0), frame_w - 1), min(max(y1, 0), frame_h - 1),
            min(max(x2, 0), frame_w - 1), min(max(y2, 0), frame_h - 1))


def preprocess(frame_bgr: np.ndarray, box: FaceBox) -> np.ndarray:
    """The model's input for one face: (1, 3, 80, 80) float32, BGR, 0-255."""
    frame_h, frame_w = frame_bgr.shape[:2]
    x1, y1, x2, y2 = crop_box(frame_w, frame_h, box)
    face = cv2.resize(frame_bgr[y1:y2 + 1, x1:x2 + 1], (INPUT_SIZE, INPUT_SIZE))
    return face.astype(np.float32).transpose(2, 0, 1)[np.newaxis]


class LivenessService:
    _instance = None

    def __init__(self, model_path: Path | None = None):
        self.model_path = Path(model_path if model_path is not None else config.FACE_LIVENESS_MODEL)
        self._session = None
        self._lock = threading.Lock()
        self._warned: set[str] = set()
        self._problem = None   # the failure kind that makes the model unusable, or None
        self._load_error = ""
        if not self.model_path.is_file():
            self._problem = "model_missing"
        elif _sha256(self.model_path) != MODEL_SHA256:
            self._problem = "model_integrity"

    @classmethod
    def get_instance(cls) -> "LivenessService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def available(self) -> bool:
        return self._problem is None

    @property
    def problem(self) -> str | None:
        """Why the model is unusable (model_missing, model_integrity, model_load), or None."""
        return self._problem

    def _warn(self, kind: str, detail: str) -> None:
        if kind not in self._warned:
            self._warned.add(kind)
            log.warning("liveness %s: %s (further ones are not logged)", kind, detail)

    def _get_session(self):
        if self._problem is not None:
            return None
        with self._lock:
            if self._session is None and self._problem is None:
                try:
                    import onnxruntime as ort
                    options = ort.SessionOptions()
                    options.intra_op_num_threads = 1
                    self._session = ort.InferenceSession(str(self.model_path), options,
                                                         providers=["CPUExecutionProvider"])
                except Exception as error:
                    self._problem = "model_load"
                    self._load_error = f"{type(error).__name__}: {error}"
            return self._session

    def warm_up(self) -> bool:
        """Load the session now (the lifespan), so the first login does not wait for it."""
        self._get_session()
        if self._problem is not None:
            self._warn_problem()
        return self.available

    def _warn_problem(self) -> None:
        details = {"model_missing": f"no model file at {self.model_path}",
                   "model_integrity": f"{self.model_path.name} failed its SHA-256 check",
                   "model_load": f"onnxruntime could not load {self.model_path.name}: "
                                 f"{self._load_error}"}
        self._warn(self._problem, details.get(self._problem, self._problem) + "; liveness scores are None")

    def score(self, frame_bgr: np.ndarray, box: FaceBox) -> float | None:
        """The probability (0..1) that the face in `box` is real, or None."""
        session = self._get_session()
        if session is None:
            self._warn_problem()
            return None
        if not (isinstance(frame_bgr, np.ndarray) and frame_bgr.ndim == 3 and frame_bgr.shape[2] == 3
                and frame_bgr.shape[0] >= 2 and frame_bgr.shape[1] >= 2):
            self._warn("bad_frame", "the frame is not a BGR image")
            return None
        if not all(math.isfinite(v) for v in (box.x, box.y, box.w, box.h)) or box.w <= 0 or box.h <= 0:
            self._warn("bad_box", "the face box has no area or is not a number")
            return None
        try:
            tensor = preprocess(frame_bgr, box)
            logits = session.run(None, {session.get_inputs()[0].name: tensor})[0][0].astype(np.float64)
            probs = np.exp(logits - logits.max())
            probs /= probs.sum()
            real = float(probs[REAL_INDEX])
            if not math.isfinite(real):
                raise ValueError("the model gave a non-finite score")
            return real
        except Exception as error:
            self._warn("inference_failed", type(error).__name__)
            return None
