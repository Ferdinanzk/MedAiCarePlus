import os
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent   # MedAiCarePlus/
_AI_ROOT = _HERE.parent

FACE_REC_BASE = Path(os.getenv(
    "FACE_REC_BASE",
    str(_HERE / "models" / "face_recognition")
))
FACE_GALLERY_DIR  = FACE_REC_BASE / "face_gallery"
INTEL_MODELS_DIR  = FACE_REC_BASE / "intel"
FACE_DET_MODEL    = INTEL_MODELS_DIR / "face-detection-adas-0001" / "FP32" / "face-detection-adas-0001.xml"
FACE_REID_MODEL   = INTEL_MODELS_DIR / "face-reidentification-retail-0095" / "FP32" / "face-reidentification-retail-0095.xml"
LANDMARKS_MODEL   = INTEL_MODELS_DIR / "landmarks-regression-retail-0009" / "FP32" / "landmarks-regression-retail-0009.xml"

EMOTION_MODEL_PATH = Path(os.getenv(
    "EMOTION_MODEL_PATH",
    str(_HERE / "models" / "emotion_seed43" / "model_fp32.onnx")
))

# MediaPipe face/hand/pose landmark models as ONNX, for camera frames the Reachy streams to the server.
LANDMARK_MODEL_DIR = Path(os.getenv(
    "LANDMARK_MODEL_DIR",
    str(_HERE / "models" / "landmarks")
))

YOLO_MODEL_PATH = Path(os.getenv(
    "YOLO_MODEL_PATH",
    str(_AI_ROOT / "segmentation" / "prescription_best_100_epo.pt")
))

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://medai:medai@localhost:5432/medaicare")
OLLAMA_URL   = os.getenv("OLLAMA_URL",   "http://localhost:11434/api/generate")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
OCR_MODEL = os.getenv("OCR_MODEL", "gemini-3.8-flash")
SECRET_KEY   = os.getenv("SECRET_KEY",   "change-me-in-production-32chars!!")
DEVICE       = "CPU"
MEDCARE_TIMEZONE = os.getenv("MEDCARE_TIMEZONE", "Asia/Taipei")

FACE_DET_CONFIDENCE  = float(os.getenv("FACE_DET_CONFIDENCE",  "0.6"))
FACE_MATCH_THRESHOLD = float(os.getenv("FACE_MATCH_THRESHOLD", "0.3"))
OLLAMA_TIMEOUT       = int(os.getenv("OLLAMA_TIMEOUT", "180"))
OLLAMA_MODELS        = ["gemini-3-flash-preview", "minicpm-v", "llama3.2-vision", "llava", "gemma3"]

# ── React frontend / Supabase integration ────────────────────────────────────
SUPABASE_URL       = os.getenv("SUPABASE_URL", "")
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET", "")
FRONTEND_URL       = os.getenv("FRONTEND_URL", "http://localhost:5173")

# ── LINE Messaging API ───────────────────────────────────────────────────────
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET       = os.getenv("LINE_CHANNEL_SECRET", "")
LINE_API_URL = "https://api.line.me/v2/bot/message/push"

# ── Deployment mode, legal notice, consent (Phase 1) ─────────────────────────
APP_ENV = os.getenv("APP_ENV", "dev").strip().lower()          # "dev" | "prod"
TERMS_VERSION = os.getenv("TERMS_VERSION", "2026-10")
LEGAL_DIR = _HERE / "app" / "legal"

# Fill-ins rendered into the legal notice; production refuses to start while
# the ones a published notice needs are empty (app/startup_checks.py).
OPERATOR_NAME            = os.getenv("OPERATOR_NAME", "")
OPERATOR_CONTACT         = os.getenv("OPERATOR_CONTACT", "")
TUNNEL_PROVIDER          = os.getenv("TUNNEL_PROVIDER", "")
# Conversation text goes robot → LLM_SERVICE (a router) → LLM_PROVIDER (the model company).
LLM_SERVICE              = os.getenv("LLM_SERVICE", "OpenRouter")
LLM_PROVIDER             = os.getenv("LLM_PROVIDER", "")
LLM_PROVIDER_REGION      = os.getenv("LLM_PROVIDER_REGION", "")
LLM_RETENTION            = os.getenv("LLM_RETENTION", "")
OPENROUTER_API_KEY       = os.getenv("OPENROUTER_API_KEY", "")
LLM_MODEL                = os.getenv("LLM_MODEL", "")
# Optional OpenRouter provider routing: pin providers (comma-separated slugs) and refuse providers that store data.
OPENROUTER_PROVIDER_ONLY   = os.getenv("OPENROUTER_PROVIDER_ONLY", "")
OPENROUTER_DATA_COLLECTION = os.getenv("OPENROUTER_DATA_COLLECTION", "")   # "" | "allow" | "deny"

REACHY_FEATURE_ENABLED  = os.getenv("REACHY_FEATURE_ENABLED", "").strip().lower() in ("1", "true", "yes")
RISK_CLASSIFIER_API_KEY = os.getenv("RISK_CLASSIFIER_API_KEY", "")

# Host-side copy of the deletion ledger, kept outside the backup rotation so a
# restore can re-apply deletions made after the backup was taken.
DELETION_LEDGER_FILE = Path(os.getenv("DELETION_LEDGER_FILE", "/ledger/deletion_ledger.jsonl"))

# ── Reachy robot (Phase 2) ───────────────────────────────────────────────────
PUBLIC_PORT = int(os.getenv("PUBLIC_PORT", "8000"))
DEVICE_PORT = int(os.getenv("DEVICE_PORT", "8001"))   # never published or tunnelled
