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
- **Voice clips** are WAVs rendered on the robot with Matcha (`~/.medcare_reachy/clips/zh-TW/`). The deploy renders a clip again when its zh-TW text in `bridge/clips/manifest.json` changed or its WAV is missing (`rendered.json` there records what each WAV says; the deploy writes it before uploading the new manifest, so a failed render is retried next time). The deploy fails when a planned clip was not written, and warns when SenseVoice does not hear a clip exactly as written. The voice reads Simplified Chinese, so a manifest edit also needs the clip's Simplified words in the tool's `SPOKEN` table; `reachy_app/tests/test_deploy_tool.py` fails until it has them.
- **The robot's camera tops out at 10 fps unless the daemon is changed.** Reachy Mini's daemon (reachy_mini 1.11) shares the camera with apps at `IPC_FPS = 10` (`/venvs/mini_daemon/.../reachy_mini/media/media_server.py`), whatever the app asks for. The stream then stays below the server's 12 fps, so every robot dose goes to family confirmation as `degraded` (seen 2 Oct 2026: `landmark_fps` 10.1 on every dose). **Deploying robot app 0.5.1 alone does not change that.** `deploy_to_robot.py --camera-ipc-fps 15` edits the constant and keeps a backup; it applies after a daemon restart, done by hand when no reminder is running. A daemon update undoes it. The stream rate and the daemon's and app's CPU at 15 fps have not been measured yet. The app's settings page shows "Camera feed" fps, and the app logs a warning when the camera stays below 12 fps.
- Robot app 0.5.1 streams in step with the camera rather than on a timer, and encodes each 1280x720 frame to 480x360 in one pass (~12 ms on the Pi, down from ~38 ms). Its speech listener runs at nice 5: decoding the patient's 「我吃完了」 coincided with the stream falling from 10 to 6-7 fps for ~3 s.
- **Stream stalls of ~1 s have an unknown cause.** On 2 Oct 2026 the robot's stream stopped for ~1 s at a time, then got 4-6 answers within 0.2 s. It happened in every slot state, mostly with the speech listener off, so it is not decoding. The robot's Wi-Fi power save is on (untested lead). A stall while WATCHING makes that dose `degraded` even at 15 fps. 0.5.1 logs each gap over 0.25 s (`server stream: ... between frames`, with the camera wait, event-loop lateness, and frames dropped behind unanswered requests). The server logs any frame it held up over 0.25 s (`monitor frame N of session ... took`).

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
- **Safety: three layers**, because each one alone has missed real risk. On 2 Oct, 「我想自残」 raised no alert: the speech-to-text writes Simplified, and the keyword list had no 自殘.
  1. **Keywords.** `conversation.screen()` runs before any LLM call, on text converted to Traditional with spaces and punctuation stripped. `RISK_WORDS` are plain phrases; `RISK_PATTERNS` add guards (`不想活` but not `不想活動`), the speech-to-text sound-alikes, and the verb a word needs (`喝農藥`, not `噴農藥`). `NOT_RISK` removes everyday phrases (`想死你了`, `我還不想死`, `跳樓大拍賣`), each only within the words between two punctuation marks, so `我想死，你不要管我` still matches. During an OpenRouter outage this list is the **only** check that runs, so it must catch explicit statements by itself; but a match alerts family and ends the chat, and the check-in starts right after a dose, so entries must be unambiguous (`我把早上的藥全部吃了` and `一次吃了五顆` are everyday news; counts are left to the model). A matched turn never reaches the reply or risk-check model, and a flagged conversation gets no post-chat model call (summary NULL, mood `unknown`).
  2. **The model.** `conversation.classify_risk()` judges every other turn: one short OpenRouter call (`RISK_PROMPT`, label `SELF_HARM` / `OVERDOSE` / `NONE`, 4 s deadline). It runs *alongside* the reply, so a turn waits for the slower of the two, not both. Goodbye and turn-limit turns, which get a fixed line, are judged too. `parse_risk` reads a negated label (`No SELF_HARM`, `SELF_HARM: no`) as not understood. A failed or timed-out judgement (`risk_result: "unknown"`, never a risk) is retried in the background with 30 s (`api_device._late_risk_check`), 20 s later when the first try got a 429. A risk it finds flags the turn and alerts, and the conversation's next turn gets the help line without calling the model (`risk_flag` is already set). A retry that fails too is logged as an ERROR. The retry lives only in memory; after a restart, the summary is what remains. Post-chat work waits (bounded) for a conversation's retries still in flight, so a late flag also means no post-chat model call.
  3. **The summary.** `SUMMARY_PROMPT` asks for a third line, `RISK: none|self_harm|overdose` (read with a full-width colon, bold or quotes too; an answer without it goes to the fallback model). With memory on, the combined `AFTER_CHAT_PROMPT` carries the same line (see Check-in memory). Post-chat work has one owner, `services/after_chat.py`, run from `/end` and from the 5-minute after-chat sweep (`jobs/after_chat_job.py`), up to 3 attempts per conversation. When it summarises a conversation that is not yet flagged, a risk alerts family quoting the summary sentence (unless Reachy had memory notes in that chat, see Check-in memory), and the alert says it comes from the summary. If saving the summary fails, the alert is sent on its own. An attempt that gets no RISK judgement (no model answered, or none wrote a RISK line) saves nothing, mood stays NULL, and the sweep tries again (it also catches post-chat work a restart lost). A rate limit leaves the chat `pending` without using up an attempt, but only for its first 10 minutes, so under a lasting rate limit family hear within about half an hour. After the last attempt without a judgement, an unflagged conversation gets a `safety_check_incomplete` family notification in the app (any summary that attempt had is kept). So does one whose last attempt never finished or could not save its results: each claim sets the chat `pending` until the attempt writes its state, the sweep picks up `pending` chats at any attempt count, and `after_chat` ends them without another model call. A conversation still unfinished 2 days after it ended is ended the same way by the sweep (`after_chat.end_stale`).

  A risk from layer 1 or 2 means a fixed help-line reply (119 / 1925), the end of the conversation, and a `flagged` turn. All three layers alert through `conversation.alert_family()`: a `safety_alert` to *every* verified contact (`contact_flag=None`), or a `safety_alert_undelivered` notification when there is none. It sets `risk_flag` with `... AND NOT risk_flag RETURNING`, so a conversation alerts **once**, whichever layers fire. Alerts don't repeat yet, although the robot notice §6 promises that they do. Notice §3 already lists the LLM service for "summaries and safety screening".
