# MedAiCarePlus — Claude Ground Rules

## What This Project Is
Full-stack medical care app (AIOT CAREBOX). React SPA served by FastAPI at port 8000.

Working features: face-recognition login (OpenVINO), emotion detection (ONNX), **live pill-intake monitoring** (the main feature — see below), medication tracking + intake scheduling, LINE notifications.

Prescription OCR exists in the codebase but is **currently non-functional** — `ultralytics` is not installed, so `ocr_service.py`'s lazy YOLO import fails and `/health` reports `"ocr": false`. Ollama is used *only* by OCR; nothing else touches it.

## Repo Structure
```
app/
  routers/          ← api_*.py = React JSON API; others = legacy Jinja2
    api_monitor.py  ← live intake monitoring endpoints
  services/         ← face_recognition, emotion, ocr, line, monitor, intake_detection
  jobs/             ← scheduled jobs (missed dose, refill, weekly summary)
  config.py         ← env vars + model paths
  dependencies.py   ← JWT auth (Supabase + face token)
  main.py           ← lifespan model warmup, routers, SPA serving
frontend_source/    ← React source (Vite + Tailwind) — THE ONLY frontend copy
models/             ← emotion_seed43/ (ONNX), face_recognition/ (OpenVINO IR)
sql/init.sql        ← schema (runs on startup via asyncpg)
tests/              ← test_monitor_backend.py, test_medication_intake_now.py
```

There is **no `medaicareplus-web/` twin directory** and no manual file-sync step. The Dockerfile builds `frontend_source/` in a `node:22-alpine` stage and copies `dist/` → `/app/static/web`. Edit `frontend_source/` and rebuild.

## How to Run Locally
The running stack is **`docker-compose.yml`** (project `medcareai2`, services `postgres` + `app`):

```powershell
cd "d:\medcareai2_20260920\app"
docker compose up -d                 # start
docker compose up -d --build app     # rebuild after ANY backend or frontend edit
docker compose logs -f app           # tail logs
```

App: http://localhost:8000 · Health: http://localhost:8000/health

`docker-compose.dev.yml` is a *different* stack (services `medaicare` + `ollama` + `ngrok`) and is **not** what currently runs. Don't mix them.

## The Intake Monitor (main feature)

Pressing "take pill" opens a session that runs two **decoupled** HTTP streams concurrently:

1. **Landmark stream — ~15fps (66ms tick).** Browser Web Worker (`frontend_source/src/workers/monitorWorker.ts`) runs three MediaPipe models (face + hand + pose) and POSTs landmarks to `/api/intake/monitor/landmarks`. Server-side `PillIngestionDetector` (`services/intake_detection.py`) scores them — pure geometry/heuristics, **no neural net**.
2. **Vision stream — ≤5fps (200ms gate).** Uploads a ≤640×480 JPEG to `/api/intake/monitor/vision`, which runs OpenVINO face recognition (3 chained models) and the ONNX emotion model in a thread executor.

Session state lives in `services/monitor_service.py` (`MonitorRegistry`, in-memory, one session per user).

### Critical gotcha — detector thresholds are frame-rate-calibrated
`moving_toward()` (`intake_detection.py:93-96`) returns a **raw per-frame delta** with no division by elapsed time. So `APPROACH_SPEED_THRESHOLD_NORM` (0.05) and `ERRATIC_APPROACH_STD_NORM` (0.18) are implicitly tuned to ~15fps.

**Lowering the capture rate silently miscalibrates scoring** — it does not degrade gracefully. At ~3fps the deltas are ~5× larger, which loses `trajectory_contribution` (+0.12) and trips the erratic penalty (−0.05): a −0.17 swing against `CONFIRM_THRESHOLD = 0.40`, so genuine intakes stop auto-confirming with no error. Before changing fps, normalize `moving_toward()` by elapsed time and re-validate.

Practical floor is ~8fps. The frontend self-throttles (`tick()` returns early while `workerBusyRef`/`landmarkBusyRef` are set), so slow hardware reduces sample rate rather than queueing.

## Architecture Decisions (do not change without discussion)

### Auth — face tokens only
`get_current_user` (`app/dependencies.py`) accepts **only** face tokens: `itsdangerous.URLSafeTimedSerializer(SECRET_KEY)`, 8h expiry, stored as `face_auth_token` in localStorage, carrying `u_id`. Email/password login (`/api/auth/email-login`, bcrypt) also returns a face token. Supabase is **not used**: `frontend_source/src/lib/supabase.ts` is imported nowhere, and `/api/auth/link-account` still expects a Supabase `sub` claim, so it cannot work (pre-existing, left as-is).

