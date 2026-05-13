import os
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent   # MedAiCarePlus/
_AI_ROOT = _HERE.parent                          # project_AI_/

FACE_REC_BASE = Path(os.getenv(
    "FACE_REC_BASE",
    str(_AI_ROOT / "face_recognition_folder" / "face_recognition")
))
FACE_GALLERY_DIR  = FACE_REC_BASE / "face_gallery"
INTEL_MODELS_DIR  = FACE_REC_BASE / "intel"
FACE_DET_MODEL    = INTEL_MODELS_DIR / "face-detection-adas-0001" / "FP32" / "face-detection-adas-0001.xml"
FACE_REID_MODEL   = INTEL_MODELS_DIR / "face-reidentification-retail-0095" / "FP32" / "face-reidentification-retail-0095.xml"
LANDMARKS_MODEL   = INTEL_MODELS_DIR / "landmarks-regression-retail-0009" / "FP32" / "landmarks-regression-retail-0009.xml"

EMOTION_MODEL_PATH = Path(os.getenv(
    "EMOTION_MODEL_PATH",
    str(_AI_ROOT / "FaceEmotionDetector" / "FaceEmotionDetector" / "model4.2.2.pth")
))

YOLO_MODEL_PATH = Path(os.getenv(
    "YOLO_MODEL_PATH",
    str(_AI_ROOT / "segmentation" / "prescription_best_100_epo.pt")
))

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://medai:medai@localhost:5432/medaicare")
OLLAMA_URL   = os.getenv("OLLAMA_URL",   "http://localhost:11434/api/generate")
SECRET_KEY   = os.getenv("SECRET_KEY",   "change-me-in-production-32chars!!")
DEVICE       = "CPU"

FACE_DET_CONFIDENCE  = float(os.getenv("FACE_DET_CONFIDENCE",  "0.6"))
FACE_MATCH_THRESHOLD = float(os.getenv("FACE_MATCH_THRESHOLD", "0.3"))
OLLAMA_TIMEOUT       = int(os.getenv("OLLAMA_TIMEOUT", "180"))
OLLAMA_MODELS        = ["minicpm-v", "llama3.2-vision", "llava"]

# ── React frontend / Supabase integration ────────────────────────────────────
SUPABASE_URL       = os.getenv("SUPABASE_URL", "")
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET", "")
FRONTEND_URL       = os.getenv("FRONTEND_URL", "http://localhost:5173")

# ── LINE Messaging API ───────────────────────────────────────────────────────
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_API_URL = "https://api.line.me/v2/bot/message/push"
