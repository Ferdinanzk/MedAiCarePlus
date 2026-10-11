# Robot camera, finding the patient, AI card fixes and thinking levels (Phase 1)

Date: 2026-10-11. Branch: `robot-camera-thinking`, from `ai-connections` (`4808126`, not merged).
Approved by the user on Telegram and in the terminal ("go", 2026-10-11). Phase 2 (GPT-Live check-ins, with the camera
kept on during the chat) gets its own spec after a measurement spike.

## 1. Why

The user reported three things:
1. "The robot can't detect the patient's face" before it talks, although face login on the dashboard works well.
2. gpt-live-1 is not usable for Reachy's replies.
3. OpenAI models should be able to "think".

A read-only investigation (32 agents, every load-bearing claim re-checked by independent skeptics) found:

- **The camera was dead, not the face model.** The TCU companion service connects with `media_backend="no_media"`, which
  in SDK 1.11.0 releases the daemon's camera. MedCare's camera reader (GStreamer `unixfdsrc`) then never reconnects: one
  stale frame, then `None` forever. The robot app logs nothing and searches for 10 minutes. (Flaw list F7, F8.)
- **Two bridge copies can run at once** after two overlapping restarts of the robot app's worker. (F9.)
- **Even with frames, the robot path can't verify a patient beyond about 1.35 m.** Identity also needs the server's
  MediaPipe short-range face detector to see the face, which fails below about 50 px of face width in the 480x360
  frame. Dashboard login uses the OpenVINO matcher alone. (F10.) Failed checks leave no trace. (F11.)
- **Safety checks 2 and 3 can't run:** the card's OpenRouter connection has no key (copied empty from `.env` on the
  first start), and the card doesn't warn because its warning only fires when *no* job has a key. (F12.)
- **gpt-live-1 is a voice-only model** (Live WebSocket only). The card's Test fails with OpenAI's confusing 404. (F13.)
- **Thinking** works on gpt-5.4-mini and gpt-6-luna, but with thinking on they refuse the app's temperature, and
  reasoning tokens can use up the small token caps and return empty answers. A latent Test bug leaves models that
  refuse `none` (GPT-6 Astra, 6.1 Sol) thinking inside a 200-token cap. (F14.)

The flaw list is kept outside the public repo at `C:\Reachy_Mini\repo\flaw.md`.

Already done by hand (not part of this build): the TCU companion is disabled at boot on the robot, the camera was given
back (`POST /api/media/acquire`) and MedCare Reachy runs.

## 2. Scope

In:
- **A.** Robot app 0.6.0: camera watchdog with recovery, camera state in the heartbeat, early end of a search with a
  clear reason, fix of the restart race. Deployed to the robot.
- **B.** Server: the search before talking verifies with the OpenVINO match alone; identity outcomes are logged; the
  Reachy card shows the robot's camera state.
- **C.** AI connections card fixes: a warning for each job with no usable model; a keyless OpenRouter connection takes
  `.env`'s key at start; a clear Test message for voice-only models; the stale check-ins help text.
- **D.** Thinking level per job.

Out (later or never):
- Live voice with GPT-Live (Phase 2), and choosing a TTS voice (dropped: GPT-Live will speak itself).
- MediaPipe's long-range face model, higher-resolution search frames (decide after measuring with B's logs).
- Refusing a second bridge instance on the server (A fixes the cause on the robot).
- Showing the robot notice before the check-ins switch grants consent (recorded as a flaw, not fixed here).

## 3. Design

### A. Robot app 0.6.0 (`reachy_app/`)

**A1. Restart race** (`medcare_reachy/service.py`, `static/main.js`).
- `BridgeService.restart()` runs under its own lock, so two restarts never interleave.
- `stop()` never sets `_thread = None` while that thread is still alive; `start()` refuses to start a bridge thread while
  one is alive (it logs and returns).
- After two concurrent `restart()` calls exactly one bridge thread runs, and it is the tracked one.
- The settings page disables Save and Restart while a request is pending.

**A2. Camera watchdog** (`bridge/runner.py`, `bridge/media.py`).
While a monitor session is attached and streaming:
- A frame counts as new only when the SDK returns a frame. The stream records the time of the last new frame.
- **Stall:** no new frame for `CAMERA_STALL_SECONDS = 2.0`. The app logs one ERROR line per stall, with the daemon's
  `GET /api/media/status` answer, and starts recovery.
