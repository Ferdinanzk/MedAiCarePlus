# MedAiCarePlus

A local medication-care web app (FastAPI + React) with face-recognition sign-in, live pill-intake monitoring, expression sampling, medication schedules, and LINE notifications for family. An optional Reachy Mini robot reminds the patient at medication time and watches each dose.

**New to this project?**
- [HANDOFF.md](HANDOFF.md): what exists, what works, what is still open.
- [docs/NEW_LAPTOP_SETUP.md](docs/NEW_LAPTOP_SETUP.md): install everything on another computer, step by step.
- [CLAUDE.md](CLAUDE.md): developer ground rules and design decisions.

## Run in Docker

From the project directory in PowerShell:

```powershell
docker compose up -d --build
docker compose ps
Invoke-RestMethod http://localhost:8080/health
```

Open **http://localhost:8080** on this PC. Docker publishes the web app on host port 8080 by default, which leaves host port 8000 free for the Reachy Mini daemon; inside its container the app still listens on port 8000. To publish another host port, set `WEB_PORT` in `.env` and update `MEDCARE_FRONTEND_URL` to the matching browser origin.

The app and its PostgreSQL database run in the `medcareai2` Compose project. The first build can take several minutes. On a fresh copy, copy `.env.example` to `.env` and replace `SECRET_KEY` with a random value before starting. All models are in the repository; only `.env`, enrolled face photos and backups are not.

The Compose services use `Asia/Taipei` as their local timezone. Database data is preserved when the services are rebuilt.

## Use the intake monitor

Register or sign in, complete face enrollment, add a medication and schedule, then open **Medication intake** and select **Start camera** for one scheduled dose. Allow camera access. The app shows identity, expression, and intake progress together. Strong detector events (score at least 0.75) record the selected dose automatically. Eligible uncertain events (at least 0.30) ask for confirmation. **Not taken — undo** reverses an automated record and restores stock. Manual marking and skipping remain available.

The browser must reach the app through `localhost` or HTTPS to use the camera. The detector recognizes a hand-to-mouth intake gesture. **It cannot verify which pill was taken, how many, or that it was swallowed.** Review uncertain results and correct wrong records with the on-screen controls.

## Reachy Mini robot (optional)

At a scheduled dose time, the robot:
1. Wakes up.
2. Looks around until the server recognizes the patient's face.
3. Prompts each medicine with a recorded voice.
4. Watches the dose being taken.

The server, not the robot, decides what is recorded.

- **Robot app:** `medcare_reachy`, installed on the robot from the Hugging Face space `pearlyjam21/medcare_reachy`. Its local working copy is described in [HANDOFF.md](HANDOFF.md).
- **Connection:** the robot talks only to the private device API on host port **8001**. That port is published on the address in `DEVICE_BIND`, which must be this computer's Wi-Fi address. Port 8080 is for browsers only.
- **Vision:** the robot streams camera frames (about 15 per second) to `POST /api/device/monitor/frame`. This computer runs the face, hand and pose models plus face recognition and emotion on each frame. Frames are analyzed in memory and never stored.
- **Recording:** when automatic recording is on and the dose is one solid tablet, a clear hand-to-mouth event is recorded as taken, labelled *observed, pill not verified*. Anything uncertain goes to a family member to confirm through LINE.
- **"I finished":** this only happens if the patient switches on *Let Reachy listen for "I finished"* in the Reachy card. The robot then turns speech into text on the robot itself and listens for 「我吃完了」. If the camera saw nothing, the family is asked to check the pill box.
- **Pairing:** Settings → Reachy robot → review the notice → **Pair**. Enter the one-time `rdv1.` key and the server address `http://<this computer's Wi-Fi IP>:8001` on the robot app's settings page.

## Model sources

| Function | Source used in this build |
| --- | --- |
| Face identification | MedAiCarePlus OpenVINO face detection, landmarks, and re-identification models (`models/face_recognition/intel`, Git LFS), source revision `562d558a5769a72e26822e39a1295db32ff8e791` |
| Pill intake gesture | `D:\clone\medcareai-original-intake-performance`, revision `419dd7187a00c4a84f0d0a70a88bce5aca423db5` (geometry, no neural network) |
| Expression | Seed 43 ONNX export, `models/emotion_seed43/model_fp32.onnx`, SHA-256 `0caaedf04b60d1c95d89ee2162c8bf207ccd669b88865f155b17987cc15ffbad` |
| Face, hand, and pose landmarks (browser) | MediaPipe task models in `frontend_source/public/models`, downloaded by `python scripts/fetch_mediapipe_models.py` |
| Face, hand, and pose landmarks (robot frames) | The same MediaPipe models converted to ONNX, in `models/landmarks/` (six files, pinned by SHA-256 in `app/services/landmark_service.py`) |
| Robot voice and listening | On the robot, in `~/.medcare_reachy/models`: Matcha-TTS `zh-baker` + vocos for the prompts, SenseVoice-Small (int8) + silero VAD for "I finished" |

The expression model predicts Angry, Disgust, Fear, Happy, Sad, Surprise, and Neutral. The face gallery is bind-mounted from `models/face_recognition/face_gallery`, so enrollments survive container rebuilds. PostgreSQL data is in the `medcareai2_pgdata` volume.

`/health` reports each model service. In the default local setup, `ocr: false` (OCR is not installed) and `line: false` (LINE is not configured yet); everything else should be `true`.

## Checks and maintenance

```powershell
docker compose logs --tail 80 app
docker compose down
```

`docker compose down` stops the app and database while preserving the database volume. To apply source changes, run `docker compose up -d --build` again. Run frontend type checking and build from `frontend_source` with `npm run build`.

Backend tests run in a throwaway container built from the app image:

```powershell
docker compose run --rm --no-deps -T -e PYTHONPATH=/app -v "${PWD}/tests:/app/tests" -v "${PWD}/app:/app/app" app sh -c "pip install -q httpx pytest; python -m pytest tests -q -p no:cacheprovider"
```

## APIs

The browser monitor API is under `/api/intake/monitor`: `start`, `landmarks`, `vision`, `outcome`, `end`, `recent`, and `undo`. All routes require the signed-in user's session. The server records one selected scheduled dose and its expression summary in one transaction; repeating a recorded event does not decrement stock again.

The robot device API is under `/api/device/*`, on port 8001 only. It covers tasks, heartbeat, `monitor/start`, `monitor/frame`, `monitor/landmarks`, `monitor/vision`, `monitor/end`, confirmations, and extra events. It authenticates with the robot's `rdv1.` device key, and the app returns 404 for these routes on the public port.