- **When it happens:** after the last dose of a slot (state `CHECKIN`, then `POST_SLOT_OBSERVE`), or as a conversation-only task (`reason='checkin'`, no doses; `POST /api/reachy/checkin`, the "Talk to Reachy now" button).
- **Storage.** Tables are `conversation` and `conversation_turn`. `jobs/conversation_retention_job.py` (daily, `run_retention`) deletes turns after 30 days (flagged ones after 180); summaries and mood stay until the patient deletes them. The patient views them at `/conversations`, with a dashboard section, through `/api/conversations`. The after-chat sweep (`jobs/after_chat_job.py`, every 5 min) closes conversations with no turn for 15 min (`end_reason='abandoned'`, e.g. the robot was switched off mid-chat; measured from the last turn, so a live chat is never closed) and does their post-chat work in the same run, along with any ended conversation whose post-chat work is still `pending` or `failed` after 1 min, for up to 2 days (`after_chat.RETRY_DAYS`; older ones: `end_stale`).
- **Risk chats never reach a model again.** After a risk turn, any further turn gets the fixed help-line reply, and post-chat work makes no model call (summary NULL, mood `unknown`).
- **Model calls.** `services/conversation.py` sends `"reasoning": {"enabled": false}` (OpenRouter's free models are reasoning models and otherwise return empty replies), tries `LLM_MODEL`, then `LLM_FALLBACK_MODEL`, and gives up after `LLM_DEADLINE_SECONDS` (8). The pinned `inclusionai/ling-3.0-flash-sante:free` answers in about 1.5 s. Every call also carries its temperature (0 for the memory extraction, 0.7 otherwise) and, when set, the provider routing (see Check-in memory).
- **What Reachy knows about the day** (`services/context_info.py`). `conversation.reply_prompt()` adds three lines after `SYSTEM_PROMPT`, introduced by `BACKGROUND_RULES`: use them only when asked or relevant, never recite them, never guess weather, never offer to look anything up, and "the rules above still apply". The rules don't name medicine again: repeated right before the data, that made the model send the patient to a doctor or pharmacist about the weather. The risk check, the summary and the post-chat memory call never get the lines.
  - The lines:
    - Date, weekday and lunar date in `MEDCARE_TIMEZONE`, and the time as it is said: 凌晨/早上/中午/下午/晚上/深夜 plus a 12-hour 2點05分. Written 深夜 02:05, the model said it was just past midnight.
    - Weather now, today and tomorrow, without the place name (the robot notice says only conversation text goes to the model).
    - Today's holidays and the next two dates within 14 days. A 補假 goes inside its holiday's entry (「10月10日國慶日，還有7天（10月9日星期五補假）」); listed first on its own, it made the model say 國慶日 was 6 days away.
  - Holidays: Taiwan's official days off with their 補假 (`holidays`, which labels 除夕前一日 as 農曆除夕; `days_of` renames it, and calls the make-up days of both 春節補假), plus 元宵, 七夕, 中元, 重陽, 冬至, 尾牙 and the family days (`lunar_python`). Tests pin 2026 and 2027 to the DGPA calendars (120 and 121 days off). Check the next year's calendar (published around May) after upgrading `holidays`.
  - **No reply waits on the network.** The `checkin_background` scheduler job runs every 30 min, and once at startup in a worker thread. It has no misfire limit: with APScheduler's default 1 s, a busy startup skipped that first run.
    - It fetches Open-Meteo into memory (no key; only `WEATHER_LATITUDE`/`WEATHER_LONGITUDE` are sent, default 台北; `WEATHER_PLACE` is for the logs).
    - It also builds the holiday tables ahead: the first build takes ~0.3 s, too long for the event loop the robot's frames share.
    - A forecast older than 3 h is dropped, and so is the current temperature after 1 h. The line then says the weather is unknown and suggests looking outside or asking family (`NO_WEATHER`). Asked directly, the model then says it isn't sure. It can still make weather up when weather only comes up indirectly (出門散步: 2 of 13 in a review), as the plain prompt does.
    - `WEATHER_ENABLED=false` or an empty coordinate turns the weather off.
  - Dependencies are in `requirements-context.txt`, which has its own Docker layer after `requirements-video.txt`. Until the image is rebuilt with them, the container test command below needs `-r requirements-context.txt` added to its `pip install`.
- **Timings.** `conversation_turn.metrics` (JSONB, ms) holds how the robot heard each patient turn (`robot.vad_release_ms`, `stt_ms`, `handover_ms`) and how each reply was made and played (`server.llm_ms`, attempts per model; `robot.round_trip_ms`, `tts_first_audio_ms`, `tts_total_ms`). The risk check is on the same Reachy turn: `server.risk_ms`, `risk_result`, `risk_attempts` (kept apart from `attempts`, so the reply statistics stay about replies), and `risk_source` (`keyword` / `model` / `earlier` / `late_model` / `none`). A late check adds `late_risk_*` keys. Robot app 0.5.0 sends them with the turn and posts playback to `/conversations/{id}/turns/{turn_id}/metrics`. `/api/conversations/metrics/summary` gives median and p90 per stage, `/metrics/turns` the rows (no words) for the page's CSV download.

- **Robot app 0.5.3 (not yet deployed): 「嗯」, thinking phrase, inquiring3 and gestures.**
  - **The 「嗯」** (`ack` clip) still answers the pause.
  - **Thinking phrase:** once the turn is handed over, `voice.DoneListener` waits `THINK_AFTER_SECONDS` (0.5 s). Then, unless the patient is speaking again or Reachy is already answering, it plays one of the `thinking` variants: 「我再想一下喔。」「讓我想一想喔。」「我想想看喔」 (shuffled, via `ClipPlayer.think_aloud`).
  - **When it doesn't play:** never over a turn still being collected, because an earlier version that played the phrase straight away lost 活 from 「我不想…活了」. A cough gets only 「嗯」.
  - **Echo:** the phrase's echo is removed only at the start of speech inside the echo window (`voice.without_filler`, `THINKING_FORMS`). The server's `screen()` also checks each turn with those forms removed.
  - **Thinking motion:** the vendored `bridge/moves/inquiring3.json` (Pollen emotions library, rev 873ae49), played as offsets at 50 Hz. Never use the SDK's `play_move` or `cancel_move`, because `cancel_move` stops the app's own audio.
  - **Gestures:** they start at neutral when the robot is near it, ease back over 0.9 s from a moderate offset when the motors are known to be on, and are refused otherwise. The limits `NEUTRAL_TOLERANCE` / `RECENTER_LIMIT` in `media.py` are provisional until the robot's 'gesture from the pose measured' logs are read.
  - **Deploy check:** the deploy keeps a phrase only if SenseVoice hears it as recognisable echo, and fails when any clip has no WAV.
  - **Switches:** settings `checkin_ack` and `checkin_gestures` on port 8042.
- **Robot app 0.5.3: overdose-protection refusals.** A 409 whose `detail` is `dose_not_due_yet`, `dose_too_soon`, `daily_max_reached` or `dose_expired` (`app_client.DoseRefused`) on monitor start, a restarted session, the confirmation request or the frame stream is said once per dose (its `speech_text`, live Matcha synthesis, through `_say` with the listener muted), only to a patient seen verified within 90 s or who just said 「我吃完了」. The dose's outcome becomes the code, nothing more is filed (the server alerts family itself) and Reachy moves on; the dose is never retried in that task. Without the check-in TTS models on the robot, or with no `speech_text` (an older server), it stays silent and moves on. Deploy with `reachy_app/tools/deploy_to_robot.py`, then restart the app through the daemon; heartbeats then report `bridge_version` 0.5.3.
- **Backup model:** `LLM_FALLBACK_MODEL` is pinned in `.env` (`apodex/apodex-1.1-mini:free`). `openrouter/free` once routed a reply to a safety-classifier model whose "User Safety: safe" `usable_reply` now rejects.

### Check-in memory (Oct 2026)
Plan: `docs/superpowers/plans/2026-10-03-reachy-conversation-memory.md`.
- **Consent.** Its own legal kind `memory` (`app/legal/memory/<version>/`), scope `conversation_memory`, off by default. The robot notice and `TERMS_VERSION` were not changed for it; every future `TERMS_VERSION` needs a memory notice too (and a video notice), because `load_documents()` loads every kind. Memory is on only when `core`, `cloud_voice`, `conversation_analysis` and `conversation_memory` are all current (`memory.MEMORY_SCOPES`). It is not in `CHECKIN_SCOPES`. Withdrawing it deletes no notes on the server (memory notice §5); the UI offers the delete through `DELETE /api/memory`.
- **Data.** `patient_memory`: one row per fact per conversation (UUID ids, never reissued after a restore); the newest row per `(kind, subject)` wins, and a patient-entered row (`source='patient'`) beats any chat row. Kinds: `name` (patient entry only, never from speech), `person`, `like`, `routine`, `event` (needs `event_date`). No health, medicine or care facts, for anyone. `patient_memory_deleted` tombstones block re-learning for 7 days. The daily retention job deletes event notes 30 days after their date and tombstones after 7 days.
- **Write path** (`services/after_chat.py`, from `/end` and the 5-minute after-chat sweep `jobs/after_chat_job.py`): one model call (`AFTER_CHAT_PROMPT`) writes the summary, the mood, the `RISK:` line and the facts. A chat without memory consent, or too short (fewer than 2 patient turns or 15 characters), gets the plain `SUMMARY_PROMPT` call instead, so the RISK backstop (safety layer 3) runs for every unflagged chat, memory on or off.
  - Facts are stored only from an answer with an explicit `RISK: none`. An answer without a RISK line is no judgement: nothing is saved and the sweep retries; the last attempt keeps its summary (never facts) and sends `safety_check_incomplete`. `parse_summary` and `risk_line` read the same RISK line, skipping the format echoed back (`RISK: none|self_harm|overdose`).
  - A summary risk alerts family **without the summary quote** whenever Reachy may have had memory notes in that chat, whichever prompt wrote the summary (memory notice §3: family never see the notes): memory on now, or its consent changed after the chat started, and the patient has any note; or the chat had a follow-up. Reachy's lines (the block, the named opening) can carry notes into a summary even on the short-chat `SUMMARY_PROMPT` path.
  - Facts pass `memory.validate_fact()` (which rejects anything `conversation.screen()` matches, checking the subject with its `_` read as spaces too) and must appear in the patient's own words (`grounded`); they are stored under `memory.lock_user()` (`SELECT … FROM "user" … FOR UPDATE`, the same lock deletes and patient edits take) after an uncached consent re-read, and only while the conversation's `risk_flag` is still unset.
  - Before claiming a conversation, `after_chat` waits (bounded) for its late risk checks still in flight, so a late flag means no post-chat model call.
  - No model call for a risk chat or when check-in analysis consent was withdrawn. A rate limit leaves the chat `pending` for the sweep.
- **Read path.** Every turn rebuilds a facts-only block (`memory.build_block`) sent as a second system message after the reply prompt (`SYSTEM_PROMPT` plus the day's background); no summaries or moods (a visitor may be listening). Only the reply gets it, and only on an unflagged, non-keyword, non-closing turn: the risk check, the summary and family alerts never see it. It is built in a savepoint: if it fails, the reply goes without it and the turn (its words, its risk check) is unaffected. The block's preamble repeats "the rules above still apply" (no medical advice); on 3 Oct both pinned models took the two system messages (HTTP 200, ~1.5-2 s) and still sent a dose question to a doctor or pharmacist. The opening line gets a name only from the patient's own entry. One past event per chat is chosen at start (`conversation.followup_memory_id`) and marked asked only after a real model reply.
- **Deletion.** Chat deletes cascade to their facts; every patient delete and every withdrawn consent scope writes `deletion_ledger` (kinds `conversation`, `memory`, `consent`), replayed by id + owner (consent: only if the restored grant is older than the withdrawal). `replay_ledger` runs the retention purges before clearing the restore marker.
- **Provider routing** (optional): `OPENROUTER_PROVIDER_ONLY` and `OPENROUTER_DATA_COLLECTION` go into every OpenRouter call (reply, risk check, summary, memory) for both models, so with `OPENROUTER_PROVIDER_ONLY` set, `LLM_MODEL` and `LLM_FALLBACK_MODEL` must both be served by those providers. Left unset on the laptop: untested, they can make the fallback unroutable or exclude the free endpoints.

### Emotion during medication (Oct 2026)
- **What is scored.** Every dose session scores the 7 FER classes (`emotion_service.LABELS`) on the verified owned face. This includes moments when the mouth is covered, which are marked `occluded`: synthetic tests showed covered faces shift results toward surprise and angry. Robot server-vision sessions score 4 times a second once the patient is verified; identity checks stay at most 2 Hz.
- **One result per dose session.** `services/dose_emotion.py` writes one `dose_emotion` row per dose session, whatever the outcome: camera commit, confirmation request to family, patient claim, or left unresolved.
  - **What the result uses:** uncovered faces from `PRE_SECONDS` (5 s) before the intake event and `POST_SECONDS` (5 s) after it. Without an event it uses the last 120 s.
  - **"Unsure":** a result with fewer than 4 clear frames, or whose top class scores below `MIN_RELIABLE_SCORE` (0.4), is marked unsure.
  - **Consent:** core consent is re-checked before writing, plus robot camera consent for robot sessions.
  - **Idle sessions:** they are ended after `IDLE_SESSION_SECONDS` (10 min) by the minute job.
- **Unchanged:** the commit path's `during_ingestion` emotion row and `monitor_event.emotion_probabilities` still use uncovered faces since the intake started. That row is what the existing family emotion-alert job reads.
- **Where it shows.** `/api/emotion/medication` and `/api/emotion/medication/{intk_id}`. The `dose_emotion` field on `/api/medications/today` and on `/api/history/intakes` rows feeds the chip on Today, Schedule and History. The chip appears only for doses that were taken or are waiting for family, and shows "mouth covered" or "unsure" as visible text.
- **Not shared with family.** No LINE message carries it. The core notice §4 covers storing it ("facial-expression analysis results at medication time").
- **Still open:** a patient-side delete of per-dose results (core §7). Robots doing their own vision (`vision_on_server=false`) still skip covered mouths and score at 2 Hz.

### Dose videos for family (Oct 2026)
- **Opt-in.** Consent kind `video`, scope `dose_video`, notice `app/legal/video/2026-10/`. The Family page's "Dose videos for family" card shows the notice before switching on. It is the one exception to "camera images are never saved".
- **Capture.** While a monitor session of a consenting patient runs (`MonitorSession.clip_enabled`, checked at start), the JPEGs the server already receives (browser `/vision`, robot `/monitor/frame` and `/monitor/vision`) are also kept in memory for 20 s, at most 10 fps (`services/dose_video.py`). `commit_monitored` (any recorded dose) and the robot's `/tasks/{id}/confirmation` call `capture()`, which encodes the frames before that moment into H.264 with PyAV (`requirements-video.txt`, its own Docker layer) under `DOSE_VIDEO_DIR`.
- **With a confirmation request.** A clip of a dose sent to caregivers (`/tasks/{id}/confirmation`: degraded, uncertain, patient claim) goes out as soon as it is encoded, right after the request, whatever the frame rate (`dose_video.send_with_confirmation`). The request text also gives the AI's estimate (`services/dose_report.py`: `48%（不確定）`, or "not determined" when the camera saw nothing).
- **Sending.** `taken_confirmation_job` now goes through the outbox. Each dose line says how it was recorded and the AI's estimate (detector band → `82%（高）` etc., or "not determined" when the camera saw nothing), plus a footnote that the AI can't see the pill. Each clip goes as a separate `dose_video` outbox row, so a rejected video never blocks the text. Every recipient gets their own link, `/api/media/line/<43-char token>.mp4|.jpg` (only the token's SHA-256 is stored). The link is public (no login) and served with Range support by `api_dose_video.py`. The base URL is `PUBLIC_BASE_URL`, or else the LINE webhook's host if that tunnel answers.
- **Deletion.** The cleanup runs every 2 min. Files go 10 min after every recipient's app fetched the last byte or LINE sent `videoPlayComplete` (`trackingId` `dv-<link uuid hex>`); after 24 h in any case; at once on withdrawal (`api_consent` calls `delete_all`); and when never sent within 4 h, with no recipients, or with no public link. Rows stay as a record without media. LINE messages can't be recalled, so delivered videos stay on family phones.

### Schedules, stock, adherence (Oct 2026)
- **Schedules** live in `services/schedule.py`. `medication.schedule_time` holds the four preset booleans (08:00, 12:00, 20:00, 22:00), plus optional `custom_times` (`"HH:MM"`, at most 8 times a day in total) and `weekdays` (ISO 1 = Monday … 7 = Sunday; missing means every day). Rows are generated 30 days ahead, or until `use_before`. Nothing tops them up yet, so a schedule simply ends 30 days after its last edit.
- **Stock is `NUMERIC(8,2)`** (half tablets).
  - Every taken dose removes `units_per_dose` through `intake_repository.take_stock`, and stores the amount in `intake.units_taken`.
  - Undo calls `return_stock` with that stored amount. Rows taken before the column existed restore 1.
  - Don't write `pills_remaining-1` anywhere.
  - Refills go through `POST /api/medications/{id}/supply`, which logs them in `medication_supply`.
  - The refill alert fires at ≤7 **days** of supply (`schedule.supply`).
- **Overdose protection (「防止重複服藥」): per patient, on by default.** (On 3 Oct, test alerts at midnight recorded the 08:00, 12:00 and 20:00 doses within an hour.) The switch is `notification_settings.overdose_protection`, the field `overdose_protection` of `GET`/`POST`/`PUT /api/notify/settings` (left out of a save: kept). All four rules live in `services/dose_safety.py`; R1's arithmetic is `schedule.due_from`.
  - **R1, due:** from DOSE_EARLY_MINUTES (120) before its time, but never before halfway from the same medicine's previous dose.
  - **R2, minimum gap** from the same medicine's nearest *taken* dose, by actual intake time:
    - `medication.min_interval_minutes` (30-2880), else half the shortest gap between its daily times (across midnight; once a day: 12 h; allegra at 08/12/20/22: 60 min), else 240 min.
    - It applies to ad-hoc Take Now doses too.
  - **R3, daily maximum:** `medication.max_daily_doses` (1-24), else its number of daily times (unscheduled: no limit).
    - It counts taken doses *scheduled* on the dose's Taipei day (an ad-hoc dose: the day it was taken).
    - So a 22:00 dose taken at 00:30 counts for its own day, not the next.
  - **A dose waiting for family's answer counts as taken** for R2 and R3, at its open request's `created_at` (`FACTS_SQL`'s `last_taken_pending`). Every robot dose goes to family today (10 fps, `degraded`), and up to 2 h without an answer must not leave room for a second pill: Take Now, a tap or the next dose's robot request in that time is refused. Of two requests for one medicine, the earlier one counts against the later one.
  - **R4, expiry:** a dose not taken by halfway to the same medicine's next *scheduled* dose (for the last of a day: the next day's first) stays missed. Take Now then refuses (`dose_expired`) rather than make an ad-hoc dose.
    - **Ad-hoc rows** are told apart by their time: the schedule generates whole minutes, Take Now stores the moment it made the row, never a whole minute (`dose_safety.is_ad_hoc` / `ad_hoc_sql`). An ad-hoc dose never expires, and `next_sql` leaves ad-hoc rows out, so an abandoned or later Take Now moves no dose's halfway point.
  - **Where it is enforced:** `dose_safety.check(conn, u_id, ids, at=, lock=, started_at=, after_intake=)`, under the intake row lock.
    - Every path that starts a dose: robot tasks, monitor starts, the manual task, Take Now (ad-hoc rows included).
    - Every path that records it taken or sends it to family: `commit_monitored`, `transition_intake`, `dose_confirmation.create` / `resolve`, legacy `/medicines`. These pass `lock=True`: `LOCK_SQL` locks the medicines' rows **in a statement of its own, before** `FACTS_SQL` reads. (`FOR UPDATE` inside the reading statement does not work: under READ COMMITTED it keeps the snapshot from before it waited, and two doses of one medicine recorded at once both passed.)
    - **Evidence paths judge R1 and R4 when the camera session started** (`MonitorSession.started_at`; the robot's confirmation uses its open session for that dose): the dose was allowed then, so a pill swallowed a minute after the halfway point is still recorded and spaces the next dose. R2 and R3 are judged when the pill went down.
    - Queries that pick doses use the SQL forms: `startable_sql` (lease, manual task: R1 and R4 under the switch) and `open_sql` (Take Now and the legacy page: which dose a pill counts for). The lease looks only at a task's open doses: a taken dose past its halfway point doesn't hold back the slot.
  - **`FACTS_SQL` and the tests:** `FACTS_SQL` gathers what the rules need, as of the moment judged (`previous_time` leaves out an ad-hoc dose made after it). `tests/dose_facts.py` answers it for the tests' fake databases, so change both together.
  - **Refusal:** a `schedule.DoseRefused` (`DoseNotDueYet`, `dose_safety.DoseTooSoon` / `DailyMaxReached` / `DoseExpired`) → 409 (handler in `main.py`).
    - The body: `{"detail", "intk_id", "med_name", "scheduled_time"}`, plus `due_from` (not due), `last_taken_at`, `next_allowed_at`, `min_interval_minutes` and `last_taken_pending` (too soon), `next_allowed_at`, `taken_today` and `max_daily_doses` (daily maximum, the next Taipei day), or `expired_at`.
    - `reply` is one sentence in the patient's language (`language`: the latest check-in's, else zh-TW); `speech_text` is the same in Simplified for the robot's voice. A medicine named in Chinese is named (the robot can say it); one in Latin letters is 「這個藥」 except in the written daily-maximum sentence.
    - `after_intake` is true on paths with evidence of a pill already swallowed (camera commit, the robot's request, a caregiver's answer). The sentence then says it was not recorded and to tell family if unwell (「這次沒有記錄，因為…」), and that family has been told when a double-dose alert went.
    - The web shows `reply` only when `language` matches the page's language; otherwise its own words (`lib/doses.ts`).
  - **A caregiver's 'taken'** is judged at the request's `created_at`, which is also stored as `actual_intake_time`: the next gap counts from when the pill went down. `taken_confirmation_job` therefore keeps such doses by `GREATEST(actual_intake_time, resolved_at)`.
  - **Double-dose alert:** a `dose_too_soon` / `daily_max_reached` refusal on a path with evidence queues a priority-0 `double_dose_alert` to verified family with `notify_missed`, at most once per dose in a rolling hour (`ALERT_EVERY`: an outbox row for the dose in the last hour holds it).
    - Evidence: a camera commit, any robot confirmation request, a caregiver's 'taken'.
    - A refused button press only shows its sentence.
  - **Reminders (`missed_dose_job`)** judge the slot's doses on the run's clock: the robot is sent for the doses allowed (one medicine's refusal doesn't keep it from the others), inside a savepoint so a late refusal never undoes the reminder flag. The patient's LINE reminder asks only for allowed doses and gives each held-back dose its sentence (e.g. 「…已經錯過了，請不要補吃」), never 「請盡快服用」 for it. Reminder and missed-alert times are in `MEDCARE_TIMEZONE` (until 3 Oct they were UTC: "14:00" for the 22:00 dose).
  - **The legacy page** (`/medicines/{id}/taken`) uses the open dose a pill counts for, else today's nearest open dose, so a refusal says why (with protection off it records as before); `medicines.html` and `dashboard.html` show `reply`.
  - **`taken_notified`** is cleared whenever a dose becomes or stops being 'taken' (`commit_monitored`, `transition_intake`, `resolve`, `undo_monitored`), and the taken report's outbox key names the recording time: a dose taken again is reported again. The report quotes a family confirmation only for a dose recorded that way (`caregiver_confirmed`). (On 3 Oct the test records were reverted to pending/missed with `taken_notified` still TRUE and their 'confirmed' requests left in place.)
  - **LINE confirmation requests** say when the medicine was last recorded (within 24 h) and how early the dose is (over 30 min), with protection on or off.
  - **Off:** nothing is refused and no alert is sent. Turning it off writes a notification and tells every verified family contact on LINE; turning it on again sends nothing.
  - **Medication API:** create, update and list take `min_interval_minutes` and `max_daily_doses` (null = the default; an update that leaves one out keeps it). The list adds `default_min_interval_minutes` and `default_max_daily_doses`, and `/today` rows add `expires_at` (null: never), which the web prefers over its own reckoning. The medication form warns (doesn't block) when a limit refuses some of the medicine's own scheduled doses.
  - **Skip:** skipping a later dose is still allowed.
  - **Which dose a pill counts for:** the open dose nearest to now; the earlier one on a tie.
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
| `DOSE_EARLY_MINUTES` | How early a dose may be started or recorded (also capped at halfway from the previous dose), while the patient's overdose protection is on | `120` |
| `LLM_FALLBACK_MODEL` | Tried after `LLM_MODEL` fails, times out or is rate-limited; pin a chat model. With `REACHY_FEATURE_ENABLED`, the startup check requires both models pinned (not `openrouter/free`): both get the memory notes | `openrouter/free` |
| `LLM_DEADLINE_SECONDS` | Longest a check-in reply may take (both models) before Reachy says a fixed line | `8` |
| `OPENROUTER_PROVIDER_ONLY`, `OPENROUTER_DATA_COLLECTION` | Optional provider routing for every OpenRouter call; both models must be served by the pinned providers | empty |
| `WEATHER_ENABLED`, `WEATHER_PLACE`, `WEATHER_LATITUDE`, `WEATHER_LONGITUDE` | Weather for the check-in background (Open-Meteo; only the coordinates are sent) | on, 台北 |
| `BACKUP_PASSPHRASE` | Encrypts weekly backups (`backup` service) | empty |

## Tests
- Robot bridge: `…\.venv-test\Scripts\python -m pytest reachy_bridge/tests -q -p no:cacheprovider` (no robot, MediaPipe, or network needed).
- End-to-end checks against the running stack live in `D:\medcareai2_20260920\spike\`. Phase 2 must run inside the container, because port 8001 isn't published: `docker compose cp ../spike/e2e_phase2.py app:/tmp/ && MSYS_NO_PATHCONV=1 docker compose exec -T -w /app app python /tmp/e2e_phase2.py`. Without `MSYS_NO_PATHCONV=1`, Git Bash rewrites `/app` into a Windows path.
- Backend: `python -m pytest tests -q`. Tests use fake pools and stub `asyncpg`. The Microsoft Store Python on this machine lacks the app deps; a ready venv is at `D:\medcareai2_20260920\spike\.venv-test` (`…\Scripts\python -m pytest tests -q -p no:cacheprovider`). On the `C:\medcareai` laptop there is no venv; run them in a throwaway app container (this also runs the PyAV encode test): `MSYS_NO_PATHCONV=1 docker run --rm -v "C:/medcareai/MedAiCarePlus:/src" -w /src --entrypoint sh medcareai2-app -c "pip install -q pytest httpx; python -m pytest tests -q -p no:cacheprovider"`.
- Frontend consent flows: `frontend_source/tests/consent.spec.ts` (API fully mocked). `playwright.config.ts` points at a non-existent `../playwright_test`, so run it with a config whose `testDir` is `./tests` and whose `webServer` is `npx vite preview --port 8000`.
- Frontend lint: `eslint src` reports 20 errors in 13 files, all from before the memory merge (react-hooks `set-state-in-effect` / `immutability`, plus one `no-useless-assignment` in `Family.tsx`; e.g. `Settings.tsx`'s `fetchSettings` used before it's declared, `MemoryPanel.tsx`, `DoseVideoCard.tsx`). `npx tsc -b` is the gate; don't add new ones.
- Frontend memory UI: `frontend_source/tests/reachy.spec.ts` covers the memory switch and notice, withdrawal from the switch, check-ins and the camera (with and without deleting the notes), and `MemoryPanel` on `/conversations`.

## LINE Notifications
- **Channel:** "Care Bot" (`@331ealnq`). Its QR is `frontend_source/public/line-bot-qr.png` (committed; `.gitignore` excludes `*.png` except this one). Keys go in `.env` as `LINE_CHANNEL_ACCESS_TOKEN` and `LINE_CHANNEL_SECRET`.
- **Webhook:** `scripts/line-tunnel.ps1` starts the `line` compose profile:
  - `line-webhook-proxy` (nginx) forwards **only** `POST /api/notify/webhook/line` and `GET`/`HEAD` of dose-video links (`/api/media/line/<43-char token>.mp4|.jpg`), and returns 404 for everything else. After editing `scripts/line-webhook/nginx.conf`, reload with `docker exec medcareai2-line-webhook-proxy-1 nginx -s reload` (no tunnel restart, so the address stays).
  - `line-tunnel` is a Cloudflare quick tunnel to that proxy.
  
  The script then sets LINE's webhook URL through the Messaging API and runs LINE's webhook test. The rest of the app is never exposed. Never tunnel port 8080 directly: the legacy routes and the SPA path handling are unsafe on the internet (see the security review).
- **The quick-tunnel address changes whenever the tunnel container restarts** (reboot, Docker restart). Rerun `scripts/line-tunnel.ps1`, which takes about 30 s and is idempotent.
- Test: `POST /api/notify/missed-dose?u_id=1&med_name=Aspirin&scheduled_time=08:00`
- Status: `/api/notify/status` → `{"configured": true/false}`
