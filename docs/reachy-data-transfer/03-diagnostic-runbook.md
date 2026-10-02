# Reachy data-transfer diagnostic runbook

Use this runbook to locate the boundary where the installed Reachy app stops reaching MedAiCare. It assumes the current private Wi-Fi setup:

| Component | Address | Purpose |
| --- | --- | --- |
| Reachy Mini Wireless | `192.168.49.81` | Robot daemon and installed `medcare_reachy` app |
| MedAiCare laptop | `192.168.49.32:8001` | Private device API used by the installed app |
| MedAiCare laptop | `:8080` | Browser UI and public app API |
| Reachy daemon | robot port `8000` | Robot control API; not the MedAiCare device API |

Use the Reachy Mini Control desktop app to inspect the installed app and its logs. The old robot web interface is outdated and is not part of this procedure. Do not run the optional Docker `reachy-bridge` while `medcare_reachy` is handling the same device.

## Safety and data handling

- Start with the read-only checks below. Do not create a task, claim a dose, re-pair, change consent, or replay a camera frame just to test connectivity.
- Pairing returns the `rdv1.` device token once; the database stores only its hash. Never query or copy the hash, paste the token into a ticket/chat, or print it in a log. Re-pairing revokes the existing device and token, so do not use it as a recovery shortcut.
- A manual heartbeat is an authenticated write. Use it only when the account holder has current core and robot-camera consent and has approved the check. A heartbeat updates `last_seen_at` and the device status fields; it does not create a medication event. Prefer observing the installed app's regular heartbeat.
- Camera frames and landmarks are sent from the robot to the laptop API. A successful upload/HTTP response confirms transfer and server acceptance only; it does not by itself mean an intake event was recorded.

## 1. Confirm the app is ready to accept a monitor session

From PowerShell on the laptop, check the public health endpoint:

```powershell
Invoke-RestMethod http://localhost:8080/health | ConvertTo-Json -Compress
```

For the current snapshot, health reports `status: ok`, `face_recognition: true`, `intake_detection: true`, and `emotion: false`. Heartbeats and task polling do not require the emotion model, but `/api/device/monitor/start` returns **503** unless both face recognition and emotion services are available. The current emotion service failure is separate from the network path: `models/emotion_seed43/model_fp32.onnx` is absent from the running container (the metadata file alone is insufficient).

The repository README lists an expected SHA-256 for the ONNX artifact. Before any model change, establish the artifact's source and verify its hash against that recorded value; the source provenance listed there has not been independently confirmed by this diagnostic. After the artifact is restored through the normal image/update process, check `/health` again. Do not troubleshoot frame uploads until `emotion` and `face_recognition` both report `true`.

## 2. Prove the installed app can reach the correct API

The robot calls `http://192.168.49.32:8001/api/device/...`. Port 8001 is the private, device-token API. Port 8080 serves MedAiCare's browser UI. Robot port 8000 is Reachy's daemon. A laptop request to its own `localhost` or a successful browser page at 8080 does not prove the robot can reach port 8001.

First verify the laptop publishes the private port, without dumping the full Compose environment:

```powershell
docker compose port app 8001
```

It should report a mapping on `192.168.49.32:8001` for this setup. If it reports `127.0.0.1:8001`, the API is loopback-only and the robot cannot connect; `DEVICE_BIND` must be the laptop's private Wi-Fi address for this deployment.

Then make an unauthenticated, read-only route probe **from the same execution context and network namespace as the installed `medcare_reachy` app**. For example, use the app's supported diagnostic shell/console. An SSH shell on the robot is useful only if it shares the installed app's network context; otherwise it proves robot-OS reachability, not app reachability. Do not add a new SSH host key or bypass host verification. If using an already authenticated, trusted SSH session, run:

```sh
curl --connect-timeout 3 -sS -o /dev/null -w 'HTTP %{http_code}\n' \
  http://192.168.49.32:8001/api/device/tasks/current
```

