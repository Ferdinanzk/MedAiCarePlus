# MedAiCarePlus — Claude Ground Rules

## What This Project Is
Full-stack medical care app (AIOT CAREBOX) with:
- Face recognition login (OpenVINO)
- Emotion detection (PyTorch)
- Prescription OCR scanning (YOLO + Ollama vision)
- Medication tracking + intake scheduling
- LINE notifications (missed dose, emotion alerts, weekly summary)
- React SPA served by FastAPI at port 8000

## Repo Structure
```
MedAiCarePlus/          ← Backend (FastAPI, Python)
  app/
    routers/            ← API endpoints (api_*.py = new React API, others = legacy)
    services/           ← ML services (face, emotion, ocr, line)
    jobs/               ← Scheduled jobs (missed dose, refill, weekly summary)
    config.py           ← All env vars and model paths
    dependencies.py     ← JWT auth (Supabase + face token)
  sql/init.sql          ← DB schema (runs on startup via asyncpg)
  docker-compose.dev.yml ← USE THIS for local dev
  Dockerfile            ← Full ML build (torch, openvino, mediapipe, yolo)
  frontend_source/      ← React source (Vite + Tailwind)

medaicareplus-web/      ← ALSO edit React source here (synced twin)
  src/pages/            ← Page components
  src/lib/              ← Auth helpers (face-auth.ts, supabase.ts, ai-api.ts)
```

## How to Run Locally
```powershell
# Start everything (backend + postgres + ollama)
cd "d:\Claude_tool\repo\MedAiCarePlus"
docker compose -f docker-compose.dev.yml up -d

# After editing backend Python:
docker compose -f docker-compose.dev.yml up -d --build medaicare

# After editing frontend React (MUST sync both locations first):
Copy-Item "medaicareplus-web\src\pages\*.tsx" "frontend_source\src\pages\" -Force
docker compose -f docker-compose.dev.yml up -d --build medaicare
```

App runs at: http://localhost:8000

## Architecture Decisions (do not change without discussion)

### Auth — Two token types
1. **Supabase JWT** — for email/password login users. Validated with `SUPABASE_JWT_SECRET`.
2. **Face token** — itsdangerous `URLSafeTimedSerializer(SECRET_KEY)`, 8h expiry. Stored as `face_auth_token` in localStorage.
- `get_current_user` in `dependencies.py` tries face token first, then Supabase JWT.
- Dev fallback: if `SUPABASE_JWT_SECRET` is empty, Supabase tokens accepted without verification (ES256/HS256 both).

### React ↔ Backend sync
- React source lives in **two places**: `medaicareplus-web/src/` (dev editing) and `frontend_source/src/` (Docker build source).
- **Always copy** edited files from `medaicareplus-web/src/` → `frontend_source/src/` before rebuilding Docker.
- Auth helper: `getAuthHeaders()` in each page — always uses `supabase.auth.getSession() || getFaceToken()`.

### OCR Pipeline
- YOLO segments the prescription image → perspective warp → CLAHE enhancement
- Two-pass Ollama call: Pass 1 = full text extraction (19 fields), Pass 2 = icon row (schedule checkboxes)
- Active model priority: `OLLAMA_MODELS = ["gemini-3-flash-preview", "minicpm-v", "llama3.2-vision", "llava", "gemma3"]`
- **Cloud models** (`:cloud` suffix) must NOT have `num_predict` in options — causes timeout.
- `gemini-3-flash-preview:cloud` is the current preferred model (works with images, fast).
- `_clean()` strips surrogates from Ollama responses before JSON parsing.

### DB — asyncpg, no ORM
- Direct `asyncpg` connection pool. All queries are raw SQL.
- `schedule_time` and `prescription_meta` columns are JSONB — must `json.dumps()` before passing to asyncpg.
- `"user"` table name is quoted everywhere (PostgreSQL reserved word).
- DB URL: direct connection to Supabase (port 5432), NOT pooler (port 6543).

### Navigation in React SPA
- Use `window.location.href = '/path'` for auth state transitions (login, logout, post-registration).
- Use React Router `navigate()` only for non-auth page changes.
- Reason: `navigate()` doesn't re-run `useEffect` mount hooks → `onboardingComplete` from localStorage stays stale.

### Logout — clear all localStorage flags
```typescript
localStorage.removeItem('face_auth_session');
localStorage.removeItem('face_auth_token');
localStorage.removeItem('onboarding_complete');
localStorage.removeItem('onboarding_face_done');
```

## Key Env Vars
| Variable | Purpose | Default |
|----------|---------|---------|
| `DATABASE_URL` | Supabase direct connection (port 5432) | local postgres |
| `SUPABASE_JWT_SECRET` | Validates Supabase JWTs | empty (dev: skip) |
| `SECRET_KEY` | Signs face auth tokens | `change-me-in-production-32chars!!` |
| `OLLAMA_URL` | Ollama API endpoint | `http://host.docker.internal:11434/api/generate` |
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE Messaging API | empty |
| `MOONSHOT_API_KEY` | Kimi direct API (optional OCR alt) | empty |

## LINE Notifications
- Webhook URL changes every ngrok restart (free tier). Must update LINE Developer Console each time.
- Test missed-dose: `POST /api/notify/missed-dose?u_id=1&med_name=Aspirin&scheduled_time=08:00`
- `/api/notify/status` → `{"configured": true/false}`

## What's Been Built (current state as of 2026-05-16)

### Working ✅
- Face login + enrollment (3-photo auto-capture sequence)
- Emotion detection via camera
- Prescription OCR — 19 fields, YOLO + Gemini Flash via Ollama
- Medications CRUD with manual input form (schedule, use_before, pill_description)
- Intake schedule auto-generation (30 days ahead on medication add)
- Family contacts with verification code generation
- LINE missed-dose notifications
- Supabase auth + face auth dual-token system
- React SPA: Dashboard, Medications, Scan, Family, Intake, Emotion pages

### Pending ⏳
- History page backend endpoints
- BottomNav "Scan" and "History" labels still hardcoded in English
- LINE webhook persistent URL (needs paid ngrok or fixed domain)
- Registration flow end-to-end test with real camera
- Emotion check-in end-to-end test

## Common Gotchas
1. **OCR all N/A?** → Check if Ollama model has `num_predict: -1` (breaks cloud models). Remove it for `:cloud` models.
2. **face_auth_token expired?** → 8h TTL. Re-generate: `python3 -c "from itsdangerous import URLSafeTimedSerializer; print(URLSafeTimedSerializer('change-me-in-production-32chars!!').dumps({'u_id':1,'name':'Ferdinan'}))"` inside container.
3. **asyncpg JSONB error `expected str, got dict`?** → Wrap dict in `json.dumps()` before passing.
4. **React page shows wrong user?** → Supabase and face sessions can conflict. Clear `sb-*-auth-token` from localStorage if testing face auth.
5. **Model not loading?** → Check `/health` endpoint. Face recognition needs OpenVINO models at the volume path.
