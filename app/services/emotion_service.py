"""Seed 43 seven-expression ONNX inference with the original export transform."""

import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from app.config import EMOTION_MODEL_PATH

LABELS = ("angry", "disgust", "fear", "happy", "sad", "surprise", "neutral")
MEAN = np.array((0.485, 0.456, 0.406), dtype=np.float32)[:, None, None]
STD = np.array((0.229, 0.224, 0.225), dtype=np.float32)[:, None, None]


def prepare_face(face_bgr: np.ndarray, size: int = 112) -> np.ndarray:
    gray = cv2.cvtColor(face_bgr, cv2.COLOR_BGR2GRAY)
    image = Image.fromarray(gray).convert("RGB").resize((size, size), resample=Image.Resampling.BILINEAR)
    values = np.asarray(image, dtype=np.float32).transpose(2, 0, 1) / np.float32(255)
    return ((values - MEAN) / STD)[None, ...]


def crop_face(frame_bgr: np.ndarray, box: tuple[int, int, int, int], padding: float = .10) -> np.ndarray:
    x, y, width, height = box
    h, w = frame_bgr.shape[:2]
    left, top = max(0, int(x - width * padding)), max(0, int(y - height * padding))
    right, bottom = min(w, int(x + width * (1 + padding))), min(h, int(y + height * (1 + padding)))
    face = frame_bgr[top:bottom, left:right]
    if face.size == 0:
        raise ValueError("Face crop is empty")
    return face


class EmotionService:
    _instance = None
    _available = False

    def __init__(self):
        self.error = None
        try:
            import onnxruntime as ort
            model = Path(EMOTION_MODEL_PATH)
            metadata = json.loads(model.with_name(model.stem + "_metadata.json").read_text(encoding="utf-8"))
            if tuple(metadata["labels"]) != LABELS or metadata["preprocess_version"] != "ferplus-gray-rgb-imagenet-v1":
                raise ValueError("Unexpected seed 43 model metadata")
            self.size = int(metadata["input_size"])
            options = ort.SessionOptions()
            options.intra_op_num_threads = 1
            options.inter_op_num_threads = 1
            self.session = ort.InferenceSession(str(model), sess_options=options, providers=["CPUExecutionProvider"])
            self.input_name = self.session.get_inputs()[0].name
            EmotionService._available = True
        except Exception as exc:
            self.error = str(exc)
            EmotionService._available = False

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def predict_crop(self, face_bgr: np.ndarray) -> dict:
        if not EmotionService._available:
            return {"detected": False, "error": self.error or "Seed 43 model unavailable"}
        logits = np.asarray(self.session.run(None, {self.input_name: prepare_face(face_bgr, self.size)})[0][0], dtype=np.float32)
        logits -= logits.max()
        probabilities = np.exp(logits)
        probabilities /= probabilities.sum()
        winner = int(probabilities.argmax())
        return {"detected": True, "emotion_type": LABELS[winner].capitalize(),
                "emotion_score": float(probabilities[winner]),
                "probabilities": {name: float(probabilities[i]) for i, name in enumerate(LABELS)}, "error": None}

    def predict_frame(self, frame_bgr: np.ndarray) -> dict:
        if not EmotionService._available:
            return {"detected": False, "error": self.error or "Seed 43 model unavailable"}
        cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        faces = cascade.detectMultiScale(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY), scaleFactor=1.1, minNeighbors=5)
        if not len(faces):
            return {"detected": False, "error": None}
        face = max(faces, key=lambda box: box[2] * box[3])
        return self.predict_crop(crop_face(frame_bgr, tuple(int(x) for x in face)))