### Consent is enforced server-side (Phase 1, terms version `2026-10`)
- Data routes depend on **`get_consented_user`**, not `get_current_user`. It returns 403 `consent_required` unless the user's latest `core` consent is granted at the current `TERMS_VERSION`. New authenticated routes must use it unless they belong to *limited mode*. `tests/test_consent_enforcement.py` introspects every route and fails on a missed swap.
- Limited-mode (exempt) routes: `/api/consent/*`, `/api/legal/*`, `/api/account/export`, `/api/account/delete`, `/api/auth/onboarding-status`, plus the open login/register/identify/webhook routes.
- The legal notice text lives in `app/legal/<kind>/<version>/<lang>.json` (`core` now; `robot` for Reachy later), **not** in `i18n.ts`. `legal_service` renders operator fill-ins and hashes the canonical JSON. Consent rows reference `(kind, version, language, sha256)`. Changing the text means bumping `TERMS_VERSION`, which re-prompts everyone.
- Consent state is cached per process for 5 s and invalidated on write. It's append-only; the latest state is the highest `consent_id` per scope.
- Account deletion writes `deletion_ledger` plus a host JSONL (`./ledger`, mounted at `/ledger`). `scripts/restore.ps1` replays it after any restore; the app refuses to start while `ops_state.restore_in_progress` exists.
- The LINE webhook **always** requires a valid `X-Line-Signature`. With an empty `LINE_CHANNEL_SECRET`, every webhook call returns 401.
- `APP_ENV=prod` makes startup fail on the default `SECRET_KEY`, an empty LINE secret, or empty legal fill-ins (`app/startup_checks.py`). `dev` (the default) only warns.

### Reachy robot (Phase 2): one process, two listeners
- `python -m app.serve` (the Dockerfile CMD) runs **two uvicorn servers in one process**, sharing one lifespan, one scheduler, and one in-memory `MonitorRegistry`: public `:8000`, plus device `:8001` for `/api/device/*`. Port 8001 is never published or tunnelled. The middleware in `main.py` returns 404 for device routes on 8000, 404 for anything else on 8001, and 401 for device tokens (`rdv1.` prefix) on 8000. `uvicorn app.main:app` alone gives you **no** device API.
- `reachy_bridge/` is the robot-side SDK (its own container, `docker compose --profile reachy up -d reachy-bridge`). It is a client of the device API only. It never decides a dose was taken; the server records under policy.
- The server enforces recording policy:
  - `MonitorSession` never auto-commits while `degraded` (< 12 fps, a gap > 0.25 s, or < 1 s of history), in `observe` mode, or when `auto_commit` is false.
  - Device sessions get `auto_commit = device.auto_record AND dose supported` (`dose_form='solid_oral' AND units_per_dose=1`).
  - `reachy_device.auto_record` **defaults to FALSE** until decision D2 (recording without pill identification) is made.
- Unverifiable doses go to `pending_confirmation`, and a caregiver answers with LINE buttons (a postback HMAC-signed with `SECRET_KEY`; the sender must be that verified contact). `transition_intake` refuses to change a `pending_confirmation` dose under the row lock. Confirmations expire after max(missed window, 2 h).
- **Every LINE push added from Phase 2 on goes through `notification_outbox`** (`app/services/outbox.py` to enqueue; `outbox_dispatcher` delivers with `X-Line-Retry-Key`). Don't call `LineService.send_*` directly for new notifications.
- Withdrawing `robot_camera` consent (or unpairing) calls `reachy_tasks.revoke_devices` in the same transaction.

### ML services are singletons warmed at startup
`main.py` lifespan calls `get_instance()` on FaceRecognition, Emotion, Line, and IntakeDetection. Endpoints then check the **class attribute** `_available` *without* calling `get_instance()` (e.g. `api_monitor.start` returns 503 if either is False). **If you add a service, warm it in lifespan** or its endpoints will 503 forever.

### DB — asyncpg, no ORM
- Raw SQL only. `schedule_time` and `prescription_meta` are JSONB — `json.dumps()` before passing to asyncpg.
- `"user"` is quoted everywhere (PostgreSQL reserved word).
- Dev runs a **local `postgres:16-alpine` container** (`postgresql://…@postgres:5432/medcareai2`), not Supabase. Production points at Supabase direct (5432), not the pooler (6543). Query latency differs by ~100× between these — re-measure before assuming a DB step is cheap.

### Navigation in React SPA
Use `window.location.href = '/path'` for auth transitions (login, logout, post-registration); React Router `navigate()` only for non-auth changes. Reason: `navigate()` doesn't re-run mount `useEffect`s, so `onboardingComplete` from localStorage goes stale.

### Logout — clear all localStorage flags
`face_auth_session`, `face_auth_token`, `onboarding_complete`, `onboarding_face_done`.

## Dependencies — smaller than you'd expect
`requirements.txt` is 19 lines. The complete installed ML stack is **`openvino`, `onnxruntime`, `opencv-python-headless`** (+ numpy/scipy/pillow). Image size: **1.67GB**.

