import torch
import torch.nn as nn
import numpy as np
import cv2
import math
from PIL import Image
from torchvision import transforms
from app.config import EMOTION_MODEL_PATH

_CLASS_NAMES = ["Angry", "Happy", "Neutral", "Sad"]
_IMG_SIZE = 64
_ROTATION_ALPHA = 0.2
_FACE_CROP_PADDING_RATIO = 0.18
_MIN_FACE_BOX_SIZE = 48


class _CNN(nn.Module):
    """Exact replica of the training architecture in Facial_Emotion_Detector_Final.py."""
    def __init__(self, num_classes: int = 4):
        super().__init__()
        self.features = nn.Sequential(
            nn.Conv2d(3, 16, 3, padding=1), nn.ReLU(), nn.MaxPool2d(2),
            nn.Conv2d(16, 32, 3, padding=1), nn.ReLU(), nn.MaxPool2d(2),
            nn.Conv2d(32, 64, 3, padding=1), nn.ReLU(), nn.MaxPool2d(2),
            nn.Conv2d(64, 128, 3, padding=1), nn.ReLU(), nn.MaxPool2d(2),
        )
        self.classifier = nn.Sequential(
            nn.Flatten(),
            nn.Linear(128 * 4 * 4, 512), nn.ReLU(), nn.Dropout(0.4),
            nn.Linear(512, num_classes),
        )

    def forward(self, x):
        return self.classifier(self.features(x))


