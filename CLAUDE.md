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

App: http://localhost:8080 · Health: http://localhost:8080/health. The host port is `WEB_PORT`, default 8080, because the Reachy Mini daemon owns 8000. The robot's device API is host port 8001, bound to `DEVICE_BIND`.

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
- Limited-mode (exempt) routes: `/api/consent/*`, `/api/legal/*`, `/api/account/export`, `/api/account/delete`, `/api/auth/onboarding-status`, `GET /api/memory`, `DELETE /api/memory`, `DELETE /api/memory/fact`, plus the open login/register/identify/webhook routes. `tests/test_consent_enforcement.py` checks method + path for the memory routes (`POST`/`PATCH` on the same paths stay consent-gated).
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
- **The robot streams frames; the server computes the landmarks.** The robot's Pi 4 reached only ~3 fps running the six face/hand/pose models itself, below the 12 fps recording gate. `POST /api/device/monitor/frame` takes one JPEG plus its capture `timestamp` (seconds, robot clock). `LandmarkService` (`services/landmark_service.py`, code in `services/landmarks/`, ported from the robot app's `vision.py`) computes the packet and feeds `registry.landmarks`. Identity and emotion run on the same frames in the background every 0.5 s, or at once while a candidate waits. Each session keeps its own trackers (`MonitorSession.vision_engine`), and its frames are processed in order under `frame_lock`. Measured at about 29 ms per frame on this laptop.
- The six landmark ONNX files live in `models/landmarks/` and are committed, like the emotion model (`.gitignore` excludes `*.onnx` except these). They're pinned by SHA-256, and `/health` reports `"landmarks"`.
- The robot app can listen for 「我吃完了」 ("I've finished") with SenseVoice on the robot, but only while the `robot_microphone` consent is current. The server sends `microphone` in the task payload and every heartbeat. It's a claim, not evidence: if the camera resolves nothing within 8 s, the robot files a `patient_claim` confirmation.
- The robot app's source is in `reachy_app/` (published as the Hugging Face space `pearlyjam21/medcare_reachy`). `reachy_app/tools/deploy_to_robot.py` copies it onto the robot. After deploying, restart the app through the daemon (`POST /api/apps/restart-current-app`): saving settings restarts only its worker thread, and the process keeps running the old code.

- **Two ways a robot can see.**
  - **Server vision** (robot app 0.4.0 default, `vision_on_server=true`): the robot streams frames to `/monitor/frame` and the server computes landmarks *and* emotion (`MonitorSession.vision_engine` is set).
  - **On-robot vision** (`vision_on_server=false`, and `reachy_bridge/`): the robot app runs the landmark models (MediaPipe models converted to ONNX, on ONNX Runtime; the MediaPipe library can't load on the robot's Pi 4) **and the emotion model**. That session's JPEGs (≤ 2 fps) are identity-only: `vision()` skips `EmotionService` for reachy sessions *without* a `vision_engine`. Emotion arrives as an optional `emotion: {face_index, probabilities}` on `/api/device/monitor/landmarks` and is accepted only for the verified, owned face with an uncovered mouth (`_accept_robot_emotion`).
  - `public()` exposes `target_box` so the robot knows which face to score. The device API no longer requires `EmotionService` to start a session.

