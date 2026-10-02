# Reachy data contracts and failure modes

This report describes the implementation checked into this repository. It does **not** establish the behavior of the installed `medcare_reachy` app on the robot: its source, build, and version are not present here. Treat the on-robot app as an independent client until its version and API behavior are verified.

## Current evidence and immediate blockers

The operator-provided snapshot for 2026-10-02 is: Reachy Mini Wireless at `192.168.49.81:8000` is reachable and `medcare_reachy` is installed/running; the Docker app is healthy at host port 8080 and publishes its private device API on `192.168.49.32:8001`; a device is registered, but `last_seen_at` is null. These are observations supplied for this investigation, not facts verified by the code review. The installed app's version and wire protocol remain unknown.

The app's reported `/health` state is `status=ok`, face recognition available, intake detection available, and emotion unavailable. The configured emotion model path is `/models/emotion_seed43/model_fp32.onnx`; the supplied container inspection found only `model_fp32_metadata.json`, not the ONNX file, and `EmotionService` reported an ONNX Runtime `NO_SUCHFILE` error. This is a confirmed monitor-start blocker: both browser and device `monitor/start` return 503 unless **both** face recognition and emotion models are available ([app health/startup](../../app/main.py#L28), [device monitor start](../../app/routers/api_device.py#L177), [browser monitor start](../../app/routers/api_monitor.py#L76), [emotion model loading](../../app/services/emotion_service.py#L35)).

