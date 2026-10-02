# reachy_bridge

Robot-side SDK for Phase 2 (spec `mdPlanReachy/01-reachy-camera-wake-intake.md`). It pulls camera frames
from a Reachy Mini Wireless, runs the browser worker's MediaPipe face/hand/pose models (converted to ONNX, on
ONNX Runtime, because the MediaPipe library can't load on the robot's Raspberry Pi 4), drives the
slot state machine, and plays prerecorded prompts. It talks **only** to the app's private device API
(`/api/device/*` on port 8001) with the `rdv1.` device token shown once at pairing. It has no DB access
and makes no recording decisions; the server's policy is authoritative. **No microphone** is opened.

## Run

Docker (opt-in profile, no published bridge ports):

```powershell
# From any working directory, run this repository helper:
& "C:\medcareai\MedAiCarePlus\scripts\reachy-start.ps1"

# Check configuration and Docker readiness without building or starting anything:
& "C:\medcareai\MedAiCarePlus\scripts\reachy-start.ps1" -CheckOnly
```

The helper locates `docker-compose.yml` and `.env` relative to its own file, checks that Docker is
running, reads the resolved Compose configuration in memory, validates bridge settings without
displaying them, and checks the configuration before it builds or starts `reachy-bridge`. It never
creates or overwrites `.env`.

The Docker bridge is an optional runner. If the installed `medcare_reachy` app on the robot is already
handling MedAiCare integration, use that runner and leave this Docker bridge stopped. Run only one
runner for the same Reachy device at a time. Reachy Mini Control is the desktop app for managing the
robot and its installed app; the outdated robot web interface is not part of setup.

If choosing the Docker bridge, launch/connect the robot with the **Reachy Mini Control desktop app**
and set `REACHY_ROBOT_HOST` to the robot's LAN address. Pair the bridge from the **MedAiCare app UI**
and set `REACHY_DEVICE_TOKEN` to the resulting `rdv1.` device token. Set these values in the
repository `.env` or in the current PowerShell process; the helper reports missing values by name
and never prints the token.

To follow bridge logs:

```powershell
docker compose --file "C:\medcareai\MedAiCarePlus\docker-compose.yml" --profile reachy logs --follow reachy-bridge
```

> **Unverified:** the Linux GStreamer/WebRTC setup in `Dockerfile.bridge` has not been exercised against
> a real robot. It stays unverified until Phase 0 spike M1 (`spike/m1_m2_media_probe.py`) passes from
> inside a container (default network with a fixed robot IP, then Docker Desktop host networking).

**Windows-native fallback** (the reachy-mini Windows wheels ship GStreamer), from the repo root:

```powershell
pip install -r reachy_bridge/requirements.txt
$env:APP_INTERNAL_URL="http://localhost:8001"; $env:REACHY_DEVICE_TOKEN="<rdv1 device token>"; $env:REACHY_ROBOT_HOST="<robot LAN address>"
python -m reachy_bridge
```

The app publishes device API port 8001 using `DEVICE_BIND` from `.env`. If it is set to the laptop's
LAN address `192.168.49.32`, point this native bridge at `http://192.168.49.32:8001`:

```powershell
$env:APP_INTERNAL_URL="http://192.168.49.32:8001"
```

If `DEVICE_BIND` is unset, Compose binds port 8001 to `127.0.0.1` by default, so keep
`APP_INTERNAL_URL="http://localhost:8001"` for a bridge running on that same laptop.

Hardware-free: `ROBOT_BACKEND=video VIDEO_PATH=clip.mp4 python -m reachy_bridge` replays a file as the
camera and logs motion/audio.

| Env | Default | |
|---|---|---|
| `APP_INTERNAL_URL` | `http://localhost:8001` | compose sets `http://app:8001` |
| `REACHY_DEVICE_TOKEN` | — | required, `rdv1.…` |
| `REACHY_ROBOT_HOST` | — | required for `ROBOT_BACKEND=reachy` |
| `ROBOT_BACKEND` | `reachy` | `reachy` or `video` |
| `VIDEO_PATH` | — | required for `video` |
| `LANGUAGE` | `zh-TW` | `zh-TW` or `en` (clip folder) |
| `MODELS_DIR` | `reachy_bridge/models` | the six ONNX models in `vision.MODEL_FILES` (from the `medcare_reachy` app) |
| `CLIPS_DIR` | `reachy_bridge/clips` | WAVs at `<CLIPS_DIR>/<LANGUAGE>/<clip_id>.wav` |

## Clips

`clips/manifest.json` lists every prompt with zh-TW and en text, all `pending_clinician_review`. No WAVs are
committed. Generate them with a TTS engine of your choice (`python -m reachy_bridge.tools.make_clips
--engine pkg.module:fn --language zh-TW`), then have the text and audio reviewed before release. A missing
WAV is logged, skipped, and reported as `missing_clips` in the heartbeat. Optional per-medication prompts
are `med_<med_id>.wav`; otherwise `med_prompt_generic` plays.

## Layout

- `session.py` — pure `SlotSession` state machine (injected clock, robot, clips, stream, app client)
- `runner.py` — task long-poll, 10 s heartbeat (`stop_all`), 15 fps capture → landmarks every frame,
  identity-only JPEG ≤ 2 fps, one request in flight per stream
- `emotion.py` — the server's seed-43 emotion model on the robot (≤ 2 fps, only the face the server verified, never
  with a hand over the mouth); the score rides on the landmark packet as `emotion` and the server re-checks it
- `app_client.py` — httpx client; 409 → `SessionLost` (`BusyOtherClient` for `busy_other_client`),
  401/403 → `NotAuthorised`, network/5xx → `AppUnreachable`, 503 → `ServiceUnavailable`
- `vision.py` / `mp_geometry.py` — the Tasks face/hand/pose landmarkers on ONNX Runtime (anchors, NMS, rotated
  crops, VIDEO-mode tracking); `packets.py` — results → the browser worker's landmark packet
- `tools/bench_vision.py` — per-model timings and the expected landmark fps on this machine
- `media.py` — `Robot` protocol, `ReachyRobot` (lazy `reachy_mini` import), `VideoFileRobot`

## Tests

```powershell
python -m pytest reachy_bridge/tests -q
```

No network, hardware, models, or `reachy_mini` needed (tests need `httpx`, `numpy`, `pillow`). The model-accuracy
test runs only when `MODELS_DIR` holds the ONNX models.
The index-parity test is skipped when the package is used outside the full app repository.
