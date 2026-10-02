# Setting up MedAiCarePlus on a different laptop

This guide installs the server and web app on a new Windows laptop and points the Reachy Mini at it. It takes about an hour, mostly downloads and the first Docker build.

There are two cases:

- **Moving** (most likely): the new laptop takes over from the old one. Keep the patients, schedules, enrolled faces and robot pairing. Do every step.
- **Fresh install**: a new, empty system. Skip the steps marked *(moving only)*, and pair the robot again in step 9.

Commands are for **PowerShell**, run from the project folder unless stated otherwise.

---

## 0. On the old laptop: collect what isn't in git

The code, every model the app needs (emotion, landmarks, browser MediaPipe models), the LINE QR code, `HANDOFF.md` and the robot app (`reachy_app/`) are all in the GitHub repository. Only private or personal files are not. Gather them onto a USB drive or a private network share. **Never upload `.env`, the database dump or the face gallery anywhere public.**

| Item | Path on the old laptop | Needed for |
| --- | --- | --- |
| `.env` | `C:\medcareai\MedAiCarePlus\.env` | Keys and settings. **Keep the same `SECRET_KEY`**: it signs the robot's pairing key and logins. |
| Database dump *(moving only)* | made below | Patients, medications, doses, consents, robot pairing |
| Face gallery *(moving only)* | made below | Enrolled faces for face recognition |
| Deletion ledger *(moving only)* | `ledger\deletion_ledger.jsonl` | Required by the restore script |

Make the database dump and the face-gallery archive. Run these in the project folder on the old laptop, with the stack running:

```powershell
docker compose exec -T postgres pg_dump -U medai -Fc -f /tmp/database.dump medcareai2
docker compose cp postgres:/tmp/database.dump .\database.dump
tar -C models\face_recognition\face_gallery -cf gallery.tar .
```

> Don't write the dump with `> database.dump` in Windows PowerShell 5.1. It re-encodes the output and corrupts the binary file. Use `-f` plus `docker compose cp`, as above.

**Server code changes:** if the old laptop has uncommitted changes on the `reachy-integration` branch (`git status`), either commit and push them first, or copy the whole `MedAiCarePlus` folder instead of cloning.

Once the new laptop works, **turn the old laptop's stack off** (`docker compose down`). If both run, the robot might still be pointed at the old one.

## 1. Install the tools on the new laptop

1. **Docker Desktop for Windows.** Install, start it, and enable **Settings → General → Start Docker Desktop when you sign in**.
2. **Git for Windows**, then **Git LFS**: run `git lfs install` once. The face-recognition model files are stored in Git LFS.
3. **Python 3.10 or newer.** Only needed for two helper scripts: fetching the browser models and deploying the robot app. For the robot deploy: `pip install paramiko`.

## 2. Get the code

