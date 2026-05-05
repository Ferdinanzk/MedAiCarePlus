# MedAiCarePlus

AIOT CAREBOX — a medical care web application combining face recognition login, real-time emotion monitoring, and AI-powered prescription OCR.

## Features

| Module | AI Model | Description |
|--------|----------|-------------|
| Face Login | OpenVINO face-detection + face-reidentification | Identify registered patients via webcam |
| Emotion Check-In | PyTorch CNN (model4.2.2.pth) | Detect Angry / Happy / Neutral / Sad in 5s session |
| Prescription Scan | YOLO + Ollama (minicpm-v) | Two-pass OCR of Taiwanese hospital prescriptions |
| Dashboard | — | Today's intake status, emotion history chart |
| Notifications | — | Pending dose alerts, LINE send stub |

---

## Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (for the Docker path)
- **OR** Python 3.11 + pip (for local dev)
- Webcam connected and accessible

---

## Quick Start — Docker

```bash
# 1. Start all services
docker compose up --build -d

# 2. First-time: pull the Ollama vision model (~5 GB)
docker compose exec ollama ollama pull minicpm-v

# 3. Open the app
start http://localhost:8000
```

The PostgreSQL database is initialised automatically on first start.

---

## Quick Start — Local Development (Windows)

```bat
# Install dependencies
pip install -r requirements.txt

# Set env vars (copy and edit .env.example)
set DATABASE_URL=postgresql://medai:medai@localhost:5432/medaicare

# Start PostgreSQL separately (or use Docker for just the DB):
docker compose up postgres -d

# Start the app
run_dev.bat
```

---

## Environment Variables

| Variable | Default (dev) | Docker value |
|----------|--------------|--------------|
| `DATABASE_URL` | `postgresql://medai:medai@localhost:5432/medaicare` | `postgresql://medai:medai@postgres:5432/medaicare` |
| `FACE_REC_BASE` | `../face_recognition_folder/face_recognition` | `/models/face_recognition` |
| `EMOTION_MODEL_PATH` | `../FaceEmotionDetector/FaceEmotionDetector/model4.2.2.pth` | `/models/emotion/model4.2.2.pth` |
| `YOLO_MODEL_PATH` | `../segmentation/prescription_best_100_epo.pt` | `/models/segmentation/prescription_best_100_epo.pt` |
| `OLLAMA_URL` | `http://localhost:11434/api/generate` | `http://ollama:11434/api/generate` |
| `SECRET_KEY` | `change-me-in-production-32chars!!` | Set a strong random string |

---

## Database Schema

| Table | Description |
|-------|-------------|
| `user` | Registered patients (name, face_label, line_id) |
| `detail` | Extended profile (age, gender, address) — 1:1 with user |
| `emotion` | Emotion check-in records (emotion_type, emotion_score, time_stamp) |
| `medication` | Prescriptions (med_name, schedule_time JSONB, pill_prescribed) |
| `intake` | Daily dose events (intake_stats: taken/skipped/pending, notify_stats) |

---

## AI Model Files Required

| Model | Location in project_AI_ |
|-------|------------------------|
| Face detection | `face_recognition_folder/face_recognition/intel/face-detection-adas-0001/FP32/` |
| Face re-identification | `face_recognition_folder/face_recognition/intel/face-reidentification-retail-0095/FP32/` |
| Landmarks | `face_recognition_folder/face_recognition/intel/landmarks-regression-retail-0009/FP32/` |
| Face gallery | `face_recognition_folder/face_recognition/face_gallery/` |
| Emotion CNN | `FaceEmotionDetector/FaceEmotionDetector/model4.2.2.pth` |
| YOLO prescription | `segmentation/prescription_best_100_epo.pt` |
| Ollama vision | Downloaded via `ollama pull minicpm-v` |

---

## Web Pages

| URL | Page |
|-----|------|
| `/auth/login` | Face recognition login |
| `/auth/register` | Patient registration |
| `/display/` | Dashboard (intake + emotion chart) |
| `/emotion/` | Live emotion check-in |
| `/ocr/` | Prescription scanner |
| `/medicines/` | Medication list |
| `/notifications/` | Pending dose alerts |
| `/docs` | FastAPI OpenAPI docs |
