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

    def predict_frame(self, frame_bgr: np.ndarray) -> dict:
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
            re_x, re_y = int(lms[33].x * W) - x1, int(lms[33].y * H) - y1
            le_x, le_y = int(lms[263].x * W) - x1, int(lms[263].y * H) - y1
            dx, dy = le_x - re_x, le_y - re_y
            if dx > max(10, 0.12 * face.shape[1]):
                angle = math.degrees(math.atan2(dy, dx))
                if abs(angle) <= 30:
                    cx, cy = face.shape[1] // 2, face.shape[0] // 2
                    M = cv2.getRotationMatrix2D((cx, cy), -angle, 1.0)
                    face = cv2.warpAffine(face, M, (face.shape[1], face.shape[0]),
                                          flags=cv2.INTER_LINEAR,
                                          borderMode=cv2.BORDER_REPLICATE)

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
            }
        except Exception as exc:
            return {"detected": False, "emotion_type": None, "emotion_score": None,
                    "probabilities": {}, "error": str(exc)}
