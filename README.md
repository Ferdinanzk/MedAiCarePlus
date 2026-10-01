# MedAiCarePlus concurrent intake monitor

This local web app uses one camera session to identify the signed-in person, follow that person's pill intake gesture, and sample their facial expression. A bystander can remain in view; the monitor only feeds hands associated with the signed-in person's face and pose to the intake detector.

## Run in Docker

From `D:\medcareai2_20260920\app` in PowerShell:

```powershell
docker compose up -d --build
docker compose ps
Invoke-RestMethod http://localhost:8000/health
```

Open **http://localhost:8000** on this PC. The app and its dedicated PostgreSQL database run in the `medcareai2` Compose project. The first build can take several minutes. A prepared `.env` in this workspace contains a randomly generated signing key. If recreating this workspace, copy `.env.example` to `.env` and replace `SECRET_KEY` with a random value before starting.

The Compose services run with `Asia/Taipei` as their local timezone, matching this PC. Existing database data is preserved when the services are rebuilt.

Register or sign in, complete face enrollment, add a medication and schedule, then open **Medication intake** and select **Start camera** for one scheduled dose. Allow camera access. The app shows identity, expression, and intake progress together. Strong detector events (score at least 0.75) record the selected dose automatically; eligible uncertain events (at least 0.30) ask for confirmation. **Not taken — undo** reverses an automated record and restores stock. Manual marking and skipping remain available.

The browser must access the app through `localhost` or HTTPS to use the camera. The detector recognizes a hand-to-mouth intake gesture; it cannot verify the pill's identity, number of pills, or swallowing. Review uncertain results and correct wrong records with the on-screen controls.

## Model sources

| Function | Source used in this build |
| --- | --- |
| Face identification | MedAiCarePlus OpenVINO face detection, landmarks, and re-identification models, source revision `562d558a5769a72e26822e39a1295db32ff8e791` |
| Pill intake gesture | `D:\clone\medcareai-original-intake-performance`, revision `419dd7187a00c4a84f0d0a70a88bce5aca423db5` |
| Expression | `D:\newEmotion` seed 43 ONNX export, `models/emotion_seed43/model_fp32.onnx`, SHA-256 `0caaedf04b60d1c95d89ee2162c8bf207ccd669b88865f155b17987cc15ffbad` |
| Face, hand, and pose landmarks | Browser MediaPipe task models in `frontend_source/public/models` |

Only the original repository's **face identification** weights are loaded. The new expression model predicts Angry, Disgust, Fear, Happy, Sad, Surprise, and Neutral. The Docker health endpoint reports `ocr: false` and `line: false` in the default local setup because their optional services are not configured.

Model binaries are included in this working directory and copied into the Docker image at build time. The face gallery is bind mounted from `models/face_recognition/face_gallery`, so enrollments survive container rebuilds. PostgreSQL data is in the `medcareai2_pgdata` volume.

## Checks and maintenance

```powershell
docker compose logs --tail 80 app
docker compose down
```

`docker compose down` stops the app and database while preserving the database volume. To apply source changes, run `docker compose up -d --build` again. Run frontend type checking and build from `frontend_source` with `npm run build`.

The monitor API is under `/api/intake/monitor`: `start`, `landmarks`, `vision`, `outcome`, `end`, `recent`, and `undo`. All routes require the existing user session. The server records one selected scheduled dose and its expression summary in one transaction; repeating a recorded event does not decrement stock again.