### Reachy check-in conversations (demo, Oct 2026)
- **Consent gate.** Check-ins need all four scopes `robot_microphone`, `cloud_voice`, `conversation_analysis` and `safety_alerts` (`reachy_tasks.checkin_allowed`). The task payload's `checkin` flag carries this to the robot, and every `/api/device/conversations*` call re-checks it (403 `checkin_consent_required`). In the UI, the Reachy card's "Daily check-ins" switch grants or withdraws them.
- **Flow.**
  - The robot turns speech into text on the robot (SenseVoice, `voice.py` in "chat" mode).
  - `POST /api/device/conversations` → `/{id}/turn` → `/{id}/end` sends text only.
  - The server calls OpenRouter (`services/conversation.py`, model `LLM_MODEL`, default `openrouter/free`) and returns `reply` (Traditional) plus `speech_text` (Simplified, via `zhconv`, for the robot's Matcha voice).
- **When the model is unavailable:** no key, a 429, or an empty answer gives a fixed reply, so the demo never stalls.
- **Safety.** `conversation.screen()` runs before any LLM call. A risk word means:
  - the words never reach the model
  - a fixed help-line reply (119 / 1925)
  - the conversation ends
  - a `safety_alert` goes through the outbox to *every* verified contact (`contact_flag=None`)
  - the turn is `flagged`
  
  Keyword matching is crude, and `NOT_RISK` lists everyday phrases to ignore. Alerts don't repeat yet, although the robot notice §6 promises that they do.
- **When it happens:** after the last dose of a slot (state `CHECKIN`, then `POST_SLOT_OBSERVE`), or as a conversation-only task (`reason='checkin'`, no doses; `POST /api/reachy/checkin`, the "Talk to Reachy now" button).
- **Storage.** Tables are `conversation` and `conversation_turn`. `jobs/conversation_retention_job.py` deletes turns after 30 days (flagged ones after 180); summaries and mood stay until the patient deletes them. The patient views them at `/conversations`, with a dashboard section, through `/api/conversations`.
- **Risk chats never reach a model again.** After a risk turn, any further turn gets the fixed help-line reply, and post-chat work makes no model call (summary NULL, mood `unknown`).

### Check-in memory (Oct 2026)
Plan: `docs/superpowers/plans/2026-10-03-reachy-conversation-memory.md`.
- **Consent.** Its own legal kind `memory` (`app/legal/memory/<version>/`), scope `conversation_memory`, off by default. The robot notice and `TERMS_VERSION` were not changed for it; every future `TERMS_VERSION` needs a memory notice too, because `load_documents()` loads every kind. Memory is on only when `core`, `cloud_voice`, `conversation_analysis` and `conversation_memory` are all current (`memory.MEMORY_SCOPES`). It is not in `CHECKIN_SCOPES`.
- **Data.** `patient_memory`: one row per fact per conversation (UUID ids, never reissued after a restore); the newest row per `(kind, subject)` wins, and a patient-entered row (`source='patient'`) beats any chat row. Kinds: `name` (patient entry only, never from speech), `person`, `like`, `routine`, `event` (needs `event_date`). No health, medicine or care facts, for anyone. `patient_memory_deleted` tombstones block re-learning for 7 days.
- **Write path** (`services/after_chat.py`, from `/end` and the 10-minute sweep `jobs/after_chat_job.py`): one model call writes the summary and the facts; facts pass `memory.validate_fact()` and must appear in the patient's own words (`grounded`); they are stored under `memory.lock_user()` (`SELECT … FROM "user" … FOR UPDATE`, the same lock deletes and patient edits take) after an uncached consent re-read. No model call for a risk chat or when check-in consent was withdrawn. A rate limit leaves the chat `pending` for the sweep.
- **Read path.** Every turn rebuilds a facts-only block (`memory.build_block`) sent as a second system message; no summaries or moods (a visitor may be listening). The opening line gets a name only from the patient's own entry. One past event per chat is chosen at start (`conversation.followup_memory_id`) and marked asked only after a real model reply.
- **Deletion.** Chat deletes cascade to their facts; every patient delete and every withdrawn consent scope writes `deletion_ledger` (kinds `conversation`, `memory`, `consent`), replayed by id + owner (consent: only if the restored grant is older than the withdrawal). `replay_ledger` runs the retention purges before clearing the restore marker.

### Schedules, stock, adherence (Oct 2026)
- **Schedules** live in `services/schedule.py`. `medication.schedule_time` holds the four preset booleans (08:00, 12:00, 20:00, 22:00), plus optional `custom_times` (`"HH:MM"`, at most 8 times a day in total) and `weekdays` (ISO 1 = Monday … 7 = Sunday; missing means every day). Rows are generated 30 days ahead, or until `use_before`. Nothing tops them up yet, so a schedule simply ends 30 days after its last edit.
- **Stock is `NUMERIC(8,2)`** (half tablets).
  - Every taken dose removes `units_per_dose` through `intake_repository.take_stock`, and stores the amount in `intake.units_taken`.
  - Undo calls `return_stock` with that stored amount. Rows taken before the column existed restore 1.
  - Don't write `pills_remaining-1` anywhere.
  - Refills go through `POST /api/medications/{id}/supply`, which logs them in `medication_supply`.
  - The refill alert fires at ≤7 **days** of supply (`schedule.supply`).
- **Archive, don't delete.** `POST …/archive` stops a course: future pending rows go, history stays, and the missed-dose job skips inactive medications. `…/reactivate` regenerates from now. `DELETE` returns 409 `has_history` unless the medication never had a past or resolved dose.
- **Adherence** follows one rule (`services/adherence.py`), used by `/api/history/summary` and the weekly LINE summary:
  - Only due doses count. Future doses never do, and a pending dose counts only once it is more than 1 h overdue.
  - Only `taken` is adherent; awaiting confirmation is not, yet.
  - The streak skips days with no doses.
  - `/api/history/intakes` is past-only and paged (`{items, total, has_more}`).
- **Browser auto-record** follows the robot's rule: one `solid_oral` unit, otherwise the person confirms.

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
- **Channel:** "Care Bot" (`@331ealnq`). Its QR is `frontend_source/public/line-bot-qr.png` (committed; `.gitignore` excludes `*.png` except this one). Keys go in `.env` as `LINE_CHANNEL_ACCESS_TOKEN` and `LINE_CHANNEL_SECRET`.
- **Webhook:** `scripts/line-tunnel.ps1` starts the `line` compose profile:
  - `line-webhook-proxy` (nginx) forwards **only** `POST /api/notify/webhook/line` and returns 404 for everything else.
  - `line-tunnel` is a Cloudflare quick tunnel to that proxy.
  
  The script then sets LINE's webhook URL through the Messaging API and runs LINE's webhook test. The rest of the app is never exposed. Never tunnel port 8080 directly: the legacy routes and the SPA path handling are unsafe on the internet (see the security review).
- **The quick-tunnel address changes whenever the tunnel container restarts** (reboot, Docker restart). Rerun `scripts/line-tunnel.ps1`, which takes about 30 s and is idempotent.
- Test: `POST /api/notify/missed-dose?u_id=1&med_name=Aspirin&scheduled_time=08:00`
- Status: `/api/notify/status` → `{"configured": true/false}`