class EmotionService:
    """Singleton wrapping the PyTorch CNN + MediaPipe face mesh."""

    _instance: "EmotionService | None" = None
    _available: bool = False

    def __init__(self):
        try:
            import mediapipe as mp
            self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
            ckpt = torch.load(str(EMOTION_MODEL_PATH), map_location=self.device, weights_only=False)
            state = ckpt.get("model_state", ckpt)
            self.class_names = ckpt.get("class_names", _CLASS_NAMES)
            self.img_size = ckpt.get("img_size", _IMG_SIZE)

            self.model = _CNN(num_classes=len(self.class_names))
            self.model.load_state_dict(state, strict=False)
            self.model.to(self.device).eval()

            self.preprocess = transforms.Compose([
                transforms.ToTensor(),
                # ImageNet normalization matches Facial_Emotion_Detector_Final.py line 306.
                # Removing this causes a training/inference mismatch that drops valdb
                # accuracy from 54.5% to 50.5% (sad class recall collapses from 72.7% to 22.7%).
                # See: C:\Users\user\Documents\MedCareAi\Emotion Detection — Learnings & Failures.md
                transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
            ])
            mp_fm = mp.solutions.face_mesh
            self.face_mesh = mp_fm.FaceMesh(
                max_num_faces=1, refine_landmarks=True,
                min_detection_confidence=0.7, min_tracking_confidence=0.5,
            )

            self._last_angle = 0.0
            EmotionService._available = True
            print(f"[Emotion] Loaded on {self.device} — classes: {self.class_names}")
        except Exception as exc:
            EmotionService._available = False
            print(f"[Emotion] Not available: {exc}")

    @classmethod
    def get_instance(cls) -> "EmotionService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def reset_session(self):
        """Reset per-session state (rotation smoothing) before processing a new video stream."""
        self._last_angle = 0.0

    def _letterbox_face(self, face_bgr: np.ndarray) -> np.ndarray:
        """Resize face crop without aspect-ratio distortion, padding to model input size."""
        h, w = face_bgr.shape[:2]
        if h <= 0 or w <= 0:
            raise ValueError("empty face crop")

        scale = min(self.img_size / w, self.img_size / h)
        new_w = max(1, int(round(w * scale)))
        new_h = max(1, int(round(h * scale)))
        interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR


        resized = cv2.resize(face_bgr, (new_w, new_h), interpolation=interp)
        canvas = np.zeros((self.img_size, self.img_size, 3), dtype=face_bgr.dtype)
        top = (self.img_size - new_h) // 2
        left = (self.img_size - new_w) // 2
        canvas[top:top + new_h, left:left + new_w] = resized
        return canvas

    def predict_frame_debug(self, frame_bgr: np.ndarray) -> dict:
        """
        Returns:
            {detected, emotion_type, emotion_score, probabilities: {Angry,Happy,Neutral,Sad}, error}
        """
        if not EmotionService._available:
            return {"detected": False, "emotion_type": None, "emotion_score": None,
                    "probabilities": {}, "error": "Model not loaded"}
        try:
            rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            result = self.face_mesh.process(rgb)
            if not result.multi_face_landmarks:
                return {"detected": False, "emotion_type": None, "emotion_score": None,
                        "probabilities": {}, "error": None}

            H, W = frame_bgr.shape[:2]
            lms = result.multi_face_landmarks[0].landmark
            xs = [int(lm.x * W) for lm in lms]
            ys = [int(lm.y * H) for lm in lms]
            raw_x1, raw_y1 = max(0, min(xs)), max(0, min(ys))
            raw_x2, raw_y2 = min(W, max(xs)), min(H, max(ys))
            face_box_w = raw_x2 - raw_x1
            face_box_h = raw_y2 - raw_y1
            if face_box_w < _MIN_FACE_BOX_SIZE or face_box_h < _MIN_FACE_BOX_SIZE:
                return {"detected": False, "emotion_type": None, "emotion_score": None,
                        "probabilities": {}, "error": "Face crop too small"}

            pad = int(_FACE_CROP_PADDING_RATIO * max(face_box_w, face_box_h))
            x1 = max(0, raw_x1 - pad)
            y1 = max(0, raw_y1 - pad)
            x2 = min(W, raw_x2 + pad)
            y2 = min(H, raw_y2 + pad)

            face = frame_bgr[y1:y2, x1:x2]
            if face.size == 0:
                return {"detected": False, "emotion_type": None, "emotion_score": None,
                        "probabilities": {}, "error": None}

            # Roll alignment using eye keypoints (landmarks 33 = right eye, 263 = left eye)
            # Matches Facial_Emotion_Detector_Final.py rotation smoothing logic
            re_x_full, re_y_full = int(lms[33].x * W), int(lms[33].y * H)
            le_x_full, le_y_full = int(lms[263].x * W), int(lms[263].y * H)
            # Convert to crop-relative coordinates (same as reference c1_crop / c2_crop)
            c1_crop = (re_x_full - x1, re_y_full - y1)
            c2_crop = (le_x_full - x1, le_y_full - y1)
            dx = c2_crop[0] - c1_crop[0]
            dy = c2_crop[1] - c1_crop[1]
            face_w = face.shape[1]
            min_dx = max(10, 0.12 * face_w)
            max_angle = 30.0
            min_angle = 2.0

            if dx > min_dx:
                raw_angle = math.degrees(math.atan2(dy, dx))
                if abs(raw_angle) <= max_angle:
                    smooth_angle = self._last_angle * (1.0 - _ROTATION_ALPHA) + raw_angle * _ROTATION_ALPHA
                    if abs(smooth_angle) >= min_angle:
                        cx, cy = face.shape[1] // 2, face.shape[0] // 2
                        M = cv2.getRotationMatrix2D((cx, cy), -smooth_angle, 1.0)
                        face = cv2.warpAffine(face, M, (face.shape[1], face.shape[0]),
                                              flags=cv2.INTER_LINEAR,
                                              borderMode=cv2.BORDER_REPLICATE)
                        self._last_angle = smooth_angle
                    else:
                        self._last_angle = self._last_angle * (1.0 - _ROTATION_ALPHA)
                else:
                    self._last_angle = self._last_angle * (1.0 - _ROTATION_ALPHA)
            else:
                self._last_angle = self._last_angle * (1.0 - _ROTATION_ALPHA)

            # Grayscale conversion: model was trained on grayscale FER2013 data
            gray = cv2.cvtColor(face, cv2.COLOR_BGR2GRAY)
            face = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)

            face_input = self._letterbox_face(face)
            pil = Image.fromarray(cv2.cvtColor(face_input, cv2.COLOR_BGR2RGB))
            inp = self.preprocess(pil).unsqueeze(0).to(self.device)
            with torch.no_grad():
                probs = torch.softmax(self.model(inp), dim=1).squeeze().cpu().numpy()

            idx = int(np.argmax(probs))
            return {
                "detected": True,
                "emotion_type": self.class_names[idx],
                "emotion_score": float(probs[idx]),
                "probabilities": {c: float(p) for c, p in zip(self.class_names, probs)},
                "error": None,
                # Debug fields (can be stripped by predict_frame)
                "_raw_angle": float(raw_angle) if 'raw_angle' in dir() else None,
                "_smooth_angle": float(smooth_angle) if 'smooth_angle' in dir() else None,
                "_last_angle": float(self._last_angle),
                "_face_wh": (int(face.shape[1]), int(face.shape[0])),
                "_face_box_wh": (int(face_box_w), int(face_box_h)),
                "_input_wh": (int(face_input.shape[1]), int(face_input.shape[0])),
                "_crop_xy": (int(x1), int(y1), int(x2), int(y2)),
            }
        except Exception as exc:
            return {"detected": False, "emotion_type": None, "emotion_score": None,
                    "probabilities": {}, "error": str(exc)}

    def predict_frame(self, frame_bgr: np.ndarray) -> dict:
        """Thin wrapper — runs predict_frame_debug and strips debug fields."""
        result = self.predict_frame_debug(frame_bgr)
        for key in ("_raw_angle", "_smooth_angle", "_last_angle", "_face_wh", "_face_box_wh", "_input_wh", "_crop_xy"):
            result.pop(key, None)
        return result
