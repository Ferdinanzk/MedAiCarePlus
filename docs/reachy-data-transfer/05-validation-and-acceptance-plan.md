# Validation and acceptance plan

Status: **proposed checks**, 2026-10-02. The research task did not execute authenticated robot data-transfer tests or create a monitoring session. Previously observed HTTP/service checks are listed in the [report index](README.md).

## What counts as success

| Level | Evidence required | What does not count |
| --- | --- | --- |
| Control | Robot daemon API reachable; expected robot identity | Docker health or a desktop cached device card. |
| Sender | Correct deployed app/config identified and its transfer loop running | App process marked `running` alone. |
| Transport | A response from the laptop device API, initiated in the robot app's execution context | A laptop request to itself. |
| Authorization | Actual device credential accepted with current consent | Port open, unauthenticated 401, or a new unrelated test token. |
| Heartbeat | 200, `stop_all=false`, and a fresh persisted `last_seen_at` | 200 with `stop_all=true`, stdout saying 'sent', or an old timestamp. |
| Stream | Matching session/generation/frame sequence acknowledged and state advances | Camera preview, WebRTC signaling alone, or stale-frame 200 responses. |
| Inference | Frame processed by ready identity/emotion services | A healthy process with `emotion=false`. |
| Outcome | Expected test event persisted once under server policy | Merely uploading a frame or receiving an HTTP acknowledgement. |

## Baseline and prerequisites

- Record package and backend revisions, robot mode, actual sender location, backend address, and time. Redact tokens and user details.
- Use the existing paired credential for connection checks. Use a dedicated test account and isolated data for tests involving task leasing, sessions, outcomes, stock, or notifications.
- Read the installed app's configuration and logs before treating this repository's implementation as deployed truth.
- Run the tests below in order. A later green result does not excuse a missing earlier gate.

## Test matrix

| ID | Scenario | Procedure | Expected result |
| --- | --- | --- | --- |
| T01 | Correct destination | From the sender runtime, call the private device API without authorization. | API 401; no HTML redirect or Reachy control response. No state mutation. |
| T02 | Valid paired device | GET `/api/device/tasks/current` using the actual configured credential. | 204 if idle, or current task metadata; no new task is leased. |
| T03 | First heartbeat | Start the actual communication loop with truthful readiness fields. | Three 200 responses with `stop_all=false` and advancing database timestamps. |
| T04 | Consent denied/revoked | In an isolated test fixture, use valid device credentials with withdrawn consent or revocation. | Protected requests rejected; heartbeat may return `stop_all=true`; no online refresh or monitoring activity. |
| T05 | Invalid token | Use an invalid token in a test fixture. | 401 and explicit authentication diagnosis; no automatic pairing or silent success. |
| T06 | Wrong service/HTML | Stub a 200 HTML response and an API 404/405 response. | Sender reports wrong endpoint/protocol; no unhandled background-loop death. |
| T07 | Slow camera/model initialization | Block or fail media/model initialization in a controlled harness. | Heartbeat continues with degraded readiness; worker count remains bounded. This is a proposed improvement. |
| T08 | Model readiness | Verify trusted ONNX artifact and attempt authorized observe-session creation. | `emotion=true`, session created. Without the artifact: explicit 503, heartbeat still operational. |
| T09 | Normal ordered frames | Send matching landmark JSON and JPEG for one observe session. | Positive sequence advancement, paired inference, no dose or stock mutation. |
| T10 | Missing or stale sequence | Send a JPEG without its matching stored packet, an old frame sequence, then an old generation. | Current behavior may return unchanged state for an ignored frame; stale generation is rejected with 409. No wrong-frame inference. |
| T11 | Oversized/malformed upload | Submit a test JPEG over 1,000,000 bytes and malformed payloads. | Expected 413/validation response; no crash or credential leak. |
| T12 | Sustained throughput | Run an observe-mode stream for a proposed ten-minute trial. | Measure accepted rates, latency, frame gaps, drops, memory, and CPU; judge automatic-recording eligibility against actual server gates. |
| T13 | Backend outage/restart | In a test deployment, interrupt connectivity and restart backend. | Clear outage state, bounded retries, fresh session after restart; no replay of stale camera frames. |
| T14 | Lost HTTP response | Simulate a committed operation whose response is lost. | Endpoint-specific retry policy; idempotent event commit does not change stock twice. Do not assume all POSTs are safe to repeat. |
| T15 | Competing clients | Start an authorized browser monitor before the robot in a test account. | `busy_other_client` / 409 respected; robot does not take over silently. |
| T16 | Two runners | Configure a duplicate sender only in a test harness. | Deployment prevents duplicate ownership or exposes it clearly; no interleaved frames in one session. |
| T17 | Real outcome persistence | Use an isolated account/database with synthetic dose data and controlled detection evidence. | One expected monitor event and one corresponding stock/status change; retry leaves counts unchanged. |
| T18 | Address/firewall change | In a controlled environment, make the backend bind/address unavailable. | Sender reports connection failure with correct endpoint; diagnosis does not recommend disabling protection blindly. |

These rows specify intended behavior. They do not claim every proposed resilience or diagnostic feature already exists.

## Measurement record

For each run, record:

```text
run_id:
timestamp_and_timezone:
robot_app_revision:
backend_revision:
sender_location: robot | docker | windows
device_api_base_url:
auth_configured: true | false
first_request_at:
first_accepted_heartbeat_at:
last_persisted_heartbeat_at:
stop_all:
model_readiness:
frames_captured:
landmark_requests_attempted / acknowledged:
jpeg_requests_attempted / acknowledged:
frames_dropped_or_ignored:
accepted_landmark_fps:
jpeg_payload_bytes_mean:
request_latency_p50_ms / p95_ms:
largest_frame_gap_ms:
session_replacements:
last_error_category:
expected_test_event_count / actual_count:
```

Do not record raw authorization headers, token contents, patient identifiers, or camera images in this default diagnostic record. Use temporary correlation IDs; restrict any necessary deeper capture to a controlled test dataset.

## Proposed release gates

1. **Connectivity gate:** the real sender produces three accepted heartbeats within 45 seconds after configuration validation. This target excludes intentional permission/configuration failures and is not a claim about current behavior.
2. **Idle stability gate:** thirty minutes of heartbeats with no unexplained gaps over 25 seconds on the test LAN. No-task 204 responses and zero image FPS are valid while idle.
3. **Stream gate:** ten-minute observe session with paired acknowledgements, no memory growth from queues, and measured performance against server eligibility thresholds. Any frame-rate shortfall remains visible and preserves degraded/confirmation behavior.
4. **Recovery gate:** the agreed outage, restart, invalid-credential, consent, and stale-session tests pass. No stale frame replay or duplicate domain mutation.
5. **Outcome gate:** one complete isolated test demonstrates end-to-end storage exactly once at the domain-event level. This is distinct from a clinical validation of gesture detection.

Failure at a gate produces a concrete issue containing the redacted measurement record and responsible layer. Do not mark the system ready simply because the Reachy app and Docker containers remain running.