- **Recovery, at most once per `CAMERA_RECOVER_INTERVAL = 5.0` s:**
  1. If the daemon says media is released (`released: true`) or unavailable, ask the daemon for it back through the
     SDK's own `acquire_media()` on the app's `ReachyMini` object (it also gives the app a fresh media manager).
  2. Otherwise reopen the camera reader through the SDK (a release/acquire cycle of the app's own media, or the SDK's
     supported reopen call). The implementer must read the SDK 1.11.0 source on the robot
     (`/venvs/apps_venv/lib/python3.12/site-packages/reachy_mini/`) and use public methods where they exist. A private
     method is allowed only behind a version check, with a test, and a log line naming it.
  3. A recovery attempt that raises is logged and counted; it never crashes the stream.
- **Give up:** no new frame for `CAMERA_GIVE_UP_SECONDS = 15.0` since the stall began. The search ends with task status
  `not_found` and detail `{"reason": "camera_unavailable"}`, the robot says nothing extra and goes to sleep as for any
  `not_found`. A dose task ends the same way (the server's missed-dose path handles the dose as for any `not_found`).
- **Recovered:** a new frame after a stall logs one INFO line with the stall's length and resets the stall.
- The existing slow-camera warning resets after a recovery, so it can fire again.

**A3. Camera state in the heartbeat** (`bridge/runner.py`, `bridge/app_client.py`).
The heartbeat adds `camera`: `{"state": "idle"|"ok"|"stalled"|"recovering"|"unavailable", "fps": <float>,
"since": <unix seconds of the last state change>, "media": <daemon media status dict or null>}`.
- `idle` when no session is attached (the camera isn't read then, so 0 fps is normal).
- `unavailable` stays until a later session gets frames again.

**A4. Version and deploy.** `__version__ = "0.6.0"` (both places the repo pins it). Deploy with
`reachy_app/tools/deploy_to_robot.py`, then restart the app through the daemon. The deploy also replaces the robot's
older SenseVoice `voice.py` with the repo's Whisper Base version (F15); the deploy tool installs what Whisper needs.

### B. Server

**B1. Identity for the search before talking** (`app/services/monitor_service.py`).
- A robot session that **started** in `observe` mode (the search before a check-in or before prompting a dose) verifies
  the patient with the OpenVINO match alone:
  - exactly one face labelled as the patient;
  - fewer than 4 OpenVINO faces (`saturated` false);
  - 2 hits in a row whose boxes overlap by at least 0.20, as today.
  - It does **not** need a MediaPipe face box on the same frame.
- Everything else keeps today's rule, including the MediaPipe gate: dose sessions, browser sessions, and a dose session
  that switched to `observe` after recording. The session records the mode it started in (a new field); `mode` alone
  can't tell, because it changes after a record.
- A dose is still never recorded from a search session. The dose prompt is spoken only after verification in that
  session, as today.

**B2. Identity log** (`app/services/monitor_service.py`).
For robot sessions, every change of `identity_status`, and at most one line per 5 s otherwise, logs one INFO line:
session id, status, rule used (`openvino_only` or `with_mediapipe`), best patient distance, the patient box width in
pixels, the OpenVINO face count and the MediaPipe face count. No images, names or labels other than "patient"/"other".

**B3. Robot camera state on the Reachy card**
(`app/routers/api_device.py` heartbeat, `app/routers/api_reachy.py`, `frontend_source/src/components/ReachyCard.tsx`,
`src/lib/reachy-api.ts`, `src/i18n.ts`).
- `HeartbeatPayload` accepts the optional `camera` object (validated; unknown states are refused with 422) and stores
  it in `reachy_device.status_detail`.
- `/api/reachy/status` returns it.
- The card shows "Robot camera not working" (zh-TW 「機器人鏡頭無法使用」) when the state is `stalled`, `recovering` or
  `unavailable` and the robot is online, and shows the camera fps while a session runs.
- The task's `not_found` detail (`camera_unavailable`) is stored with the task like any status detail; no new task
  history screen is added.
- Robots older than 0.6.0 send no `camera`; the card then shows nothing new.

### C. AI connections card fixes

**C1. Warning for each job with no usable model**
(`app/services/ai_settings.py` `public()`, `AiConnectionsCard.tsx`, `i18n.ts`, startup log).
- A model is usable for a job when it is tested (has a profile) and its connection has a readable key.
- `public()` adds `jobs_usable: {"reply": bool, "safety": bool, "after_chat": bool}`.
- The card shows, under each job without a usable main or backup model:
  - "No usable model: this job can't run." / 「沒有可用的模型：這項工作無法執行。」
  - For safety: "Reachy's model safety check is off." / 「Reachy 的模型安全檢查已停止。」
  - For after_chat: "Summaries and the summary safety check are off." / 「摘要與摘要安全檢查已停止。」
- The existing `no_key_in_use` warning stays.
- At startup the server logs one WARNING per such job.

**C2. A keyless OpenRouter connection takes `.env`'s key at start** (`app/services/ai_store.py`, `app/main.py` lifespan).
- After the first-start seed, on every start: if `OPENROUTER_API_KEY` is set in the environment, every connection of
  kind `openrouter` whose key is NULL gets that key (encrypted as usual), in one `change()` transaction.
- It logs "filled the key of N OpenRouter connection(s) from .env", never the key.
- A connection that has a key is never touched. The card stays the place to change keys.

**C3. Voice-only models** (`app/services/ai_profile.py` detect, `app/routers/api_ai.py`, card, i18n).
- When Test gets an HTTP 404 (or 400) whose message says the model is not a chat model ("not a chat model" or "not
  supported in the v1/chat/completions endpoint", case-insensitive), the test result's error is the code
  `not_chat_model`.
- The card shows: "Voice-only model (like GPT-Live or a TTS model): it can't write Reachy's text replies." /
  「語音專用模型（例如 GPT-Live 或 TTS）：無法撰寫 Reachy 的文字回覆。」
- `last_error` stores the same code text. Nothing else about Test changes.

**C4. Stale help text** (`i18n.ts` `reachy.checkinsHelp`, en and zh-TW).
It names "OpenRouter (free AI models, demo)". Replace it with wording that names no company: "the AI service chosen on
the AI connections card". Keep the rest of the sentence's meaning: only text is sent.

### D. Thinking level per job

**D1. Storage** (`sql/init.sql`).
`ALTER TABLE ai_job ADD COLUMN IF NOT EXISTS thinking VARCHAR(10) NOT NULL DEFAULT 'off'`, with a CHECK
(`off`, `low`, `medium`, `high`). The default keeps today's requests byte for byte.

**D2. What a model can do, found by Test** (`app/services/ai_profile.py`).
The profile JSONB gains `thinking: {"param": "reasoning_effort" | "openrouter_effort" | null, "levels": [...],
"ms": {"low": int, ...}, "always": bool}`. Old profiles without the key mean "can't think" and are sent as today.

After the base profile passes:
- **OpenAI and OpenAI-compatible:**
  1. Send one request with an invalid level (`reasoning_effort: "max"`) and no temperature. A 400 naming
     "Supported values are: ..." gives the list.
  2. If the list can't be parsed, probe `low`, `medium` and `high` one by one instead.
  3. A model that refuses `reasoning_effort` altogether ("Unrecognized request argument") can't think.
- **OpenRouter:** read the public `GET {base_url}/models` entry for the model id: `reasoning.supported_efforts` and
  `mandatory`. No entry, or no efforts, means "can't think".
- **Then, for each of `low`, `medium` and `high` in the list:** one short probe with no temperature and the reply
  budget plus that level's allowance (D4). Save its time in ms. A level that comes back empty or cut off
  (`finish_reason: length`) is not offered.
- The card shows the extra time on Test as a short "testing thinking levels" status.

**The latent bug (F14).** When a model refuses `none` (GPT-6 Astra, 6.1 Sol), its `reasoning_off` becomes the lowest
accepted level and `thinking.always = true`. It must never fall back to "no setting", which leaves the model at its
default level. Such a model's "off" means its lowest level, and its requests always get that level's token allowance.

**D3. Requests** (`ai_profile.request_body`, `app/services/ai_settings.py` `Target` carries the job's level,
`app/services/conversation.py`, `app/services/memory.py`).
- **Level `off`:** today's body, unchanged (including `reasoning_off`).
- **Another level, OpenAI:** `reasoning_effort: <level>` and **no `temperature`**.
- **Another level, OpenRouter:** `reasoning: {"effort": <level>, "exclude": true}`, and `temperature` only if the
  model's OpenRouter entry lists `temperature` in `supported_parameters`; else none.
- **Main and backup can differ:** when a model can't do the job's level, it uses the nearest lower level it accepts,
  else `off`.
- **Memory extraction:** its `temperature=0` is dropped when thinking is on.

**D4. Token caps** (`conversation.py`, `memory.py`).
A call's cap is the job's base cap plus an allowance for the level actually sent: low +800, medium +1600, high +3000
(also for an `always` model at its lowest level). The base caps stay as they are: reply 200, risk 32, summary 400,
memory 1000. Per-call timeouts and deadlines don't change.

**D5. Measuring** (`conversation.py`).
- Each attempt's metrics record `reasoning_tokens` (from `usage.completion_tokens_details.reasoning_tokens`, when present)
  and the level sent.
- When an answer is empty with `finish_reason: length` and `reasoning_tokens == completion_tokens`, the attempt's error
  and the model's `last_error` say "thinking used the whole budget" (code `thinking_budget`).

**D6. Jobs API and card** (`app/routers/api_ai.py` `PUT /jobs`, `AiConnectionsCard.tsx`, `src/lib/ai-connections-api.ts`,
`i18n.ts`).
- `PUT /jobs` takes `thinking` per job (`off|low|medium|high`; anything else is 422 `invalid_request`).
- **Each job row** gets a "Thinking" select:
  - Off is always offered. A level is offered when the main model accepts it.
  - Next to the select: the main model's measured time at that level.
  - A warning when that time is over the job's per-call limit (reply 6 s, safety 2.5 s, after-chat 12 s): "Thinking at
    this level took longer than this job allows; Reachy may use its fixed line." / 「此思考等級花的時間超過這項工作的
    上限；Reachy 可能會說固定句子。」
- **Each model row** shows "Can think: low · medium · high" or "Can't think" (zh-TW 「可思考：低・中・高」 / 「不可思考」),
  from its profile.
- Changing a job's thinking level goes through `change()`, like other job changes.
- Thinking doesn't change notice fills (same models, same companies).

## 4. Errors and safety

- Every new robot-side path catches its own errors and logs them. The stream and the session never crash from the
  watchdog.
- `camera_unavailable` ends a search; it never records or skips a dose by itself. The server treats the dose as for
  any `not_found`.
- The relaxed identity rule applies only to search sessions. Recording a dose keeps the strict rule.
- Keys: C2 never logs a key. Thinking probes use the existing redaction (`ai_crypto.redact` with the exact key).
- Thinking `off` is the default everywhere, so nothing changes for a running installation until someone picks a level.

## 5. Testing

- **Robot app** (`reachy_app/tests`, pytest, no robot needed):
  - two concurrent `restart()` calls leave one bridge thread, and it is tracked;
  - stall detection after 2 s of `None` frames, with the ERROR line;
  - recovery calls `acquire_media()` when the daemon reports released;
  - a recovered stream resets the state;
  - give-up after 15 s posts `not_found` with `camera_unavailable`;
  - the heartbeat carries `camera` with each state;
  - the watchdog never raises.
- **Server** (`tests/`, the existing fake-pool style):
  - a search session verifies with OpenVINO alone (no MediaPipe face) after 2 hits;
  - a dose session still needs the MediaPipe box;
  - a dose session switched to observe still needs it;
  - identity log lines are rate-limited and carry no names;
  - the heartbeat stores and returns `camera`, and refuses bad states;
  - `jobs_usable` is false for a keyless job;
  - the startup key fill (keyless `openrouter` filled, keyed untouched, no env var does nothing, key never logged);
  - `not_chat_model` mapping from the real OpenAI 404 text.
  - Thinking:
    - parsing "Supported values are" and the fallback probes;
    - the Astra/Sol `none` refusal sets `always`;
    - request bodies per level and kind, with temperature dropped;
    - the nearest-lower-level rule;
    - caps with allowances;
    - `reasoning_tokens` and `thinking_budget` recorded;
    - `PUT /jobs` validation.
- **Frontend:** `tsc -b`, `npm run build`, and Playwright tests for the job warnings, the Thinking select (offered levels,
  time, warning), the model "can think" line, the voice-only Test message, and the Reachy card camera warning; screenshots
  at 390 and 1280 px.
- **Live checks on this PC:**
  - one Test of gpt-5.4-mini, gpt-6-luna and an OpenRouter model, saving their thinking levels;
  - one reply at `low` through the real stack;
  - the OpenRouter key filled from `.env` at start (connection has a key, safety jobs usable).
- **Robot:**
  - deploy 0.6.0, then one real check-in. Pass: frames at the camera rate, the patient verified, the chat runs.
  - A camera-failure check: release the daemon's media during a search (`POST /api/media/release`). Pass: the app
    recovers, or ends with `camera_unavailable` within 15 s, and the card shows the camera warning.

## 6. Docs

- `CLAUDE.md`: the camera watchdog and the search identity rule; the per-job thinking level and token allowances; the
  OpenRouter key fill; the not-chat-model message.
- `reachy_app/README.md`: version 0.6.0, the camera watchdog, and the TCU companion clash (it releases the camera).
- The vault and `C:\Reachy_Mini\repo\flaw.md` (part 2: flaws found after the changes) are updated at the end.

## 7. Review focus (likely to bite, not covered by the obvious tests)

1. The SDK reopen path on the real robot. A wrong private call can kill the app's audio, because media holds the
   speaker and the microphone too. Check that playback still works after a recovery.
2. A camera that delivers frames slowly (5 fps) must not count as stalled. Only a 2 s gap counts.
3. A dose task's search ending with `camera_unavailable` must not mark the dose taken or missed early; the server's
   normal path decides.
4. The thinking probes on Test cost money and time. A model that times out on a level must not block saving its base
   profile.
5. A job whose main and backup models accept different levels must send each model a level it accepts, never a refused
   one.