That model failure does **not** gate `/api/device/heartbeat`. The heartbeat route uses a different auth dependency and does not inspect model availability ([heartbeat handler](../../app/routers/api_device.py#L154)). Therefore, the missing ONNX artifact explains why a monitoring session cannot start, but does not by itself explain `last_seen_at = null`. The repository bridge can still heartbeat while monitor start is returning 503, provided its process has completed initialization and its device token/consent/network path is valid.

## Repository bridge data flow

```text
Reachy camera -> bridge SDK + MediaPipe -> landmark JSON -> private device API
                                  `------> JPEG multipart -> private device API
private API -> in-memory monitor state -> identity / emotion / intake policy
app task queue -> bridge long-poll -> robot motion and audio prompts
bridge heartbeat -> device status + task-lease extension
```

The bridge has no direct database access. It sends an HTTP bearer token to the app's private `/api/device/*` API, receives task/session state, and uses the Reachy SDK locally for camera, motion, and prerecorded audio. The checked-in implementation does not open a microphone ([bridge client](../../reachy_bridge/app_client.py#L57), [bridge entry point](../../reachy_bridge/__main__.py#L26), [robot media interface](../../reachy_bridge/media.py#L97)).

When a monitor session is attached, the bridge samples frames at a target 15 fps, runs local MediaPipe face/hand/pose landmarkers, and creates a JSON packet with normalized coordinates, frame dimensions, capture timestamp, and frame sequence. The bridge caps the packet at four faces, eight hands, and four poses. The packet carries selected face-box landmarks, nose/shoulders/wrists, and selected hand landmarks; it does not send the full MediaPipe landmark arrays ([stream](../../reachy_bridge/runner.py#L33), [packet builder](../../reachy_bridge/packets.py#L34)).

At most every 200 ms, it also JPEG-encodes a camera frame at quality 75. JPEG upload is capped at five frames per second and is sent only after the corresponding landmark request succeeds, with the same `frame_seq`. The server accepts up to 1 MB per JPEG and rejects decoded images larger than 1920x1080. The reviewed routes decode frames and hold landmark snapshots in the in-memory session; these paths do not write image/JPEG data to a media store. Intake results and dose events follow the database paths described below; this statement is limited to the reviewed routes/services ([stream flow](../../reachy_bridge/runner.py#L99), [device routes](../../app/routers/api_device.py#L210), [monitor session](../../app/services/monitor_service.py#L292)).

The task response includes patient display name, medication names/descriptions, dose form and units, intake status, dose IDs, task timing/status, and server policy flags. No live values are reproduced here. A heartbeat carries operational status only: robot reachability, measured landmark/vision FPS, bridge version, and missing-clip count/list ([task response builder](../../app/services/reachy_tasks.py#L55), [heartbeat schema](../../app/routers/api_device.py#L47)).

## HTTP contract

All bridge calls use `Authorization: Bearer rdv1.…` and the internal/private API listener. Device tokens are signed claims bound to a device and user, hashed in storage, scoped to that device's user, and rejected on the public listener. Pairing is a separate user-authenticated app request; its token is shown once. Ordinary device routes require an active device and current core plus robot-camera consent ([device authentication](../../app/services/device_auth.py#L18), [port isolation](../../app/main.py#L49), [pairing/status](../../app/routers/api_reachy.py#L55)).

| Direction and endpoint | Request / response contract | Server effect |
|---|---|---|
| Bridge → app `GET /api/device/tasks/current` | No body. Returns the task currently leased to this device, or HTTP 204. Task fields include `task_id`, `slot_time`, `reason`, `attempt`, `status`, `expires_at`, `patient_name`, `auto_record`, and `doses[]`. | Restart recovery; task and dose state are re-read from the database. |
| Bridge → app `GET /api/device/tasks/next?wait=25` | `wait` is an integer from 0 to 25 seconds. Returns a task or HTTP 204. | Long-polls queued, unexpired work; oldest eligible slot is leased to this device for 60 seconds. |
| Bridge → app `POST /api/device/heartbeat` | JSON: `robot_reachable` (bool/null), `landmark_fps` and `vision_fps` (0–240/null), `bridge_version` (≤50 chars/null), `missing_clips` (int/string-list/null). | With valid current consent: writes `last_seen_at`, robot reachability, landmark FPS, plus vision FPS/version/missing-clips in `status_detail`; extends this device's open task leases by 60 seconds. Response is `{stop_all:false, server_time}`. |
| App → bridge heartbeat response | `stop_all` plus UTC `server_time`. | On revoked device or withdrawn consent, returns `{stop_all:true,...}` and skips the status/lease update. Bridge stops its active slot when it receives `stop_all`. |
| Bridge → app `POST /api/device/tasks/{task_id}/status` | JSON `{status, detail}`; statuses are `searching`, `in_progress`, `completed`, `not_found`, `aborted`. | Validates task ownership/lease and transition; nonterminal state extends lease, terminal state clears it. |
| Bridge → app `POST /api/device/tasks/{task_id}/confirmation` | JSON `{intk_id, source, evidence}`. Source is one of `uncertain_detection`, `unsupported_dose`, `degraded`, `auto_record_off`, `patient_claim`. | Confirms only an intake belonging to this leased task. Sets eligible intake to `pending_confirmation`; creates caregiver confirmation/outbox work, without marking the dose taken or changing stock. |
| Bridge → app `POST /api/device/tasks/{task_id}/extra-event` | JSON `{event_id, decision, confidence}`, decision `confirmed` or `uncertain`, confidence 0–1. | Stores a derived observation and queues at most one family notice per stable event ID; never commits a dose. |
| Bridge → app `POST /api/device/monitor/start` | JSON `{mode:"dose"|"observe", intk_id?, task_id?}`. Dose mode requires both IDs; observe mode has no dose. Returns public session state with `session_id` and `generation`. | Requires face and emotion model availability. Dose must be pending/missed, active, stocked, belong to this account and leased task. Server—not bridge—sets `auto_commit`. Another live client type can receive 409 `busy_other_client`. |
| Bridge → app `POST /api/device/monitor/landmarks` | JSON `{session_id, generation, frame_seq, timestamp, width, height, faces[], poses[], hands[]}`. Limits: sequence >0; dimensions 1–1920 by 1–1080; at most 4 faces, 4 poses, 8 hands. | Validates session/user/client, advances accepted sequence, saves the snapshot in bounded in-memory history, runs detector policy, returns monitor public state. |
| Bridge → app `POST /api/device/monitor/vision` | Multipart fields `session_id`, `generation`, `frame_seq`, and `file` (`image/jpeg`). | Uses the landmark packet for that exact frame for identity/emotion/candidate readiness; may commit only under server policy. |
| Bridge → app `POST /api/device/monitor/end` | JSON `{session_id, generation}`. | Ends the in-memory session and detector state; success response `{success:true}`. |

The task queue is app-owned: scheduling creates/re-arms a task only when an active device and current core + robot-camera consent exist. It does not push tasks to the robot. `tasks/next` leases queued work; `tasks/current` is the restart-recovery path ([task enqueue/lease](../../app/services/reachy_tasks.py#L80), [device routes](../../app/routers/api_device.py#L82), [runner polling](../../reachy_bridge/runner.py#L198)).

## Heartbeat and idle semantics

In the checked-in bridge, the runner starts its heartbeat loop and camera loop before task polling. Heartbeat calls are scheduled every 10 seconds after each prior call completes. Each heartbeat first calls `robot.is_reachable()` via the blocking-work adapter, then posts the payload. This loop exists even when there is no active task/session. With no attached monitor session, `MonitorStream.step()` returns without reading or sending camera frames; the task loop still long-polls. Thus an idle but initialized bridge should produce heartbeats and task polls, but no camera-frame or landmark transfer ([runner loops](../../reachy_bridge/runner.py#L170), [heartbeat](../../reachy_bridge/runner.py#L247), [idle stream gate](../../reachy_bridge/runner.py#L99)).

The app reports the device online only when `last_seen_at` is non-null and no more than 60 seconds old. The timestamp is written by the successful, consent-current heartbeat handler, not by task polling or monitor traffic. It is therefore a heartbeat-health marker, not a camera/data-transfer counter ([status calculation](../../app/routers/api_reachy.py#L33), [heartbeat update](../../app/routers/api_device.py#L154)).

`last_seen_at = null` means no heartbeat reached the database's normal update branch for that device. It does **not** prove that no heartbeat request was attempted: a valid revoked/no-consent token gets `stop_all:true` without updating the timestamp; invalid tokens get 401; network, startup, and private-listener failures also leave it null. Conversely, a non-null/fresh timestamp says nothing by itself about camera capture, landmark FPS, or whether `monitor/start` succeeds. The robot camera API can be completely idle while heartbeat remains healthy.

## Ordering, replay, and backpressure

- **Session identity:** the server creates fresh UUID `session_id` and `generation` for every monitor session. Every monitor request carries both; lookup checks user, active registry entry, generation, and `client_type="reachy"`. A session belongs to one client type, and the registry permits only one live browser-or-Reachy session per user. Sessions are process-memory state, not durable rows ([registry](../../app/services/monitor_service.py#L217), [session lookup](../../app/services/monitor_service.py#L284)).
- **Frame sequence:** the bridge increments `frame_seq` within one attachment and resets it to 0 when a new session attaches. It sends frames starting at 1. The server silently returns the current state for duplicate/older landmark sequence numbers, without advancing activity. Vision is processed only once and only when the exact sequence's landmark packet is still in the latest 48-frame cache; stale, missing, or repeated JPEGs return current state without inference ([bridge sequence](../../reachy_bridge/runner.py#L60), [server sequence/cache](../../app/services/monitor_service.py#L292), [vision matching](../../app/services/monitor_service.py#L356)).
- **Generation is the reset boundary:** old in-flight requests cannot update a new session because their session/generation no longer resolves. A fresh server session has a new generation, and the bridge resets its frame sequence on attach. On server restart, in-memory sessions disappear; the task lease/current-task path permits the bridge to rebuild a session, subject to lease expiry.
- **Backpressure:** there is no unbounded frame queue. At most one landmark call and one vision call are in flight. When landmarks are still in flight, the next capture step returns and drops that frame. Vision is skipped while the prior JPEG call is in flight; JPEGs are also rate-limited to 5 fps. The latest public monitor state accepts newer frame responses and will not regress to an older response or lose a same-frame `recorded` result ([stream gate/dispatch](../../reachy_bridge/runner.py#L99), [state acceptance](../../reachy_bridge/runner.py#L152)).
- **Task leases:** leases last 60 seconds and heartbeat extends only open tasks held by that device. A minute maintenance job requeues expired leases unless the task itself has expired. A task already leased/searching/in-progress is not reset by task enqueue. After restart the bridge checks `tasks/current` before polling new work ([lease transitions](../../app/services/reachy_tasks.py#L114), [maintenance](../../app/services/reachy_tasks.py#L205), [restart recovery](../../reachy_bridge/runner.py#L198)).

## Idempotency and failure/retry behavior

| Case | Checked-in behavior | Consequence |
|---|---|---|
| Retry same task status after response loss | Repeating the task's current status returns its task payload; normal legal transitions update the lease or finish it. | Status is idempotent for a repeated current state. |
| Retry extra-event | A stable event UUID maps to a deterministic user-scoped UUID; DB conflict skips duplicate row and notification. | Event delivery is idempotent for the same event ID. |
| Retry dose confirmation | Request has no idempotency key. Each successful `create()` selects still-pending/missed doses, changes them to `pending_confirmation`, then creates a new confirmation UUID. Once the first call changes the intake state, a retry normally returns 409 `No pending or missed dose to confirm`. | Not an idempotent request contract; client handles a 409 by refreshing task doses and treating an already-resolved dose as done. A lost-response/commit boundary deserves targeted operational scrutiny. |
| Retry landmark/JPEG | Old/duplicate sequence returns current monitor state, without rerunning detection/vision. | Safe against duplicate inference, but dropped/out-of-order frames are not buffered for later retry. |
| Transport error or HTTP 5xx other than 503 | `AppClient` marks first outage time and raises `AppUnreachable`; a subsequent non-5xx response clears the outage. During an active slot, bridge retries by ticking and fails closed after 10 seconds unreachable. Idle task polling backs off five seconds. | No durable client-side camera queue; streams stop once the slot fails closed. |
| HTTP 503 | Separate `ServiceUnavailable`, does not count as transport outage. `monitor/start` uses this when either model is unavailable. | A slot shuts down as `model_not_ready` and reports aborted status; heartbeat loop is separate and can continue. |
| HTTP 401/403 | `NotAuthorised`; device routes require consent. Heartbeat handles revoked/withdrawn consent with `stop_all`, but other route calls can produce 401/403. | Active slot is stopped and bridge sleeps the robot; idle runner logs and waits 60 seconds before retrying task poll. |
| HTTP 409 | Generic conflict becomes `SessionLost`; detail `busy_other_client` becomes its subtype. Monitor conflict triggers task/dose refresh and session recovery, capped at 10 attempts. Busy client waits 30 seconds before retrying and aborts at task expiry. | A competing live browser session can block Reachy; old generation cannot be reused. |
| HTTP 4xx other than 401/403/409/503 | `RequestRejected`. A 404 for a task is treated as task gone; other rejected slot requests abort with `bridge_error`. | No automatic retry for schema/policy rejection. |
| Graceful process shutdown | Ends the monitor session best-effort and sleeps robot; task is left leased for recovery, then `tasks/current` or lease expiry recovers it. | There is no durable stream cursor. The session starts with a new generation. |
| Robot SDK reachability call stalls | `is_reachable()` is invoked before the heartbeat HTTP request. Startup also calls it before constructing the vision engine and starting runner loops. The checked-in path has no explicit timeout around those SDK calls. | A blocked connect/probe can delay or prevent heartbeats even with a reachable app. |
| Bridge dependencies fail during initialization | Before any runner loop starts, entry point validates config, imports/constructs MediaPipe `VisionEngine` (requires three `.task` assets), constructs/connects the robot, and audits clips. | Missing local model files/import failures or a blocking robot connection can prevent the heartbeat loop from ever starting. This differs from the server-side missing emotion ONNX, which blocks monitor start but not heartbeat. |

References: [HTTP error mapping](../../reachy_bridge/app_client.py#L75), [slot retry/fail-closed policy](../../reachy_bridge/session.py#L106), [heartbeat loop](../../reachy_bridge/runner.py#L247), [bridge initialization order](../../reachy_bridge/__main__.py#L26), [MediaPipe model checks](../../reachy_bridge/vision.py#L10), [deterministic extra-event key](../../app/routers/api_device.py#L130), [confirmation create path](../../app/services/dose_confirmation.py#L139).

## Confirmed findings vs. open questions and proposed checks

### Confirmed in repository or supplied runtime evidence

1. The repository bridge has a distinct device-token API and transfers heartbeat/task state even when no monitor session is attached.
2. The monitor pipeline transfers compact landmark JSON and selected JPEG frames only after a server monitor session starts. Backpressure drops frames; it does not queue them.
3. `/api/device/heartbeat` writes `last_seen_at` only on its normal consent-current branch. Device status considers a timestamp fresh for 60 seconds. Model readiness is not checked by heartbeat.
4. The checked-in device and browser monitor-start handlers both require face and emotion models. The supplied live health state reports emotion unavailable, and the supplied image inspection found the configured ONNX model file missing. This prevents monitor startup with HTTP 503.
5. The current database observation `last_seen_at = null` is compatible with no request, network or token failure, missing consent/revocation branch, or pre-runner initialization failure; it does not identify which one occurred.

### Unknown; do not infer from the app name

The robot's installed `medcare_reachy` version, its heartbeat URL and cadence, token format, whether it uses this repository's task/monitor API, whether it owns camera capture, whether it buffers or retries data, and how its app-managed daemon state affects launch are unknown. The live process name and port do not prove protocol compatibility with `reachy_bridge`.

### Documentation-only investigation plan

1. Record the installed app's exact package/version/build identifier and obtain its source or vendor contract. Confirm which app UI starts it and whether the daemon must be running.
2. Correlate one controlled app start with app-side logs and server-side access logs for the private listener. Record method/path/status/timestamp only; redact bearer headers and request bodies. First distinguish no attempt from connect failure, 401/403, 404/422, and successful heartbeat.
3. Restore and verify the missing `model_fp32.onnx` artifact from the approved model source, then confirm `/health` reports emotion available. Do not treat that correction as a heartbeat fix; separately verify `last_seen_at` advances and that `monitor/start` no longer returns the model readiness 503.
4. If the installed app and repository bridge are both candidates, compare their endpoint/payload contracts against this report before selecting a single owner. The repository bridge and the robot-installed app must not be presumed interchangeable or run in parallel without proving device/session ownership behavior.

No service, configuration, or runtime behavior was changed for this report. Source and tests were read but not executed.
