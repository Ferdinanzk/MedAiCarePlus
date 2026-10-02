# Implementation plan: reliable Reachy data transfer

Status: **proposed**, 2026-10-02. This plan prioritizes proving the sender-to-server path before adding features or replacing the transport. It does not authorize changing a user's consent or recording a real medication dose during diagnostics.

## Target architecture and decisions

1. Prefer the existing on-robot `medcare_reachy` app for the first recovery attempt. Verify its code and configuration before assuming it runs the repository bridge.
2. Keep one active MedAiCare runner for a paired device. A Docker-hosted bridge is an alternative deployment, not an additional producer with the same token.
3. Retain the existing HTTP JSON and multipart device protocol initially. There is no evidence that MQTT, a message broker, a custom WebSocket protocol, or public cloud routing is needed to fix the first heartbeat.
4. Keep Reachy control at the robot's port 8000, MedAiCare's browser interface at laptop port 8080, and authenticated device traffic at laptop port 8001.
5. Verify progress at separate stages: application started, HTTP request attempted, authenticated response received, heartbeat stored, session created, frame accepted, inference completed, outcome committed.

## Priority and dependencies

| Work package | Priority | Depends on | Completion evidence |
| --- | --- | --- | --- |
| P0: identify the deployed sender | Immediate | Access to app configuration/logs | Package version/source and redacted effective settings recorded. |
| P1: prove transport and authentication | Immediate | P0 | Robot-originated device request succeeds; three heartbeats update `last_seen_at`. |
| P2: recover model readiness | High, parallel with P1 | Trusted model artifact | Model checksum verified and backend reports `emotion=true`. |
| P3: make startup and background failures visible | High | P0 source located | First heartbeat does not wait for camera/model initialization; loop failures visible. |
| P4: prove paired frame transfer | High | P1, P2, capture ready | Landmarks/JPEG acknowledged for matching session/generation/frame sequence. |
| P5: verify recovery and persistence | High | P3, P4 | Outage/revocation/restart scenarios pass without stale-frame replay or duplicate outcomes. |
| P6: stabilize deployment and document operation | Before regular use | All prior gates | Repeatable startup, address management, diagnostics, and rollback. |

## P0 — Identify exactly what is running

**Work**

- Obtain the installed `medcare_reachy` entry point, package version, source repository/revision, and runtime dependency versions through Reachy Control's app logs or an authenticated administrative session.
- Inspect its effective backend base URL and whether a device token is present. Report presence only; do not print the token. Verify configuration precedence: saved app settings, process environment, package defaults, and `.env` loading.
- Establish how that application obtains camera media: the Reachy app runtime's existing connection, a separate SDK connection, or another mechanism.
- Determine whether it calls the repository's `Runner`, a copied version, or an unrelated implementation. If source is maintained elsewhere, make changes there and document the deployment procedure; editing this laptop repo does not update a package already installed on the robot.
- Collect startup timing and the last exception. HTTP app status `running` is insufficient. The log WebSocket returned 403 to our diagnostic client, and SSH authentication was unavailable; neither result reveals the app's actual exception.

**Acceptance**: an inventory identifies the real source to modify, the runner's location, and the redacted effective URL. The first failure is classified as configuration, network, authentication/consent, initialization, or runtime supervision.

**Do not do**: regenerate pairing tokens, clear Reachy environments, launch a duplicate bridge, or infer deployed package behavior solely from the local folder name.

## P1 — Establish the smallest genuine data transfer

**Existing code to inspect**: [device client](../../reachy_bridge/app_client.py), [device authentication](../../app/services/device_auth.py), [heartbeat handler](../../app/routers/api_device.py), [port isolation](../../app/main.py), [Docker ports](../../docker-compose.yml).

**Work**

1. From the actual robot application environment, connect to the current laptop LAN address on TCP 8001. A connection initiated on the laptop is not this test.
2. Verify that an unauthenticated GET `/api/device/tasks/current` produces the expected API response, normally 401, rather than HTML. Never use `/tasks/next` as a supposedly read-only connectivity probe: it can lease a task.
3. Use the already paired device's actual credential from its local secure configuration to make the same GET. A valid device with current consent and no task should receive 204; no task is a normal result.
4. Send the application's genuine heartbeat payload. Require HTTP 200 **and** `stop_all=false`; compare server time with a new `reachy_device.last_seen_at` value.
5. Observe three successive accepted heartbeats. Record timestamps, response latency, endpoint, and a request correlation ID without user details or credentials.
6. If the token is invalid, identify whether it is absent, revoked, from a different server, or signed against a changed `SECRET_KEY`. If replacement is needed, use the normal pairing workflow; never bypass authentication or consent.
7. If access fails only from the robot, inspect the bound laptop address, route, network isolation, and firewall events. Add a narrowly scoped local-network rule only when the relevant block is identified and policy permits it. Do not disable McAfee or Windows protection as a diagnostic shortcut.

