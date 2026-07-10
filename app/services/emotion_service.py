"""Live facial-emotion inference using the local SigLIP2 checkpoint."""
from __future__ import annotations

import math

import cv2
import numpy as np
import torch
from PIL import Image
from transformers import AutoImageProcessor, AutoModelForImageClassification

from app.config import EMOTION_HF_MODEL_PATH
from app.services.emotion_labels import APP_EMOTION_LABELS, normalize_emotion_label

_ROTATION_ALPHA = 0.2
_FACE_CROP_PADDING_RATIO = 0.18
_MIN_FACE_BOX_SIZE = 48


def normalize_prediction_logits(logits: torch.Tensor, id2label: dict[int, str]) -> tuple[str, float, dict[str, float]]:
    """Collapse raw model classes into canonical app probabilities.

    Multiple raw classes may map to one product label, so probability mass is
    summed after softmax. Unsupported labels are discarded rather than leaked.
    """
    if logits.ndim != 2 or logits.shape[0] != 1:
        raise ValueError("Expected one row of classification logits")
    raw_probs = torch.softmax(logits, dim=-1).squeeze(0).detach().cpu()
    probabilities = {label: 0.0 for label in sorted(APP_EMOTION_LABELS)}
    for index, raw_label in id2label.items():
        app_label = normalize_emotion_label(raw_label)
        if app_label is not None:
            probabilities[app_label] += float(raw_probs[int(index)])
    supported = {label: value for label, value in probabilities.items() if value > 0.0}
    if not supported:
        raise ValueError("SigLIP checkpoint has no supported emotion labels")
    predicted = max(supported, key=supported.get)
    return predicted, supported[predicted], probabilities


class EmotionService:
    """Singleton wrapping local SigLIP2 inference and MediaPipe face mesh."""

    _instance: "EmotionService | None" = None
    _available: bool = False

    def __init__(self, model_path=None):
        self.model_path = model_path or EMOTION_HF_MODEL_PATH
        try:
            import mediapipe as mp

            self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
            self.processor = AutoImageProcessor.from_pretrained(
                self.model_path, local_files_only=True, use_fast=False,
            )
            self.model = AutoModelForImageClassification.from_pretrained(
                self.model_path, local_files_only=True,
            ).to(self.device).eval()
            raw_id2label = self.model.config.id2label
            self.id2label = {int(key): value for key, value in raw_id2label.items()}
            # Validate the checkpoint vocabulary at startup without exposing it.
            if not any(normalize_emotion_label(value) for value in self.id2label.values()):
                raise ValueError("Checkpoint does not contain supported emotion labels")

            self.input_size = self.processor.size
            mp_fm = mp.solutions.face_mesh
            self.face_mesh = mp_fm.FaceMesh(
                max_num_faces=1, refine_landmarks=True,
                min_detection_confidence=0.7, min_tracking_confidence=0.5,
            )
            self._last_angle = 0.0
            EmotionService._available = True
            print(f"[Emotion] Loaded local SigLIP2 on {self.device} — labels: {sorted(APP_EMOTION_LABELS)}")
        except Exception as exc:
            EmotionService._available = False
            print(f"[Emotion] SigLIP2 not available: {exc}")

    @classmethod
    def get_instance(cls) -> "EmotionService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def reset_session(self):
        self._last_angle = 0.0

    def _extract_face(self, frame_bgr: np.ndarray):
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        result = self.face_mesh.process(rgb)
        if not result.multi_face_landmarks:
            return None, {"error": None}

        height, width = frame_bgr.shape[:2]
        landmarks = result.multi_face_landmarks[0].landmark
        xs = [int(point.x * width) for point in landmarks]
        ys = [int(point.y * height) for point in landmarks]
        raw_x1, raw_y1 = max(0, min(xs)), max(0, min(ys))
        raw_x2, raw_y2 = min(width, max(xs)), min(height, max(ys))
        box_w, box_h = raw_x2 - raw_x1, raw_y2 - raw_y1
        if box_w < _MIN_FACE_BOX_SIZE or box_h < _MIN_FACE_BOX_SIZE:
            return None, {"error": "Face crop too small"}

        pad = int(_FACE_CROP_PADDING_RATIO * max(box_w, box_h))
        x1, y1 = max(0, raw_x1 - pad), max(0, raw_y1 - pad)
        x2, y2 = min(width, raw_x2 + pad), min(height, raw_y2 + pad)
        face = frame_bgr[y1:y2, x1:x2]
        if face.size == 0:
            return None, {"error": None}

        right_eye = (int(landmarks[33].x * width) - x1, int(landmarks[33].y * height) - y1)
        left_eye = (int(landmarks[263].x * width) - x1, int(landmarks[263].y * height) - y1)
        dx, dy = left_eye[0] - right_eye[0], left_eye[1] - right_eye[1]
        raw_angle = None
        smooth_angle = None
        if dx > max(10, 0.12 * face.shape[1]):
            raw_angle = math.degrees(math.atan2(dy, dx))
            if abs(raw_angle) <= 30.0:
                smooth_angle = self._last_angle * (1.0 - _ROTATION_ALPHA) + raw_angle * _ROTATION_ALPHA
                if abs(smooth_angle) >= 2.0:
                    center = (face.shape[1] // 2, face.shape[0] // 2)
                    matrix = cv2.getRotationMatrix2D(center, -smooth_angle, 1.0)
                    face = cv2.warpAffine(face, matrix, (face.shape[1], face.shape[0]),
                                          flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
                    self._last_angle = smooth_angle
        return face, {
            "_raw_angle": raw_angle, "_smooth_angle": smooth_angle,
            "_last_angle": float(self._last_angle), "_face_wh": (face.shape[1], face.shape[0]),
            "_face_box_wh": (box_w, box_h), "_crop_xy": (x1, y1, x2, y2), "error": None,
        }

    def predict_frame_debug(self, frame_bgr: np.ndarray) -> dict:
        if not EmotionService._available:
            return {"detected": False, "emotion_type": None, "emotion_score": None,
                    "probabilities": {}, "error": "SigLIP2 model not loaded"}
        try:
            face, debug = self._extract_face(frame_bgr)
            if face is None:
                return {"detected": False, "emotion_type": None, "emotion_score": None,
                        "probabilities": {}, "error": debug.get("error")}
            face_rgb = cv2.cvtColor(face, cv2.COLOR_BGR2RGB)
            inputs = self.processor(images=Image.fromarray(face_rgb), return_tensors="pt")
            inputs = {key: value.to(self.device) for key, value in inputs.items()}
            with torch.no_grad():
                logits = self.model(**inputs).logits
            label, confidence, probabilities = normalize_prediction_logits(logits, self.id2label)
            return {"detected": True, "emotion_type": label, "emotion_score": confidence,
                    "probabilities": probabilities, "error": None,
                    "_model": "siglip2", "_input_wh": self.input_size, **debug}
        except Exception as exc:
            return {"detected": False, "emotion_type": None, "emotion_score": None,
                    "probabilities": {}, "error": f"SigLIP2 inference failed: {exc}"}

    def predict_frame(self, frame_bgr: np.ndarray) -> dict:
        result = self.predict_frame_debug(frame_bgr)
        for key in tuple(result):
            if key.startswith("_"):
                result.pop(key, None)
        return result
