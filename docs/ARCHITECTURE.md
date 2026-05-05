# MedAiCarePlus — Architecture

## System Overview

```
Browser (webcam + uploads)
        │
        ▼
FastAPI (app/main.py) ──── PostgreSQL
        │
        ├── /auth          ── FaceRecognitionService (OpenVINO)
        ├── /emotion (WS)  ── EmotionService (PyTorch CNN)
        ├── /ocr           ── OCRService (YOLO + Ollama HTTP)
        ├── /medicines     ── DB CRUD
        ├── /notifications ── DB read + LINE stub
        └── /display       ── DB aggregation
                                    │
                            Ollama (minicpm-v)
```

## Data Flow (AIOT CAREBOX diagram)

1. **Face Recognition Login** → identifies patient → looks up `user.face_label` → writes `login_log` row → sets session cookie
2. **Registration** → inserts `user` + `detail` rows → `face_label` must match a folder in `face_gallery/`
3. **Emotion Check-In** → 5s WebSocket stream → EmotionService per frame → user clicks Save → inserts `emotion` row
4. **Prescription OCR** → upload image → YOLO detects document boundary → perspective warp → two-pass Ollama OCR → user confirms → inserts `medication` row
5. **Take/Skip Dose** → upserts `intake` row for today → increments `medication.total_intake`
6. **Notification** → reads `intake` rows where `notify_stats='pending'` → dismiss updates to `'sent'` → LINE send is a stub for future integration
7. **Dashboard** → joins `medication` + `intake` for today → fetches last 14 `emotion` records for chart

## AI Services — Startup Behaviour

All three services are singletons loaded once at app startup via FastAPI `lifespan`. If a model file is missing, the service logs a warning and sets `_available = False`. Routes that call an unavailable service return a JSON error with HTTP 200 so the frontend can show a graceful message.

## Thread Safety

All AI inference (OpenVINO, PyTorch, YOLO) is synchronous. FastAPI routes call them via `asyncio.run_in_executor(None, ...)` to avoid blocking the event loop. The asyncpg pool handles concurrent DB access natively.

## Docker Volumes

Model files are never baked into the Docker image — they are mounted read-only from the host at runtime. This keeps the image small (~2 GB without model weights) and allows swapping models without rebuilding.