**Proposed diagnostic API, if existing checks are insufficient**

- Add `GET /api/device/capabilities` behind existing device authentication, returning `service`, a device-protocol version, server time, supported payload limits, and separate dependency readiness flags. It must not lease tasks, create sessions, expose tokens, or change dose records.
- Optionally accept a short validated correlation nonce and echo it; no arbitrary payload or image upload is needed for this gate.
- Keep heartbeat reachable even when emotion/media dependencies are unavailable. A capabilities response reporting an unavailable model must not be misclassified as network failure.

**Acceptance**: three acknowledged heartbeats, `stop_all=false`, fresh persisted timestamps, and no real task/medication mutation. If the app's configuration is correct but no request is emitted, proceed to P3 before changing network settings.

## P2 — Supply and verify the missing emotion artifact

**Confirmed problem**: `model_fp32_metadata.json` exists, but `model_fp32.onnx` is absent locally and inside the running backend image. `EmotionService` catches its load exception and `/health` still reports overall `status=ok` with `emotion=false`.

**Work**

- Recover the intended seed-43 ONNX export from its trusted project artifact source. The expected fingerprint recorded in the root README is a reference to validate, not proof that a downloaded file is authentic.
- Verify source provenance, checksum, label order, preprocessing version, input dimensions, and ONNX Runtime compatibility. Do not substitute an unrelated emotion model or dummy file just to make readiness green.
- Ensure the artifact is included by the Docker build and is readable by the runtime. Rebuild/redeploy the backend during an appropriate maintenance window; a backend restart invalidates in-memory monitoring sessions.
- Add a build/deployment artifact check and a structured startup error that names the missing dependency without dumping configuration or personal data.
- Keep liveness separate from monitoring readiness. Report which operation is blocked instead of treating all device communication as unavailable.

**Files**: [Dockerfile](../../Dockerfile), [emotion service](../../app/services/emotion_service.py), [model metadata](../../models/emotion_seed43/model_fp32_metadata.json), [health endpoint](../../app/main.py), [device monitor start](../../app/routers/api_device.py).

**Acceptance**: trusted model loads, readiness reports `emotion=true`, and an authorized observe-mode monitor session can be created without recording a dose. Heartbeat success is checked separately.

## P3 — Make communication independent of expensive startup

This is a code-supported risk in the **local bridge**; it has not yet been proven to be the installed app's failure.

**Work**

