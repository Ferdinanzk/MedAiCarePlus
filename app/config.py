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
# Prescription OCR needs only GEMINI_API_KEY (a secret: .env only, never committed); it serves every account.
# The default models are the pair that worked on 3-4 Oct 2026, when the earlier default gemini-3.8-flash answered
# every scan with 503 "high demand" (every account saw an all-N/A result). gemini-3.5-flash goes first because it
# read a real pharmacy receipt more accurately (4 Oct: 2 of 3 names exact, against 1 of 3 for flash-lite, which
# copied one row's name onto another), at 7-12 s a scan; flash-lite (2-3 s) is the fallback. docs/OCR.md has the
# measurements. The fallback is tried once when the primary is busy, times out or no longer exists; set
# OCR_GEMINI_FALLBACK_MODEL empty to turn it off.
OCR_DEFAULT_MODEL = "gemini-3.5-flash"
OCR_DEFAULT_FALLBACK_MODEL = "gemini-3.5-flash-lite"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
OCR_MODEL = os.getenv("OCR_MODEL", "").strip() or OCR_DEFAULT_MODEL
OCR_GEMINI_FALLBACK_MODEL = os.getenv("OCR_GEMINI_FALLBACK_MODEL", OCR_DEFAULT_FALLBACK_MODEL).strip()
# Seconds per Gemini request: gemini-3.5-flash took up to 11.8 s on a three-medicine receipt, so 25 s leaves the
# fallback 20 s of the 45 s scan budget.
OCR_GEMINI_TIMEOUT = max(1, int(os.getenv("OCR_GEMINI_TIMEOUT", "") or "25"))
OCR_GEMINI_IMAGE_BUDGET = max(OCR_GEMINI_TIMEOUT, int(os.getenv("OCR_GEMINI_IMAGE_BUDGET", "") or "45"))   # per scan
SECRET_KEY   = os.getenv("SECRET_KEY",   "change-me-in-production-32chars!!")
DEVICE       = "CPU"
MEDCARE_TIMEZONE = os.getenv("MEDCARE_TIMEZONE", "Asia/Taipei")
# How early a scheduled dose may be started (robot reminder, camera session) or recorded as taken, at most: never
# before halfway from the same medicine's previous dose. Earlier, the server refuses (409 dose_not_due_yet) while the
# patient's overdose protection is on (the default); see services/dose_safety.py.
DOSE_EARLY_MINUTES = max(0, int(os.getenv("DOSE_EARLY_MINUTES", "") or "120"))

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
LINE_WEBHOOK_ENDPOINT_URL = "https://api.line.me/v2/bot/channel/webhook/endpoint"

# Dose videos for family (opt-in, consent kind 'video'). LINE has no upload API: the family's LINE app downloads
# each clip from PUBLIC_BASE_URL (https). Empty = the address of the LINE webhook (scripts/line-tunnel.ps1 sets it).
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
DOSE_VIDEO_DIR  = Path(os.getenv("DOSE_VIDEO_DIR", "/tmp/medcare_dose_videos"))

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
# Check-in replies try LLM_MODEL, then LLM_FALLBACK_MODEL, and give up (a fixed line) after LLM_DEADLINE_SECONDS.
LLM_MODEL                = os.getenv("LLM_MODEL", "")
LLM_FALLBACK_MODEL       = os.getenv("LLM_FALLBACK_MODEL", "") or "openrouter/free"
LLM_DEADLINE_SECONDS     = float(os.getenv("LLM_DEADLINE_SECONDS", "") or "8")
# Optional OpenRouter provider routing: pin providers (comma-separated slugs) and refuse providers that store data.
# It applies to every call (reply, risk check, summary, memory) and to both models above.
OPENROUTER_PROVIDER_ONLY   = os.getenv("OPENROUTER_PROVIDER_ONLY", "")
OPENROUTER_DATA_COLLECTION = os.getenv("OPENROUTER_DATA_COLLECTION", "")   # "" | "allow" | "deny"
# Check-in replies know today's weather (services/context_info.py) from Open-Meteo, which needs no key. Only these
# coordinates are sent, so use a town's, not the patient's address. WEATHER_PLACE only names it in the logs; the
# language model never gets it. WEATHER_ENABLED=false or an empty coordinate turns the weather off; the date, time
# and holidays stay.
WEATHER_ENABLED          = os.getenv("WEATHER_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")
WEATHER_PLACE            = os.getenv("WEATHER_PLACE", "台北").strip()
WEATHER_LATITUDE         = os.getenv("WEATHER_LATITUDE", "25.0330").strip()
WEATHER_LONGITUDE        = os.getenv("WEATHER_LONGITUDE", "121.5654").strip()

REACHY_FEATURE_ENABLED  = os.getenv("REACHY_FEATURE_ENABLED", "").strip().lower() in ("1", "true", "yes")
RISK_CLASSIFIER_API_KEY = os.getenv("RISK_CLASSIFIER_API_KEY", "")

# Host-side copy of the deletion ledger, kept outside the backup rotation so a
# restore can re-apply deletions made after the backup was taken.
DELETION_LEDGER_FILE = Path(os.getenv("DELETION_LEDGER_FILE", "/ledger/deletion_ledger.jsonl"))

# ── Reachy robot (Phase 2) ───────────────────────────────────────────────────
PUBLIC_PORT = int(os.getenv("PUBLIC_PORT", "8000"))
DEVICE_PORT = int(os.getenv("DEVICE_PORT", "8001"))   # never published or tunnelled