With no `Authorization` header, **401** is the expected result: the request reached the correct device API and was rejected for missing credentials. This probe does not lease a task or change data. Run it from the installed app context; a laptop-side `Test-NetConnection` is not a substitute.

An optional comparison from the robot context to `http://192.168.49.32:8080/api/device/tasks/current` should return **404**. Device routes are intentionally unavailable on the public web port. Do not send a device token to port 8080.

### If the same-context probe cannot connect

Record the time, source address (`192.168.49.81`), destination (`192.168.49.32:8001`), and whether it timed out or was refused. Before changing a firewall rule, confirm the Compose port mapping above and inspect McAfee Firewall's event/log view for an inbound block matching that source, destination, protocol TCP, and timestamp. Also check that the laptop Wi-Fi network is classified as private/trusted. A timeout plus a matching McAfee block is evidence for a firewall rule; a refused connection more often points to a missing listener or wrong bind/mapping.

If the logs confirm the block, make only a narrow inbound TCP allow for source `192.168.49.81`, destination laptop `192.168.49.32`, destination port `8001`, on the private/trusted Wi-Fi profile. Keep the rule limited to this robot and port. Do not disable McAfee/the firewall, allow all inbound traffic, expose port 8001 on public networks, or open port 8080 for device traffic. Repeat the same-context probe and confirm it now reaches the API (401 without credentials).

## 3. Check pairing, consent, and heartbeat

In the signed-in MedAiCare UI on port 8080, check Reachy status. It should show an active paired device and current robot-camera consent. The device API requires current core and robot-camera consent. The health endpoint does not check either consent or pairing.

The installed app normally sends a heartbeat every 10 seconds. Check the database without selecting token hashes or patient identifiers:

```powershell
docker compose exec -T postgres psql -U medai -d medcareai2 -c "SELECT label, (revoked_at IS NULL) AS active, last_seen_at, robot_reachable, landmark_fps FROM reachy_device ORDER BY created_at DESC LIMIT 5;"
```

For the active paired row, `last_seen_at` should advance as heartbeats arrive. The UI considers the robot online for up to 60 seconds after a heartbeat. A `NULL` value means the backend has not persisted any heartbeat for that row; it does not identify whether the cause is network, token, consent, or whether the installed app is actually running.

The robot app's authenticated heartbeat returns HTTP 200 with `stop_all: false` when its token is active and consent is current, and updates `last_seen_at`. If the device is revoked or consent is withdrawn, heartbeat can return HTTP 200 with `stop_all: true` and does not update `last_seen_at`. Other device API calls return 401 for an invalid/revoked token and 403 when consent is missing. Confirm status and consent in the UI. Do not re-pair automatically: that revokes the current device and requires securely delivering the newly shown-once token to the installed app.

If an operator has explicitly approved a manual heartbeat and the **currently approved token is already available as a protected environment variable** in the app's own execution context, the following is a controlled write probe. Do not put a literal token in the command, shell history, or output. Set `ROBOT_REACHY_TOKEN` through the existing approved secret mechanism; do not retrieve a token hash or recover a lost token from the database.

```sh
test -n "$ROBOT_REACHY_TOKEN" && \
curl --connect-timeout 3 -sS -X POST \
  -H "Authorization: Bearer $ROBOT_REACHY_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"robot_reachable":true}' \
  http://192.168.49.32:8001/api/device/heartbeat
```

Only report the HTTP status and `stop_all` result. Do not run this if robot reachability has not been confirmed or consent is not current. A 200 with `stop_all: true` is a stop instruction, not heartbeat success for database presence.

## 4. Confirm task polling before testing a session

Observe the installed app's own task-poll logs. An authenticated `GET /api/device/tasks/next` returning **204** means the request succeeded and no task was available; it is normal while there is no queued dose. A **200** means a task was returned. Do not invoke `/tasks/next` manually as a diagnostic: it can lease a queued task. `GET /api/device/tasks/current` is the read-only check for an already leased task and can return 204 when none is held.

