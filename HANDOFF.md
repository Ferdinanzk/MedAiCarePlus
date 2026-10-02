# Handoff — MedAiCarePlus + Reachy Mini

Status as of **2 October 2026**. This covers what exists, how the pieces fit, what has been verified, and what is still open. No passwords, keys or tokens are written here; see [Secrets](#secrets) for where they live.

## 1. The system in one picture

```
                    Wi-Fi (same network)
  ┌───────────────────────────────┐            ┌──────────────────────────────────────────┐
  │ Reachy Mini Wireless          │  JPEG      │ Laptop (always on), Docker Compose       │
  │ app: medcare_reachy 0.4.0     │  frames    │ project "medcareai2"                     │
  │  • camera → JPEG, ~15 fps ────┼───────────►│ app  :8001 device API (robot only)       │
  │  • speaker: recorded prompts  │  :8001     │      :8080 web app (browsers)            │
  │  • microphone → SenseVoice    │◄───────────┼─ face/hand/pose ONNX + face recognition  │
  │    on the robot (opt-in)      │  results,  │   + emotion → intake decision → DB       │
  │  • head motion                │  tasks     │ postgres (medcareai2_pgdata volume)      │
  └───────────────────────────────┘            │ backup (nightly dump + face gallery)     │
                                               └───────────────┬──────────────────────────┘
                                                               │ LINE Messaging API (not configured yet)
                                                               ▼
                                                        family members' phones
```

- The **server decides** whether a dose is recorded. The robot is a camera, microphone, speaker and head; it never records a dose itself.
- **No pill identification exists anywhere.** "Taken" means: the right person was recognized, their hand went to their open mouth with a pinch grip, and then pulled away. Records say *observed, pill not verified*.

## 2. Where everything is

| What | Where |
| --- | --- |
| Server + web app source | `C:\medcareai\MedAiCarePlus` (GitHub `Ferdinanzk/MedAiCarePlus`, branch `reachy-integration`) |
| Robot app source | `reachy_app/` in this repository: the Hugging Face space `pearlyjam21/medcare_reachy` at `970c7a3` plus this round's changes (version 0.4.0). `reachy_app/tools/deploy_to_robot.py` copies it onto the robot. |
| Robot app as published | Hugging Face space `pearlyjam21/medcare_reachy`, which still has **0.3.0** |
| Robot app as installed | `/venvs/apps_venv/lib/python3.12/site-packages/medcare_reachy` on the robot (0.4.0, copied by the deploy script) |
| Robot app settings | `~/.medcare_reachy/settings.json` on the robot. Edit it through the settings page at `http://<robot>:8042`, not by hand. |
| Robot speech models | `~/.medcare_reachy/models/` on the robot. They survive app reinstalls. `tts/matcha-icefall-zh-baker/` + `tts/vocos-22khz-univ.onnx` (from the sherpa-onnx `tts-models` / `vocoder-models` releases); `stt/model.int8.onnx` + `stt/tokens.txt` (Hugging Face `csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17`); `stt/silero_vad.onnx` (sherpa-onnx `asr-models` release, fetched by the deploy script). |
| Robot voice prompts | `~/.medcare_reachy/clips/zh-TW/*.wav` (12 files), linked into the app's `bridge/clips/zh-TW` |
| Database | Docker volume `medcareai2_pgdata` |
| Enrolled faces | `models/face_recognition/face_gallery/` (gitignored; personal data) |
| Backups | `backups/nightly`, `backups/weekly` (the backup container must be running) |

Network on 2 Oct 2026 (a phone hotspot, so these addresses can change): laptop `192.168.49.32`, robot `192.168.49.81` (`reachy-mini.local`). The robot's SSH user is `pollen`.

## 3. What was done in this round

1. **"Robot shows Offline / no reminders" fixed.** The web app had moved to port 8080 so it wouldn't clash with the Reachy daemon on 8000, and the robot app's server address had been changed to 8080 too. The device API is on **8001**. The robot's `app_url` is now `http://192.168.49.32:8001`.
2. **Emotion model installed.** `model_fp32.onnx` was copied in and the image rebuilt; `/health` shows `emotion: true`.
3. **Voice prompts recorded on the robot** with Matcha-TTS (Mandarin `zh-baker` voice, so the text was fed in Simplified Chinese). Each prompt was checked by reading it back with SenseVoice. Before this, the robot had no audio files and could not speak reminders.
4. **Vision moved from the robot to the laptop.** On the robot's Raspberry Pi, the six landmark models plus emotion ran at only 2–3 fps. The server refuses to auto-record below 12 fps, so no dose could ever be recorded automatically. Now:
   - **Robot:** streams JPEGs to `POST /api/device/monitor/frame`.
   - **Server:** runs the same models (`app/services/landmarks/`, `landmark_service.py`). Measured at about 29 ms per frame, 15 fps, identity verified, not degraded.
   - **Robot CPU while idle:** fell from about 115% to about 17%.
5. **"I finished" listening** (SenseVoice + silero VAD on the robot):
   - **Consent:** gated by the `robot_microphone` consent. The server sends a `microphone` flag with each task and heartbeat, and the Reachy card in the web app has a switch for it.
   - **Muting:** the robot is muted while it speaks.
   - **Decision:** saying "finished" is a *claim*. If the camera resolves nothing within 8 s, the dose goes to the family as `patient_claim`.
   - **Walking away:** a patient out of sight for 90 s leaves the dose pending.
6. **Robot app restart fixes.** Saving settings used to take over 15 s and could show "stopped" while it was running. Both fixed.
7. **Medication features (2 Oct, afternoon)**, with a database backup taken first in `backups/pre-features-*.dump`:
   - **History and adherence:** history shows past doses only, paged; adherence and streak use date ranges; the weekly summary no longer counts future doses.
   - **Stock:** follows the real dose size, half tablets included. There's an "Add supply" refill record, and refill warnings use days of supply.
   - **Stop/resume:** "Stop medicine" archives instead of deleting history.
   - **Today screen** on the dashboard, with take and skip on the exact dose.
   - **Schedules:** custom dose times and weekdays.
   - **Browser camera:** follows the robot's one-tablet auto-record rule.
8. **Check-in conversations with Reachy (demo).** After the medicines, or with "Talk to Reachy now", the robot chats:
   - The patient's speech becomes text on the robot.
   - The laptop answers with `openrouter/free`.
   - The robot speaks the reply with Matcha.
   - Conversations show on the dashboard and at `/conversations`, with summary, mood and a safety flag.
   
   It needs the "Daily check-ins" switch, which grants four consents. Risk words get a help-line reply and alert all verified family contacts. Robot app files are in the working copy (`bridge/speech.py`, chat mode in `voice.py`, `CHECKIN` state in `session.py`) and still need the deploy script.
9. **Disk-full incident (2 Oct, 14:45).**
   - **Cause:** the C: drive reached 0.8 GB free. Docker's storage went read-only, and an image built at that moment had empty source files.
   - **Recovery:**
     - cleared the pip download cache (11 GB)
     - hard-restarted Docker Desktop
     - pruned the build cache
     - rebuilt the image with `--no-cache`
   - **To watch:** keep an eye on free space. Downloads (~37 GB) and the Conda package cache (~9 GB) are the big remaining items.
10. **OpenRouter key added** to `.env` (`OPENROUTER_API_KEY`, `LLM_MODEL=openrouter/free`), with a $0.001 spend cap. Nothing calls an LLM yet.

The decision table the robot and server now follow:

| Camera | Patient said "finished" | Result |
| --- | --- | --- |
| Clear hand-to-mouth, ≥12 fps, auto-record on, one solid tablet | either | **Recorded as taken**; family gets the batched "taken" message |
| Uncertain event | either | Family asked to confirm (`uncertain_detection` / `auto_record_off` / `degraded`); evidence says whether the patient said they finished |
| Nothing | yes | Family asked to check the pill box (`patient_claim`) |
| Nothing | no, or patient left | Dose left pending; the missed-dose job alerts the family later |

## 4. Verified vs not verified

**Verified:**
- **Server frame endpoint:** tested in a container at 15 fps with an enrolled face photo.
- **Tests:** 305 backend tests pass, and 153 robot app tests pass on the laptop.
- **On the robot:**
  - 0.4.0 runs and heartbeats every 10 s.
  - All 12 clips are present.
  - The speech models load without errors.
  - Restarts are clean.

**Not verified yet (needs a real reminder with a person in front of the robot):**
- That a real reminder sustains about 15 fps over Wi-Fi with the patient in view.
- That a real hand-to-mouth movement is detected. The test photo had no shoulders or hands, so the "whose hand is it" check never ran.
- That SenseVoice hears 「我吃完了」 through the robot's real microphone. The `robot_microphone` consent is still **off**; the patient turns it on.
- The robot app's own test suite run *on the robot*: pytest isn't installed in its apps environment.

## 5. Open items

1. **Watch the first real reminder.** Check the reachy card's camera rate and `docker compose logs -f app` for `monitor/frame` 200s.
2. **Publish the robot app.** Push `reachy_app/` to the Hugging Face space. This needs the owner's go-ahead because the space is public. Until then, **reinstalling or updating the app from Reachy Mini Control puts 0.3.0 back**. In that case, rerun `python tools\deploy_to_robot.py --host <robot-ip>` from the working copy.
3. **Commit and push the server changes** on `reachy-integration`. Many files are uncommitted, including earlier local edits. A new laptop that clones from GitHub won't have them until they are pushed.
4. **LINE.** Fill `LINE_CHANNEL_ACCESS_TOKEN` and `LINE_CHANNEL_SECRET` in `.env`, set the webhook URL in the LINE console, and have family members link and verify. Until then, every family message (taken, confirm, missed) has nowhere to go, and a dose sent for confirmation simply expires after about 2 h.
5. **Clinician review** of the 12 prompt texts in `bridge/clips/manifest.json` (all `pending_clinician_review`).
6. **Stable addresses.** Reserve both IPs in the router/hotspot, or use `reachy-mini.local`. A changed laptop IP means updating `DEVICE_BIND` and the robot's server address.
7. **Backups.** Make sure the `backup` container is running (`docker compose ps`) and set `BACKUP_PASSPHRASE`. The backup folders were empty on 2 Oct.
8. **Legal fill-ins.** `OPERATOR_NAME`, `OPERATOR_CONTACT`, `TUNNEL_PROVIDER`, and the `LLM_PROVIDER*`/`LLM_RETENTION` values are empty. They are required before `APP_ENV=prod`. With `openrouter/free` the provider varies per request, so pin one model if the notice must name the provider.

## 6. Day-to-day operation

- **Laptop:** must stay on and awake, with Docker Desktop started at sign-in. If it is off, the robot cannot see and stops within about 10 s.
- **Robot power:** turn it off with its power switch. "Sleep" in the dashboard keeps the medcare app running, and the app will wake the robot for the next reminder.
- **Is it working?**
  - `http://localhost:8080/health`: everything `true` except `ocr` and `line`.
  - Reachy card: *Online*.
  - Robot settings page `http://<robot>:8042`: `state: running`, `missing_clips: 0`.
- **Test reminder:** Reachy card → *Send test alert*. It uses the first pending or missed dose, and with auto-record on it can change that dose's record.

## Secrets

These are not written in any file in git:

| Secret | Where it lives |
| --- | --- |
| `SECRET_KEY`, `OPENROUTER_API_KEY`, LINE keys, `BACKUP_PASSPHRASE` | `.env` (gitignored). **Keep `SECRET_KEY` when moving laptops**: it signs the robot's pairing key. |
| Robot pairing key (`rdv1.…`) | Shown once at pairing; stored only in the robot's `settings.json`. The server keeps only its SHA-256. |
| Robot SSH password | Ask the owner. |