Clone with HTTPS (SSH cloning may fail on a new machine without GitHub's host key), or copy the folder from the old laptop:

```powershell
cd C:\medcareai
git clone --branch reachy-integration https://github.com/Ferdinanzk/MedAiCarePlus.git
cd MedAiCarePlus
git lfs pull
```

Check that the face models are real files, not LFS pointer stubs of about 130 bytes:

```powershell
Get-ChildItem models\face_recognition\intel -Recurse -Filter *.bin | Select-Object Name, Length
```

Each `.bin` should be hundreds of KB or more.

> **Windows line endings:** if the `backup` container keeps restarting with `set: illegal option -`, `scripts/backup/backup.sh` was checked out with Windows (CRLF) line endings. Convert it to LF and rebuild: `docker compose up -d --build backup`.

## 3. Check the model files

The models come with the clone. Check that they are real files, not missing or pointer stubs:

```powershell
Get-ChildItem models\emotion_seed43\*.onnx, models\landmarks\*.onnx, frontend_source\public\models\*.task | Select-Object Name, Length
```

Expect `model_fp32.onnx` at about 16.8 MB, six landmark `.onnx` files, and three `.task` files. If the `.task` files are ever missing, `python scripts\fetch_mediapipe_models.py` downloads them. The server checks the landmark models' SHA-256 and reports `landmarks: false` in `/health` if any is missing or different.

## 4. Find the new laptop's Wi-Fi address

The robot must reach this laptop over Wi-Fi, so it needs the laptop's address on that network:

```powershell
ipconfig
```

Under **Wireless LAN adapter Wi-Fi**, note the **IPv4 Address**, e.g. `192.168.49.40`. The robot and the laptop must be on the same Wi-Fi.

Recommended: reserve this address for the laptop in the router or hotspot settings. If it changes later, both `.env` and the robot's settings must be updated.

## 5. Create `.env`

- **Moving:** copy the old `.env` into the project folder, then change only `DEVICE_BIND`.
- **Fresh install:** start from the example and generate a new `SECRET_KEY`:

```powershell
Copy-Item .env.example .env
python -c "import secrets; print(secrets.token_hex(32))"   # paste as SECRET_KEY
```

Settings that matter:

| Variable | Value |
| --- | --- |
| `SECRET_KEY` | Moving: **the old value**. Fresh: the random value generated above. |
| `DEVICE_BIND` | This laptop's Wi-Fi IPv4 address from step 4. Never `0.0.0.0`. |
| `WEB_PORT` | `8080`, so port 8000 stays free for the Reachy daemon |
| `MEDCARE_FRONTEND_URL` | `http://localhost:8080` |
| `OPENROUTER_API_KEY`, `LLM_MODEL` | Optional; only for future conversation features |
| `LINE_CHANNEL_ACCESS_TOKEN`, `LINE_CHANNEL_SECRET` | Leave empty until LINE is set up |
| `BACKUP_PASSPHRASE` | A strong passphrase for the weekly encrypted backup. Store it somewhere else too. |

## 6. Build and start

```powershell
docker compose build
```

- **Fresh install:**

  ```powershell
  docker compose up -d
  ```

- **Moving:** copy the deletion ledger into the project, then restore the database and faces. The restore script starts the services itself when it finishes.

  ```powershell
  Copy-Item E:\handoff\deletion_ledger.jsonl ledger\
  .\scripts\restore.ps1 -Dump E:\handoff\database.dump -Gallery E:\handoff\gallery.tar
  ```

  It should end with `Restore completed; deletion ledger replayed and services started (exit 0).`

Check that the stack is up:

```powershell
docker compose ps
Invoke-RestMethod http://localhost:8080/health
```

Expected: `face_recognition`, `emotion`, `intake_detection` and `landmarks` are `true`; `ocr` and `line` are `false`.

Open **http://localhost:8080** and sign in:
- **Moving:** your existing account and face work as before.
- **Fresh install:** register and enroll your face.

## 7. Keep the laptop awake

The robot can't see without the laptop. Run once, in an **administrator** PowerShell:

```powershell
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /setactive SCHEME_CURRENT
```

This means: never sleep or hibernate on AC power, and do nothing when the lid closes. Keep the charger plugged in. Also set Windows Update **active hours** to cover medication times, so a restart doesn't land on a dose.

## 8. Let the robot reach the device port

From another device on the same Wi-Fi (or the robot), an unauthenticated POST to the device port should be refused with `401`, not time out:

```powershell
curl.exe -s -o NUL -w "%{http_code}" -X POST http://<laptop-ip>:8001/api/device/heartbeat
```

If it times out, allow it through Windows Firewall, for the private network only:

```powershell
New-NetFirewallRule -DisplayName "MedAiCare robot device API" -Direction Inbound -Protocol TCP -LocalPort 8001 -Profile Private -Action Allow
```

Make sure the Wi-Fi network profile is **Private**: Settings → Network → Wi-Fi → your network.

## 9. Point the robot at the new laptop

1. Turn the robot on and open **Reachy Mini Control**. Make sure the `medcare_reachy` app is running.
2. Open the app's settings page at `http://reachy-mini.local:8042`, or use the robot's IP.
3. Set **Server address** to `http://<new laptop Wi-Fi IP>:8001`. Use port **8001**, not 8080.
4. **Robot key:**
   - **Moving:** leave the key field empty; the current key stays valid, because you restored the database and kept `SECRET_KEY`.
   - **Fresh install**, or if the robot reports it is no longer authorized: in the web app go to **Settings → Reachy robot**, review the notice, then **Pair**. Copy the `rdv1.…` key, which is shown only once, into the robot's settings page.
5. Save. The robot app restarts within a few seconds.

Check: the web app's Reachy card shows **Online** within about 10 seconds, and `docker compose logs -f app` shows `POST /api/device/heartbeat ... 200 OK` every 10 seconds.

> **"Offline" troubleshooting:** a `404` on `/api/device/heartbeat` in the app log means the robot is using the wrong port (8080). A timeout means a wrong IP, wrong `DEVICE_BIND`, or the firewall (step 8). A `401` means a wrong key: pair again.

## 10. Make sure the robot runs app version 0.4.0

The heartbeat reports the robot app's version. Check it on the server:

```powershell
docker compose exec -T postgres psql -U medai -d medcareai2 -c "SELECT status_detail->>'bridge_version' FROM reachy_device WHERE revoked_at IS NULL;"
```

If it says `0.3.0`, the app was reinstalled from Hugging Face and lost the streaming and listening changes. Reinstall 0.4.0 from the working copy:

```powershell
cd C:\medcareai\MedAiCarePlus\reachy_app
python tools\deploy_to_robot.py --host reachy-mini.local
```

It asks for the robot's SSH password (user `pollen`), then:
1. Backs up the installed app.
2. Copies 0.4.0 over.
3. Records the "say when you're done" prompt.
4. Downloads the voice-activity model.
5. Sets 15 fps with vision on the server.

If the robot's speech models were never installed (a brand-new robot), copy them first. They live in `~/.medcare_reachy/models/{tts,stt}` on the robot:
- `tts/matcha-icefall-zh-baker/` and `tts/vocos-22khz-univ.onnx`, from the sherpa-onnx `tts-models` and `vocoder-models` GitHub releases.
- `stt/model.int8.onnx` and `stt/tokens.txt`, from Hugging Face `csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17`.

The deploy script fetches `stt/silero_vad.onnx` itself.

## 10b. LINE notifications

With `LINE_CHANNEL_ACCESS_TOKEN` and `LINE_CHANNEL_SECRET` in `.env` (`/health` shows `"line": true`), run:

```powershell
& "C:\medcareai\MedAiCarePlus\scripts\line-tunnel.ps1"
```

It opens a Cloudflare quick tunnel that reaches **only** the LINE webhook, points the LINE channel at it, and asks LINE to send a test event. It should end with `LINE webhook is set and reachable.`

The tunnel address changes whenever its container restarts, for example after a reboot, so run the script again then. Family members then scan the QR on the Family page, add Care Bot, and send the code shown there.

## 11. Final check

| Check | Where | Expected |
| --- | --- | --- |
| Server health | `http://localhost:8080/health` | all `true` except `ocr`, `line` |
| Robot connection | Web app → Settings → Reachy card | **Online** |
| Robot app | `http://reachy-mini.local:8042/api/status` | `"state":"running"`, `"missing_clips":0` |
| Camera rate during a reminder | Reachy card → *Camera rate* | about 15 fps (the server needs ≥12 to record by itself) |
| Backups | `docker compose ps` | `backup` running; `backups\nightly` fills after the first night |

To try a reminder, use **Reachy card → Send test alert**. It uses the first pending or missed dose, and with automatic recording on it can mark that dose taken, so use it when you mean to take that dose.