- Create the authenticated device client and communication supervision before camera/SDK/model initialization. Emit initial status with `robot_reachable=null` or false, then update readiness when dependencies become available.
- Do not claim the camera or models are ready until they actually are. Introduce explicit states such as `connecting`, `ready`, `degraded`, and `unauthorized`, with the last successful heartbeat and sanitized error category.
- Decouple `robot.is_reachable()` from each heartbeat send. Cache a separately refreshed robot status so a blocking SDK connection cannot prevent backend liveness reporting.
- Apply bounded SDK operations and one supervised worker. `asyncio.wait_for(asyncio.to_thread(...))` alone does not terminate a blocked worker thread; use SDK-native timeouts or a managed worker/process strategy that cannot accumulate hung threads on each retry. This design follows the distinction between [async task cancellation](https://docs.python.org/3.11/library/asyncio-task.html#asyncio.wait_for) and [cancelling an already running executor call](https://docs.python.org/3.11/library/concurrent.futures.html#concurrent.futures.Future.cancel).
- Supervise heartbeat, task polling, capture, and uploads. A background task exception must be surfaced to the app manager/diagnostics rather than leaving a process that appears healthy but sends no data.
- Validate response content types and response structure. Convert malformed JSON or a 200 HTML page into a clear wrong-endpoint/protocol error. Currently `response.json()` errors are not normalized into `BridgeError`.
- Add bounded retry/backoff for transient transport failures. Preserve the special behavior for unauthorized devices, withdrawn consent, lost sessions, model unavailability, and validation failures; do not retry all POST operations indiscriminately.
- Record counters for attempted requests, accepted heartbeats, last successful poll, timeout/error category, and dependency readiness. Never log authorization headers, images, or patient information as default diagnostics.

**Files**: [entry point](../../reachy_bridge/__main__.py), [runner](../../reachy_bridge/runner.py), [media adapter](../../reachy_bridge/media.py), [client](../../reachy_bridge/app_client.py), [settings](../../reachy_bridge/config.py), and the actual installed app entry point once obtained.

**Proposed acceptance targets**: first attempted heartbeat within five seconds of validated configuration; three accepted heartbeats within 45 seconds on the healthy test LAN; camera/model startup failure is separately visible while communication continues. These are new targets, not measured current guarantees.

## P4 — Prove image and landmark transfer

**Work**

- Use an explicitly selected diagnostic observe session with current consent, avoiding active real-dose tasks and any existing monitoring session. Creating a session is a state change, so document and end it after the check.
- Prove local frame acquisition first. A working robot control API is not proof of camera readiness. For an on-robot app, prefer the runtime-provided connection if its documented lifecycle supports it; for a remote bridge, validate WebRTC media independently.
- Verify the 640x480 transformed frame, MediaPipe output, session ID, generation ID, positive `frame_seq`, and capture timestamp units.
- Send landmark JSON first, await acknowledgement, then send the associated JPEG multipart request with matching sequence identifiers. Check the response sequence; a 200 can contain unchanged session state if the server ignored a stale frame.
- Preserve bounded flow: the current runner permits one landmark request and one vision request in flight, dropping excess frames rather than accumulating an unbounded queue. Do not replay old camera frames after an outage.
- Measure actual successful landmark rate, JPEG rate, request latency, and bytes. Existing nominal targets are 15 landmark captures/second and at most five JPEG uploads/second; they are not guaranteed delivery rates.
- Assess the current automatic-recording gate: at least 12 landmark frames/second over the server's measurement window and no gap above 0.25 seconds, plus identity and other policy checks. Improve processing/network performance when it fails; do not lower the safety gate to conceal delivery problems.

**Capacity estimate**: if the measured mean JPEG size is `J` bytes and landmark body size is `L`, approximate payload throughput is `5*J + 15*L` bytes/second at nominal maxima, plus HTTP overhead. For example, 50,000-byte JPEGs alone contribute about 250,000 bytes/second (2 Mbit/s). This is an illustrative estimate, not a measurement of this robot. The existing JPEG request size limit is 1,000,000 bytes.

**Acceptance**: matching landmark/JPEG sequences are accepted, backend inference succeeds, observed state advances, and no medication or stock records change during observe-mode verification.

## P5 — Recovery, ordering, and persistence

**Work**

- Test a backend restart, Wi-Fi loss, stale generation, duplicate frame, dropped response, slow model, revoked token, withdrawn consent, and an active browser session.
- Preserve the existing task lease/recovery semantics: check an already leased task first after restart, avoid leasing a second task unnecessarily, and end stale monitoring sessions before replacing them.
- Verify the current 60-second task lease, heartbeat renewal, and ten-second app-unreachable policy against actual timeouts. These constants do not guarantee wall-clock stopping time if an awaited operation blocks.
- Keep live frames ephemeral. If durable queues are introduced later, restrict them to explicitly designed events with stable IDs and expiration; never upload an old camera sequence into a new session.
- Demonstrate domain-level idempotency for a committed event. HTTP success by itself is not exactly-once delivery, and not every endpoint in the current API is idempotent. Apply the retry constraints in [HTTP Semantics, RFC 9110 section 9.2.2](https://www.rfc-editor.org/rfc/rfc9110.html#section-9.2.2): an operation with unknown non-idempotent effects needs evidence that replay is safe.
- Validate a complete outcome only in an isolated test account/database or an explicitly approved supervised real workflow. The existing commit path can update medication stock and notifications; it must not be used as a harmless network ping.

**Acceptance**: reconnection establishes current session state; no stale frames are applied; revoked access stops protected activity; a retried eligible event does not decrement stock twice.

## P6 — Deployment and operator experience

- Record a stable robot/laptop addressing strategy, preferably DHCP reservations appropriate to the local network. Detect an obsolete `DEVICE_BIND` after network changes and report it clearly.
- In Reachy Control and MedAiCare status views, distinguish app running, device connected, camera ready, stream receiving, model ready, and last accepted data time. A single green dot is insufficient.
- Add a protocol/package compatibility report so the robot package, bridge library, and backend versions can be compared before enabling a session.
- Use the normal secret configuration mechanism. Do not embed tokens in repository files, report examples, command history, or log output. Document how to rotate a token without leaving two runners active.
- Keep the optional [PowerShell launcher](../../scripts/reachy-start.ps1) for the Docker deployment only; it does not configure or deploy the already installed on-robot app.
- If LAN HTTP is retained for development, explicitly scope access to the trusted local network. For use beyond that network, design authenticated encrypted transport separately; do not expose port 8001 through a public tunnel as a quick fix.

## Rollout and rollback

1. Capture deployed versions and redacted configuration before making changes.
2. Roll out heartbeat diagnostics before media or inference changes; establish the P1 baseline.
3. Roll out the trusted model artifact and readiness reporting, then verify P2.
4. Roll out sender supervision and frame transfer changes to one runner; measure P3/P4.
5. Complete recovery checks before any real-dose acceptance workflow.
6. Roll back to the known package/image when a gate fails. Preserve database volumes and consent records. Recreate runtime monitoring sessions after a rollback; never restore an old session generation or reuse queued camera frames.

No factory reset, full Python environment reset, broad antivirus exclusion, or automatic re-pairing is part of this plan.