Only proceed with an already intended task. Do not create a real medication task just to test networking. Once a task is available, the app requests `/api/device/monitor/start`; this must pass the model readiness gate in step 1. A browser-held live monitor session can cause **409 `busy_other_client`** because the first active client owns that session.

## 5. Separate transfer, acceptance, and persistence

The normal data path is:

1. The Reachy app captures frames locally and computes face/hand/pose landmarks.
2. It posts landmark JSON to `/api/device/monitor/landmarks` and, at a lower rate, JPEG frames to `/api/device/monitor/vision` (maximum 1,000,000 bytes per frame).
3. A 2xx response means the server accepted that request. A monitor response may include session state such as identity, detector, candidate, and recorded status; these values are in-memory session state and are not proof of a persisted medication event.
4. Only a server-authorized commit writes an intake outcome and `monitor_event`; cases needing caregiver review follow the confirmation path. Verify the resulting dose state in MedAiCare's UI/history. A transferred frame, accepted landmark packet, candidate, or task status alone is not a completed medication record.

Do not save frame bodies or include them in diagnostics. Keep captures, tokens, names, face labels, and session identifiers out of tickets. If the API accepts frames but there is no final outcome, inspect the monitor state in the approved UI/app logs and verify the session's identity and detector state; do not treat transport success as clinical success.

## HTTP status guide

| Result | Meaning in this flow | Next check |
| --- | --- | --- |
| Network timeout / no route | Robot execution context did not reach laptop port 8001. | Check exact execution context, Wi-Fi/subnet, Compose mapping, then McAfee block evidence. |
| Connection refused | Laptop reached but no listener accepted the connection. | Check `docker compose port app 8001`, `DEVICE_BIND`, and app/container availability. |
| **200** | Request accepted; for heartbeat, inspect `stop_all`. | Verify DB/UI heartbeat or session/task state as appropriate. |
| **204** | No task is available/current; normal for an empty poll. | Check for a queued task only if one is expected. |
| **401** | Missing/invalid/revoked device token, or deliberate unauthenticated probe. | For probe, this proves API identity. For the app, check the currently approved token securely; never re-pair automatically. |
| **403** | A standard device request lacks current consent. | Check current core and robot-camera consent in MedAiCare. Heartbeat instead returns 200 `stop_all: true` for revoked device/withdrawn consent. |
| **404** | Wrong port/path or unknown task. Device API routes on 8080 intentionally return 404. | Use port 8001 and the current `/api/device/...` paths; do not use legacy `/api/apps/...` paths. |
| **405** | HTTP method does not match the route, or a legacy endpoint was called. | Use the current API method/path; task polling is GET, heartbeat is POST. |
| **409** | Task/dose is no longer valid, lease ownership changed, or another client owns the monitor. | Check current task and active browser monitor; do not retry stale task/session IDs. |
| **413** | JPEG exceeds the 1 MB endpoint limit. | Let the app's configured downscaler send a smaller frame; do not log or attach the image. |
| **422** | Payload fields or types failed validation. | Check installed app/API version and request shape; do not expose payloads containing identifiers. |
| **503** | A server dependency is unavailable. For monitor start, face or emotion service is not ready. | Check `/health`; in the current snapshot emotion is false because the ONNX file is missing. Restore and hash-verify the documented artifact before session testing. |

## Completion criteria

The transfer path is proven only when a request from the installed app's actual execution context gets the expected unauthenticated 401, the installed app's own authenticated heartbeat advances the active device's `last_seen_at` and UI status, and its task poll receives 204 when the queue is empty (or 200 when an intended task exists). Monitor transfer is a separate gate: both required models must be ready, the app must receive accepted responses for its session packets, and any claim of a recorded dose must be confirmed in MedAiCare's persisted dose history.