- **No `torch`** — not installed, not in requirements, not imported anywhere in `app/`. The emotion model is ONNX (MobileNetV3-Large, 4.21M params, 112×112 input).
- **No `mediapipe` server-side** — it's a client npm package (`@mediapipe/tasks-vision`).
- **No `ultralytics`** — lazy-imported by OCR only, and absent, so OCR is dead.

The Dockerfile installs `requirements.txt` and nothing else.

## Known Issues
- **Static assets aren't cached.** `serve_spa` (`main.py:101-109`) returns a bare `FileResponse`: no `Cache-Control`, and conditional requests return **200 + full body instead of 304** (verified). `HEAD` returns 405. The MediaPipe assets under `/wasm` and `/models` total **25.6MB** and can be re-downloaded every session — negligible on localhost, ~10s per session on a tablet over WiFi.
- **MediaPipe loads lazily on button press**, and only *after* the camera opens (`Intake.tsx:354`), adding ~0.6s (warm) to ~1.5s (cold) to startup. The three models also load sequentially rather than in parallel (`monitorWorker.ts:23-34`).
- **GPU→CPU delegate fallback re-runs the entire 3-model init** (`monitorWorker.ts:53-57`), doubling init time on hardware without WebGL2.

## Common Gotchas
1. **`face_auth_token` expired?** 8h TTL. Regenerate inside the container:
   `python3 -c "from itsdangerous import URLSafeTimedSerializer; print(URLSafeTimedSerializer('change-me-in-production-32chars!!').dumps({'u_id':1,'name':'Ferdinan'}))"`
2. **asyncpg `expected str, got dict`?** Wrap the dict in `json.dumps()` (JSONB column).
3. **React page shows wrong user?** Supabase and face sessions conflict — clear `sb-*-auth-token` from localStorage when testing face auth.
4. **Models not loading?** Check `/health`. OpenVINO IR files must be at the volume path in `config.py`.
5. **Never measure HTTP transfers with PowerShell `Invoke-WebRequest` (PS 5.1).** It reported 24.5s for assets `curl` fetched in 0.12s — a ~200× overstatement from response buffering. Use `curl` via the Bash tool for any timing work.

## Key Env Vars
| Variable | Purpose | Default |
|----------|---------|---------|
| `DATABASE_URL` | Postgres connection | local `postgres` container |
| `SECRET_KEY` | Signs face auth tokens | `change-me-in-production-32chars!!` |
| `EMOTION_MODEL_PATH` | seed43 ONNX model | `models/emotion_seed43/model_fp32.onnx` |
| `OLLAMA_URL` | OCR only — unused while OCR is broken | `http://host.docker.internal:11434/api/generate` |
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE Messaging API | empty |
| `LINE_CHANNEL_SECRET` | Webhook signature, **mandatory** (unsigned → 401) | empty |
| `APP_ENV` | `dev` warns / `prod` refuses unsafe config | `dev` |
| `TERMS_VERSION` | Current legal notice version | `2026-10` |
| `OPERATOR_NAME`, `OPERATOR_CONTACT`, `TUNNEL_PROVIDER` | Filled into the legal notice; required in prod | empty |
| `BACKUP_PASSPHRASE` | Encrypts weekly backups (`backup` service) | empty |

## Tests
- Robot bridge: `…\.venv-test\Scripts\python -m pytest reachy_bridge/tests -q -p no:cacheprovider` (no robot, MediaPipe, or network needed).
- End-to-end checks against the running stack live in `D:\medcareai2_20260920\spike\`. Phase 2 must run inside the container, because port 8001 isn't published: `docker compose cp ../spike/e2e_phase2.py app:/tmp/ && MSYS_NO_PATHCONV=1 docker compose exec -T -w /app app python /tmp/e2e_phase2.py`. Without `MSYS_NO_PATHCONV=1`, Git Bash rewrites `/app` into a Windows path.
- Backend: `python -m pytest tests -q`. Tests use fake pools and stub `asyncpg`. The Microsoft Store Python on this machine lacks the app deps; a ready venv is at `D:\medcareai2_20260920\spike\.venv-test` (`…\Scripts\python -m pytest tests -q -p no:cacheprovider`).
- Frontend consent flows: `frontend_source/tests/consent.spec.ts` (API fully mocked). `playwright.config.ts` points at a non-existent `../playwright_test`, so run it with a config whose `testDir` is `./tests` and whose `webServer` is `npx vite preview --port 8000`.
- `Settings.tsx` has a pre-existing `react-hooks/immutability` lint error (`fetchSettings` used before it's declared).

## LINE Notifications
- Webhook URL changes on every ngrok restart (free tier) — update the LINE Developer Console each time.
- Test: `POST /api/notify/missed-dose?u_id=1&med_name=Aspirin&scheduled_time=08:00`
- Status: `/api/notify/status` → `{"configured": true/false}`
