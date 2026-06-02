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
                transforms.Resize((self.img_size, self.img_size)),
                transforms.ToTensor(),
                transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
            ])
            mp_fm = mp.solutions.face_mesh
            self.face_mesh = mp_fm.FaceMesh(
                max_num_faces=1, refine_landmarks=True,
                min_detection_confidence=0.5, min_tracking_confidence=0.5,
            )
            # Rotation smoothing state (persistent across frames in a session)
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
            pad = int(0.3 * max(max(xs) - min(xs), max(ys) - min(ys)))
            x1 = max(0, min(xs) - pad)
            y1 = max(0, min(ys) - pad)
            x2 = min(W, max(xs) + pad)
            y2 = min(H, max(ys) + pad)

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

            pil = Image.fromarray(cv2.cvtColor(face, cv2.COLOR_BGR2RGB))
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
                "_crop_xy": (int(x1), int(y1), int(x2), int(y2)),
            }
        except Exception as exc:
            return {"detected": False, "emotion_type": None, "emotion_score": None,
                    "probabilities": {}, "error": str(exc)}

    def predict_frame(self, frame_bgr: np.ndarray) -> dict:
        """Thin wrapper — runs predict_frame_debug and strips debug fields."""
        result = self.predict_frame_debug(frame_bgr)
        for key in ("_raw_angle", "_smooth_angle", "_last_angle", "_face_wh", "_crop_xy"):
            result.pop(key, None)
        return result
