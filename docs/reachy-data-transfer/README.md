# Reachy to MedAiCare data transfer

Research date: **2026-10-02**, Asia/Taipei. This package documents the current system and proposes implementation work. The proposals are not already implemented.

## Read first

The immediate success criterion is **an authenticated heartbeat sent by the real robot application, accepted by MedAiCare, and visible as a fresh database timestamp**. A running robot app, a healthy Docker container, and a reachable robot control API do not establish that result.

The preferred first investigation is the already installed **`medcare_reachy` application on the Wireless robot**. Its deployed source, configuration, and startup logs have not been inspected. The local `reachy_bridge` code is useful evidence for the intended protocol, but it is not proof of what the installed application executes. Do not start a second bridge as a substitute for identifying the first application's failure.

## Documents

| Document | Purpose |
| --- | --- |
| [01 — Research and architecture](01-research-and-architecture.md) | Official Reachy/Docker sources, deployment choices, and network direction. |
| [02 — Data contracts and failure modes](02-data-contracts-and-failure-modes.md) | What the repository sends, accepts, stores, and retries. |
| [03 — Diagnostic runbook](03-diagnostic-runbook.md) | Evidence-driven checks from the actual sender through database acknowledgement. |
| [04 — Implementation plan](04-implementation-plan.md) | Ordered work packages, code ownership, acceptance gates, and rollback. |
| [05 — Validation and acceptance plan](05-validation-and-acceptance-plan.md) | Tests that prove transfer without confusing it with medication recording. |

## Observed deployment

Snapshot: **2026-10-02 00:09:19 +08:00**. IP addresses are observations, not guaranteed permanent assignments.

| Component | Observed address/state | What it establishes |
| --- | --- | --- |
| Reachy Mini Wireless | `192.168.49.81:8000`; daemon `running`; Wireless mode | Laptop can contact the robot's control API. |
| Installed robot application | `medcare_reachy`, `running`, `error=null` | Process status only; its backend configuration is still unknown. |
| Laptop Wi-Fi | `192.168.49.32` | Address currently used for the robot-facing Docker publication. |
| MedAiCare web application | `http://localhost:8080`; container healthy | Laptop can contact the public application. |
| Private device API | `http://192.168.49.32:8001` | An unauthenticated laptop GET to `/api/device/tasks/current` returned 401 in the preceding checks. Robot-originated access is unproven. |
| Paired device registry | One device, one active device, `last_seen_at` NULL | No heartbeat has been recorded for that device. This alone does not identify the cause. |
| Backend model readiness | `face_recognition=true`, `emotion=false`, `intake_detection=true` | A separate blocker exists for starting a monitoring session. |
| Emotion artifact | `/models/emotion_seed43/model_fp32.onnx` absent; metadata present | Read-only initialization reproduced ONNX Runtime `NO_SUCHFILE`. |
| Optional Docker bridge | No running bridge container | Missing local bridge settings do not prove that the on-robot app has missing settings. |

Repository baseline: `923d2d9eac280183e15b1f1629464370a965f907`, plus the current uncommitted working-tree changes. Current robot daemon version observed during the preceding investigation: `1.11.0`. Live status and upstream documentation may change after this snapshot.

## Two independent blockers

1. **Transport/authentication is not demonstrated.** No persisted heartbeat exists. Possible causes include the installed app's URL/token/configuration, a blocked robot-to-laptop connection, consent rejection, application initialization, or a background loop failure. These are hypotheses until the corresponding evidence is collected.
2. **Monitoring is not ready.** The missing ONNX file makes the emotion service unavailable. The current `/api/device/monitor/start` implementation requires both identity and emotion services and therefore returns 503 once authentication succeeds. Fixing this artifact will not by itself solve the missing heartbeat.

Use Reachy Mini Control for the robot's lifecycle and logs. MedAiCare's own application supplies pairing and device status; it is a separate product interface. None of these plans require the old Reachy web dashboard.

## Corrections to avoid repeating the earlier diagnosis

- MedAiCare previously logged Reachy API paths and answered an app-start POST with 405. That proves those requests reached the wrong service, but those log lines alone did not identify the host port.
- Docker's old host-port 8000 publication was removed in favor of 8080. This avoids local address confusion and Lite/simulation conflicts. The discovered robot is **Wireless**, so that change alone does not prove or repair a Wi-Fi disconnect.
- Reachy Control's cached `192.168.49.81` address was verified to be the live robot; there is no basis for clearing it as stale.
- No evidence presently establishes McAfee as the cause. A firewall hypothesis requires robot-originated connection results and matching security events.
- A 200 heartbeat response with `stop_all=true` is not a successful online-state update: the current server deliberately returns before updating `last_seen_at` in that case.

The first implementation milestone is to resolve these uncertainties and show three successive accepted heartbeats. Camera transfer, inference, and persisted medication outcomes follow as separate milestones.
