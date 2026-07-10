# MedAiCarePlus — AIOT CAREBOX

Full-stack medical care app with face recognition login, prescription OCR, medication tracking, and LINE notifications.

## Features

- **Face login** — OpenVINO 3-stage pipeline (detect → landmark → re-id)
- **Emotion detection** — Local Hugging Face SigLIP classifier via webcam
- **Prescription OCR** — YOLOv8-seg + Ollama vision (19 fields extracted)
- **Medication tracking** — CRUD with schedule, use-before dates, warnings
- **Intake scheduling** — Auto-generates 30-day intake records on medication add
- **LINE notifications** — Missed-dose alerts, emotion alerts, weekly summaries
- **Family contacts** — Verification code-based contact management

## Stack

| Layer | Tech |
|-------|------|
| Backend | FastAPI + asyncpg (raw SQL, no ORM) |
| Frontend | React + Vite + Tailwind (served by FastAPI) |
| Database | PostgreSQL 16 (via Supabase or local Docker) |
| Auth | Supabase JWT + itsdangerous face token |
| ML | OpenVINO 2024.5, PyTorch, MediaPipe, YOLOv8 |
| OCR | Ollama (minicpm-v / Gemini Flash cloud) |
| Notifications | LINE Messaging API |

---

## Prerequisites

- Docker Desktop
- Python 3.10+ (for local dev outside Docker)
- [Ollama](https://ollama.com) installed and running locally
- A [Supabase](https://supabase.com) project (free tier works)
- Git LFS: `git lfs install`

---

## Setup

### 1. Clone and configure environment

```bash
git clone <repo-url>
cd MedAiCarePlus
git lfs pull          # downloads ML model files tracked by LFS

cp .env.example .env
# Edit .env — fill in SUPABASE_URL, SUPABASE_JWT_SECRET, SECRET_KEY
```

### 2. Download ML models

Place model files in the `models/` directory as shown below.

#### A. Face recognition — OpenVINO (Intel Open Model Zoo, free & public)

```bash
pip install openvino-dev
omz_downloader --name face-detection-adas-0001          --output_dir models/face_recognition/intel
omz_downloader --name landmarks-regression-retail-0009  --output_dir models/face_recognition/intel
omz_downloader --name face-reidentification-retail-0095 --output_dir models/face_recognition/intel
```

> Docs: [Intel Open Model Zoo](https://github.com/openvinotoolkit/open_model_zoo)

#### B. Emotion detection — local SigLIP classifier

The live classifier is loaded from
`models/emotion_hf/Facial-Emotion-Detection-SigLIP2/` using Hugging Face
Transformers in offline mode. Run `git lfs pull` to obtain the checkpoint.
Application outputs are normalized to `happy`, `sad`, `angry`, `neutral`,
`surprised`, and `disgust`. The current checkpoint does not directly predict
`disgust`.

#### C. Prescription YOLO — `models/segmentation/prescription_best_100_epo.pt`

Custom YOLOv8-seg model trained on prescription documents.
Downloaded automatically by `git lfs pull`.

#### D. Ollama vision model

```bash
ollama pull minicpm-v
# Cloud OCR (gemini-3-flash-preview:cloud) needs no local pull — internet required
```

### 3. Set up the database

```bash
# Supabase: Dashboard → SQL Editor → paste sql/init.sql → Run
# Local Docker: psql -U medai -d medaicare -f sql/init.sql
```

### 4. Start the stack

```bash
docker compose -f docker-compose.dev.yml up -d
```

App runs at: **http://localhost:8000**

---

## Environment Variables

Copy `.env.example` → `.env` and fill in your values.

| Variable | Purpose |
|----------|---------|
| `DATABASE_URL` | Postgres — use port **5432** (not Supabase pooler 6543) |
| `SECRET_KEY` | Signs face auth tokens — `python -c "import secrets; print(secrets.token_hex(32))"` |
| `SUPABASE_JWT_SECRET` | Validates Supabase JWTs — Supabase Dashboard → Settings → API |
| `SUPABASE_URL` | Your Supabase project URL |
| `EMOTION_HF_MODEL_PATH` | Local Hugging Face SigLIP model directory |
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE bot token — leave empty to disable notifications |
| `OLLAMA_URL` | Ollama API — default works with Docker Desktop |

---

## LINE Webhook Setup (optional)

LINE requires a public HTTPS URL. On local dev, use ngrok:

```bash
ngrok http 8000
# Copy HTTPS URL → LINE Developers Console → Webhook URL:
# https://<id>.ngrok-free.app/api/notify/webhook
```

> Free ngrok gives a new URL on each restart. A paid plan or fixed domain gives a persistent URL.

---

## Development Workflow

```powershell
# Rebuild backend after Python changes:
docker compose -f docker-compose.dev.yml up -d --build medaicare

# Rebuild frontend (sync source first):
Copy-Item "medaicareplus-web\src\pages\*.tsx" "frontend_source\src\pages\" -Force
docker compose -f docker-compose.dev.yml up -d --build medaicare

# Health check:
curl http://localhost:8000/health
```

> **React source lives in two places:** `medaicareplus-web/src/` (edit here) and `frontend_source/src/` (Docker build input). Always sync before rebuilding.

---

## Project Structure

```
MedAiCarePlus/
├── app/
│   ├── routers/          # API endpoints
│   ├── services/         # ML services (face, emotion, OCR, LINE)
│   ├── jobs/             # APScheduler jobs (missed dose, weekly summary)
│   ├── config.py         # Env var loading + model paths
│   └── dependencies.py   # JWT auth (Supabase + face token)
├── models/
│   ├── face_recognition/ # OpenVINO models + face gallery (download via omz_downloader)
│   ├── emotion_hf/       # local SigLIP classifier (Git LFS)
│   └── segmentation/     # prescription_best_100_epo.pt (Git LFS)
├── frontend_source/      # React source — Docker build input
├── medaicareplus-web/    # React source — local editing copy
├── sql/init.sql          # Database schema
├── docker-compose.dev.yml
└── Dockerfile
```

---

## Database Schema

| Table | Description |
|-------|-------------|
| `user` | Registered patients (name, face_label, supabase_id) |
| `detail` | Extended profile (age, gender, address) |
| `emotion` | Emotion check-in records |
| `medication` | Prescriptions (med_name, schedule_time JSONB) |
| `intake` | Daily dose events (taken/skipped/pending) |
| `family_contacts` | Family members with notification preferences |

---

## Common Issues

| Symptom | Fix |
|---------|-----|
| OCR returns all N/A | Remove `num_predict` from Ollama options for `:cloud` models |
| Face auth token expired | 8h TTL — re-login via face scan |
| asyncpg JSONB error | Wrap `dict` in `json.dumps()` before passing to asyncpg |
| Model not loading | Check `/health`; verify `models/` paths are mounted correctly |
| LINE webhook not firing | Update webhook URL in LINE Developer Console after ngrok restart |
