# Handoff — MedAiCarePlus + Reachy Mini

Status as of **18 October 2026, evening**, GitHub branch **`tool-fixed-version-04`** (section 12 is new in it: Wi-Fi,
the LINE test with a family confirmation, and the intake map; section 11, the live tests and the tool audit, came
with `tool-fixed-version-03`; section 10 with `tool-fixed-version-02`; section 9 with `tool-fixed-version-01`; section 8 with `reachy-integration-0.3`, sections 6 and 7 with
`reachy-integration-0.2`, and sections 1-5 describe `reachy-integration-0.1`). **Start with
[section 12](#12-wi-fi-the-line-test-with-a-family-confirmation-and-the-intake-map-18-october-2026-afternoon)
for what to do next.** No passwords, keys or tokens are written here; [step 4.3](#43-create-env-and-put-in-the-credentials) says
where each one goes.

The handoff of 2 October is kept at the end of this file ([History](#history-handoff-of-2-october-2026)). Some of it
is out of date; the sections above it win where they disagree.

## 1. What this branch is

`reachy-integration-0.1` = the team's `reachy-integration` branch (`a67a215`) plus two pieces of work:

1. **AI connections** (10-11 Oct, 18 commits). AI keys and models moved from `.env` into a card on the Settings page.
2. **Robot camera, finding the patient, AI card fixes and thinking levels** (11-12 Oct, 40 commits, robot app
   **0.7.0**).

In total: 66 files changed and about 21,900 lines added, of which about 14,100 are the design docs and plans under
`docs/superpowers/` and about 7,800 are code, tests and docs.

Why the second piece: on 11 Oct the robot "couldn't detect the patient's face" before a check-in. The face model was
not the problem. The robot's **camera was dead**: the TCU companion (another app on the robot) released the camera
at every boot, and MedCare Reachy's camera reader never reconnected. It searched blind for 10 minutes and logged
nothing. Even with a working camera, the robot could only verify a patient within about 1.35 m.

Design docs (in this branch):
- `docs/superpowers/specs/2026-10-10-ai-connections-design.md` and its plan in `docs/superpowers/plans/`.
- `docs/superpowers/specs/2026-10-11-robot-camera-identity-thinking-design.md` and its plan.

## 2. Before and after

| Area | Before (`reachy-integration`, robot app 0.5.4) | After (this branch, robot app 0.7.0) |
|---|---|---|
| **AI keys and models** | In `.env` only (OpenRouter key and two model names). Changing them meant editing `.env` and restarting. | **Settings → AI connections card.** Add OpenRouter, OpenAI or any OpenAI-compatible service. Keys are stored encrypted (derived from `SECRET_KEY`); only the last 4 characters are ever shown. |
| **Which model does which job** | Every job used `LLM_MODEL`, with `LLM_FALLBACK_MODEL` as backup. | Each job (Reachy's replies, the safety check, the after-chat summary and memory) has its own main and backup model. |
| **Testing a model** | Not possible. | **Test** sends a short real request and finds the settings that model accepts (token setting, temperature, reasoning off). |
| **Thinking (reasoning)** | Always off. | Each job can think at **Off / Low / Medium / High** (default Off: requests are exactly as before). Test finds which levels each model accepts and how long each takes. While thinking, no temperature is sent and the token budget grows (+800 / +1600 / +3000). Reasoning tokens are recorded. A model that can't do the chosen level gets the nearest lower one. |
| **Voice-only models (gpt-live-1)** | Test failed with OpenAI's confusing "Did you mean v1/completions?". | Test says "Voice-only model (like GPT-Live or a TTS model): it can't write Reachy's text replies." |
| **A job with no usable model** | No warning: the safety check could be silently off. | A warning under each such job on the card ("Reachy's model safety check is off."), and one in the server log at start. |
| **OpenRouter key added to `.env` later** | Ignored after the first start. | An OpenRouter connection without a key takes `OPENROUTER_API_KEY` from `.env` at every start. |
| **Company named in the robot notice** | Fixed text from `.env`. | Filled from the models chosen on the card, without asking patients again. |
| **Robot camera stops** | The robot searched blind for 10 minutes and logged nothing. | A **camera watchdog**: no frame for 2 s → one ERROR line and a recovery (at most once every 5 s); no steady frames for 15 s → the search ends as "camera_unavailable" (about 17 s after the last frame). The Reachy card shows **"Robot camera not working"** (「機器人鏡頭無法使用」). If the robot's daemon won't give the media back, the app asks again with the heartbeat (at most once a minute), so the speaker and microphone come back too. |
| **Pressing Save or Restart twice on the robot page** | Could start two copies of the bridge, which ended each other's sessions. | One copy only (a restart lock); the buttons are disabled while a request runs. |
| **Finding the patient before a check-in** | Needed the face match **and** a second face detector that loses faces smaller than about 50 px: about **1.35 m** at most. | A check-in's search accepts the face match alone (2 matches in a row, face at least 6.5% of the frame width: roughly **2.5 m**). Dose reminders keep the strict rule, and recording a dose never uses the looser one. |
| **Why a search failed** | No trace. | Each robot identity check is logged: status, rule, match distance, face size, face counts. No names, no images. (The server's INFO lines now reach `docker compose logs` at all: before, nothing set up logging and every INFO line was dropped.) |
| **Safety keywords** | Missed 「活著沒什麼意思」 and similar. | Caught: 活著(真/也/都/好…)沒(有)什麼意思/意義, 活得沒啥意思, 活著一點意思都沒有, with control phrases that stay unmatched (「活著真有意思」「我活著沒什麼問題」). |
| **Robot speech recognition** | SenseVoice on the robot (older than the repo). | Whisper Base, as in the repo. |
| **Tests** | Backend 1500, robot app 494, browser 48 passing. | Backend 1756, robot app 548, browser 70 passing. The same 3 old failures remain (BUG-37, BUG-38, and timing flakes BUG-39 / F19). |

## 3. What was checked on the real system (12 Oct)

On this PC's Docker stack and the Reachy Mini Wireless (`192.168.137.235`), after the whole-branch review and its fixes:

| Check | Result |
|---|---|
| Test suites | Backend 1756 passed, robot app 548 passed (6 skipped), browser 70 passed. Only the known old failures (BUG-37, BUG-38). |
| Security scan (semgrep, 25 changed files) | 5 findings, all in code older than this branch and not real problems (triaged in the build ledger). |
| Server start | Healthy in about 10 s; the `ai_job.thinking` column added, every job Off. |
| OpenRouter key from `.env` | Filled into the keyless connection at start. The key turned out to be **expired**; the user made a new one, which was put on the card (both OpenRouter models then passed Test, 1.2-1.4 s). |
| Test on the card | gpt-5.4-mini and gpt-6-luna think at low / medium / high (1.1-1.4 s each); Gemini 2.5 on OpenRouter: "Can't think". |
| A real reply at Low thinking | gpt-5.4-mini, 1.83 s, 60 reasoning tokens: 「真好，公園散步很舒服。看到什麼呢？」 |
| Robot app 0.7.0 deployed (SSH) | Running; the server sees `bridge_version` 0.7.0 and the camera state. The TCU companion is off at boot; the camera is available. |
| Check-in 1 | Camera about 10 frames/s; the patient found about 30 s after start; the greeting played; then no speech was recognised and the chat ended on "silence". |
| Check-in 2 | Found (`rule=openvino_only`, match distance 0.20, face 150 px); a 5-turn chat that ended on 「再見」. |
| Camera-failure test (release the camera during a search) | **Skipped** at the user's request; covered only by the robot app's tests. |

**Where the time goes in a check-in turn** (check-in 2): the robot's speech-to-text (Whisper Base) 8.7-11.1 s, the
server's reply 0.7-1.4 s, Reachy's first sound 1.7-3.0 s. Measured on the robot with the same recordings, Whisper Base
takes about 9 s whatever the length (it pads every clip to 30 s), while SenseVoice-Small takes 0.4 s for 「嗯」, 1.0 s
for 「謝謝您」 and 2.4 s for a 3.9 s sentence, both word-perfect. **Switching the robot back to SenseVoice would cut
about 7-8 s from every turn** (its model is still on the robot). Not done in this branch.

## 4. Set it up, step by step

Commands are for PowerShell on Windows, run in the project folder unless stated otherwise.

### 4.1 Install the tools

1. **Docker Desktop.** Start it, and turn on *Start Docker Desktop when you sign in*.
2. **Git for Windows** and **Git LFS** (`git lfs install` once; the face models are stored in LFS).
3. **Python 3.10 or newer**, only for deploying the robot app: `pip install paramiko`.

### 4.2 Get the code

```powershell
git clone --branch reachy-integration-0.1 https://github.com/Ferdinanzk/MedAiCarePlus.git
cd MedAiCarePlus
git lfs pull
```

### 4.3 Create `.env` and put in the credentials

```powershell
Copy-Item .env.example .env
notepad .env
```

`.env` is gitignored: **never commit it** (the repository is public). Fill in these lines:

| Line in `.env` | What it is for | Where to get it | Needed? |
|---|---|---|---|
| `SECRET_KEY` | Signs logins and the robot's pairing key, and **encrypts the AI keys stored on the card**. | Make one: `python -c "import secrets; print(secrets.token_hex(32))"` | **Yes.** Keep it forever: a new value logs everyone out, unpairs the robot and makes the stored AI keys unreadable (re-enter them on the card). |
| `OPENROUTER_API_KEY` | OpenRouter models (the default safety and summary models). Copied into the AI connections card on the first start; any OpenRouter connection on the card that has no key takes it at every start. | openrouter.ai → Keys | Recommended. If it expires, **Test** on an OpenRouter model shows `HTTP 401: API key expired`: put a new key on the card (*Replace key*) or here. |
| `LLM_MODEL`, `LLM_FALLBACK_MODEL` | The OpenRouter models copied in on the first start. | Model ids from openrouter.ai | First start only. `google/gemini-2.5-flash-lite` is retired by OpenRouter on **20 Oct 2026**: pick a replacement. |
| `LLM_SERVICE`, `LLM_PROVIDER`, `LLM_PROVIDER_REGION`, `LLM_RETENTION` | The company, region and retention named in the patient notice. | Your AI provider's terms | Required with `APP_ENV=prod`; afterwards edit them on the card (*Notice details*). |
| `GEMINI_API_KEY` | Prescription scanning (the Scan page). | aistudio.google.com/apikey | For OCR. |
| `LINE_CHANNEL_ACCESS_TOKEN`, `LINE_CHANNEL_SECRET` | Messages to family on LINE. | LINE Developers Console → your channel | For LINE. |
| `DEVICE_BIND` | The address the robot uses to reach this PC (port 8001). | `ipconfig`: this PC's address on the robot's network, e.g. `192.168.137.1` when the robot is on the PC's Windows hotspot. Never `0.0.0.0`. | **Yes** for the robot. |
| `WEB_PORT`, `MEDCARE_FRONTEND_URL` | The web app's port (8080 leaves 8000 to the robot daemon). | Keep `8080` and `http://localhost:8080`. | Yes. |
| `OPERATOR_NAME`, `OPERATOR_CONTACT`, `TUNNEL_PROVIDER` | Named in the patient notices. | You | Required with `APP_ENV=prod`. |
| `RISK_CLASSIFIER_API_KEY` | Required by the production checks when the robot feature is on. | Your provider | With `APP_ENV=prod` and `REACHY_FEATURE_ENABLED=1`. |
| `BACKUP_PASSPHRASE` | Encrypts the weekly backups. | Make a strong passphrase; store it somewhere else too. | Recommended. |

The **OpenAI key does not go in `.env`.** Paste it on the AI connections card (step 4.6).

A filled `.env` looks like this (made-up values, never real ones in any file you share):

```dotenv
SECRET_KEY=3f9c...64 hex characters...a1
OPENROUTER_API_KEY=sk-or-v1-...
GEMINI_API_KEY=AIza...
DEVICE_BIND=192.168.137.1
WEB_PORT=8080
MEDCARE_FRONTEND_URL=http://localhost:8080
```

### 4.4 Start the server

```powershell
docker compose up -d --build
docker compose ps
Invoke-RestMethod http://localhost:8080/health
```

Expected: `face_recognition`, `emotion`, `intake_detection` and `landmarks` are `true`; `ocr` is `true` with
`GEMINI_API_KEY`; `line` is `true` with the LINE keys.

Let the robot reach port 8001 (once, in an **administrator** PowerShell; this allows only the hotspot's devices):

```powershell
New-NetFirewallRule -DisplayName "MedAiCare robot device API" -Direction Inbound -Protocol TCP -LocalPort 8001 -RemoteAddress 192.168.137.0/24 -Action Allow
```

### 4.5 Sign in

Open http://localhost:8080, register (or sign in) and enroll the patient's face.

### 4.6 Set up the AI on the AI connections card

Settings → **AI connections**:
1. **Add connection** → OpenAI → paste the OpenAI key. (OpenRouter is already there from `.env`.)
2. **Add model** → e.g. `gpt-5.4-mini` and `gpt-6-luna` on OpenAI.
3. Press **Test** on every model you will use. It shows the time and "Can think: low · medium · high" or "Can't think".
   Models tested before this branch say "Not checked yet: press Test".
4. Under **Jobs**, pick a main and a backup model for each job, and a **Thinking** level (start with Off; Low is
   measured at about 1.8 s for a reply). Press **Save jobs**.
5. No job may show "No usable model". A model with an expired key shows its error under the model.

### 4.7 Prepare the robot

1. Turn the Reachy Mini on and join it to the same network (on our setup: the PC's Windows hotspot).
2. **Turn off the TCU companion** on the robot: it releases the camera at boot and uses the same port (8042).
   ```bash
   ssh pollen@reachy-mini.local
   sudo systemctl disable --now reachy-ai-companion
   curl -s localhost:8000/api/media/status        # expect "available":true; if "released":true:
   curl -s -X POST localhost:8000/api/media/acquire
   ```
3. Install robot app **0.7.0** from this repository (in Git Bash; the tool asks for the robot's SSH password, or
   reads `REACHY_SSH_PASSWORD`):
   ```bash
   cd reachy_app
   python tools/deploy_to_robot.py --host <robot-ip>
   ```
   Then start **medcare_reachy** in Reachy Mini Control (or `curl -s -X POST localhost:8000/api/apps/start-app/medcare_reachy`
   on the robot).

### 4.8 Pair the robot

1. Web app → Settings → **Reachy robot** → read the notice → **Pair**. Copy the robot key (`rdv1.…`, shown once).
2. Open the robot's page `http://<robot-ip>:8042`. Server address: `http://<DEVICE_BIND address>:8001` (port **8001**,
   not 8080). Paste the key. **Save and connect**.
3. Within about 10 s the Reachy card shows **Online**.

### 4.9 LINE (optional)

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\line-tunnel.ps1
```

It ends with `LINE webhook is set and reachable.` Run it again after every PC or Docker restart (the tunnel address
changes).

### 4.10 Check it works

- Reachy card → **Talk to Reachy now**: the robot wakes, looks for the patient, greets them and chats.
- Reachy card → **Send test alert**: a dose reminder for the first pending dose.
- `docker compose logs -f app`: `identity of session …` lines show how the robot's face checks go.

### 4.11 Run the tests

```powershell
.venv\Scripts\python -m pytest tests -q -p no:cacheprovider                     # backend (a Python 3.11 venv with requirements.txt)
cd reachy_app; ..\.venv\Scripts\python -m pytest tests medcare_reachy/bridge/tests -q -p no:cacheprovider
cd ..\frontend_source; npx tsc -b; npm run build
```

Known failures that are not caused by this branch: `test_a_broken_background_never_costs_the_reply` (BUG-37),
`scan.spec.ts:148` (BUG-38), and timing tests that fail now and then (BUG-39, `test_speech` first-sound).

## 5. Still open

1. **Make check-ins faster: switch the robot's speech-to-text back to SenseVoice** (see section 3; about 7-8 s per turn).
2. **Keys on the card don't follow `.env`.** A key already on the card is not replaced when `.env` changes (only a
   keyless OpenRouter connection takes the `.env` key), and the card's "usable" check only sees that a key exists, not
   that it still works. After changing a key, use *Replace key* on the card and press **Test**.
3. **`google/gemini-2.5-flash-lite` retires on 20 Oct 2026** (OpenRouter). Replace it on the card.
4. **Robot doses always go to family to confirm.** The robot daemon gives apps the camera at 10 fps; the server needs
   12 to record a dose by itself. `deploy_to_robot.py --camera-ipc-fps 15` raises it (a daemon update undoes it).
5. **The robot still has the factory SSH password.** Change it (`passwd` on the robot).
6. **The TCU companion can't run alongside MedCare Reachy** as it is (it releases the camera).
7. **The Hugging Face space** `pearlyjam21/medcare_reachy` has 0.7.0 since 12 Oct. Its history also holds an ElevenLabs
   voice line (numbered 0.6.0, 4-5 Oct) that is on no GitHub branch; it is kept there for a later version.
8. **Server errors seen live, in code older than this branch:** a database deadlock when the robot leases a task
   (`reachy_tasks._lease_once`; the robot's retry worked), and a 500 instead of a 409 for a camera frame that arrives
   just after its session ended (`intake_detection.process_frame` raises `SessionEnded`).
9. The check-ins switch records consent to the robot notice without showing it (only pairing shows it).
10. **Phase 2, live voice with GPT-Live.** Phase 2a, live check-ins for testing, is on branch `live-voice-2a` (section
   6). Phase 2b comes before any real patient uses it: the patient notice and opt-in, checking the face while GPT-Live
   is talking (robot app 0.9.0 already keeps the camera on during the chat and starts GPT-Live only after a recent
   sighting), reconnecting, a daily budget, and a second transcript for the safety check.
11. **Release gate for robot app 0.9.0.** Before 0.9.0 reaches patients beyond the user's own test, a new robot notice
   version must say that the camera stays on during a check-in (analysed in memory, never saved) and that the chat
   lasts until goodbye or until Reachy rests. The user decides the notice text; the Reachy card's check-in help
   already says so. (Robot alerts also don't repeat yet, although the notice §6 promises they do.)

## 6. Phase 2a: live voice check-ins (test only)

Branch `live-voice-2a` (from `reachy-integration-0.1`), robot app **0.8.0**, then **0.9.0** (the companion session,
below). Design: `docs/superpowers/specs/2026-10-12-live-voice-2a-design.md`. How it works: CLAUDE.md, section "Live
voice check-ins (Phase 2a, test)". The branch also carries robot app 0.7.1 (today's check-in chat no longer ends on
silence); the robot has run 0.7.1 since 12 Oct.

**What it is.** A check-in can be a live voice chat with OpenAI's GPT-Live (gpt-live-1) instead of the turn-by-turn
text chat, which takes 12-14 s a turn. The robot streams its microphone to the server; the server talks with GPT-Live
and sends its voice back to the robot. The OpenAI key stays on the server. The server holds the start of every reply
for 0.5 s so the safety check can stop it, has the robot say the help line in its own voice, and falls back to the
text chat whenever something fails. While the chat runs, the server checks every 10 s that the robot is still paired
and the consents and the live switch are still on. Reachy also greets the patient by name now, in both chat types
("What should Reachy call you?" on the Reachy card). GPT-Live costs about US$0.05 per minute of chat.

**Turn it on for testing:**

1. In `.env`, set `LIVE_VOICE_TEST=1`. Rebuild and start the app: `docker compose up -d --build app`.
2. Settings → **AI connections** → **Live voice (test)**: pick the OpenAI connection (step 4.6), keep the model
   `gpt-live-1` and the voice `marin` (or pick another), press **Save live voice**, then **Check**. Check must show a
   time; while it shows an error, live check-ins stay off.
3. Install the robot app (0.9.0 on this branch) as in step 4.7 (`python tools/deploy_to_robot.py --host <robot-ip>`),
   then restart it on the robot: `curl -s -X POST localhost:8000/api/apps/restart-current-app`. Heartbeats then report
   `bridge_version` 0.9.0.
4. Settings → **Reachy robot**: **Daily check-ins** on, **Live voice (test)** on. Optionally fill in **What should
   Reachy call you?** and save it.
5. Press **Talk to Reachy now**. Reachy looks for the patient, greets them by name in its own voice and listens.
   Answer while it can see you: GPT-Live then answers. Say 「再見」 to end. The chat is on the Conversations page like
   any check-in.

To turn it off: switch **Live voice (test)** off on the Reachy card (one robot), or set `LIVE_VOICE_TEST=0` and run
`docker compose up -d app` (everyone). Check-ins are then text chats again.

**Still to check on the robot** (the spec's robot tests, not done yet): the time to each reply, the name greeting,
that Reachy's own voice never comes back as a patient turn (echo), one goodbye, and one fallback (break the connection
briefly during a chat, e.g. the PC's internet: the check-in must go on as a text chat).

**Companion session (robot app 0.9.0).** Decided with the user on 12 Oct; design:
`docs/superpowers/specs/2026-10-12-companion-session-design.md`; how it works: CLAUDE.md, "How it ends" in the
check-in section and "Companion session" in the live voice section. A check-in now stays until the patient says
goodbye, with either chat. Reachy greets in its own voice and keeps the camera on (2 frames a second). GPT-Live starts
only when the patient speaks and the camera recognised them in the last 15 s, pauses after 30 s without patient speech
(billing stops) and starts again with the last 20 turns when the patient speaks. The patient's first words (the last
2 s kept while paused, and what they said while GPT-Live started) go to GPT-Live at up to twice real time
(`PRE_ROLL_SEND_SPEED`), because GPT-Live drops a faster burst. A talking stretch pauses at the next
gap in the talk after 10 minutes, and at 11 minutes whatever is happening. Reachy rests after 10 minutes with no voice
and nobody seen, and ends the chat for a medicine that falls due. This settles the old question about the live chat's
45 s silence ending: only robots older than 0.9.0 keep it. Every timing is one constant (for example
`QUIET_PAUSE_SECONDS` and `REST_AFTER_SECONDS`), to change after trying. Still to check on the robot with the user:
greet, talk, stay quiet (pause), speak again (restart), say 拜拜.

After the final review (same branch, 0.9.0): the medicine ending lets the patient finish first (`MEDICINE_WAIT_SECONDS`,
`MEDICINE_SPEECH_GAP_SECONDS`); a dose left pending before the chat is brought up when its reminder retry comes
(`retry_due`), and medicine tasks are leased before a queued check-in; the sweep keeps a quiet companion chat open
while its robot holds the running task (no 2-hour ceiling), and the live relay ends a chat whose conversation someone
else closed; endings drain GPT-Live's last words before storing them; a failed GPT-Live start asks the patient to say it
again; camera first needs a verified sighting; only a fresh camera answer keeps Reachy awake, and a live chat without a
working camera for `CAMERA_LOST_SECONDS` goes on as the text chat; the text chat ends when the microphone consent is
withdrawn; once the server has ended a live chat, the robot never cuts its help line. See section 5 item 11 for the
release gate.

**Same voice (17 Oct 2026).** Reachy's own lines around a live chat (the greeting, the opening with the name, the
reminder lead-in, the repeat line, the help line, the rest and medicine lines, the wind-down) are rendered once by the
server in the live voice and kept (`services/voice_lines.py`, `GET /api/device/clips/line/{line_id}`); the relay names
the copy to play beside the words (`clip` on `ready` and `say`). Design:
`docs/superpowers/specs/2026-10-17-same-voice-design.md`; how it works: CLAUDE.md, "Same voice" in the live voice
section. **Hearing them needs robot app 0.16.0** (medcare-reachy-bridge, its own plan): it keeps the copies and plays
them, and falls back to its Matcha voice for any line it has no copy of. Robot app 0.15.0 keeps working with this server
and ignores the new fields; nothing changes without `LIVE_VOICE_TEST=1` and a passing Check.

**Real patients need Phase 2b first.** 2a has no patient notice or consent for live voice, no switch for patients, no
daily cost limit, no reconnect, and no face check while GPT-Live is talking; its safety check reads only GPT-Live's own
transcript (no second transcript and no echo guard on the server). Keep `LIVE_VOICE_TEST=0` on any installation a
patient uses.

## 7. What was finished on 12 October 2026

`reachy-integration-0.2` is `reachy-integration-0.1` plus the work below (it was built on the local branch
`live-voice-2a`).

**Finished and tested**

1. **Phase 2a, live voice check-ins with GPT-Live (test only)**, as in section 6. Built in 11 tasks, each one reviewed
   before the next. The reviews added three more pieces of work:
   - While a live chat runs, the server checks every 10 s that the robot is still paired, the live switch is on and
     the consents are current. If one is gone, the chat ends at once, so the patient's voice stops going to OpenAI. If
     3 checks in a row cannot be read, the chat ends too.
   - GPT-Live often starts answering before the patient's last words arrive. The server still stores one patient turn
     per sentence and one Reachy turn per reply, so a goodbye split in two (「拜」+「拜」) is still seen.
   - A final review of the whole branch (5 reviewers, 2 more checking each finding) found 3 problems, all fixed: a
     database error during the help-line ending could skip the family alert (now the alert always goes); a database
     call that hangs could freeze the chat's safety checks (now every call gives up after 5 s); switching memory off
     during a live chat was not noticed (now the text chat takes over). Production mode (`APP_ENV=prod`) now refuses
     to start with `LIVE_VOICE_TEST=1`.
2. **Robot app 0.7.1: a check-in no longer ends on silence.** After trying Reachy, the user decided that a companion
   must not leave when the patient is quiet or steps away. Since 0.7.1 a check-in ends only on a goodbye, a risk, a
   stop, or after 5 minutes. It was deployed to the robot on 12 Oct (the robot keeps a backup of the old app in
   `~/.medcare_reachy/backup-20261012-111649.tgz`). 0.8.0 includes it.
3. **A live test on this PC with the real gpt-live-1** (no robot; the server's real relay with stand-ins for the
   database and the robot):
   - A normal sentence (「今天天氣有點冷，我不太想出門散步。」): GPT-Live greeted 「王奶奶，今天感覺怎麼樣？想跟我聊聊嗎？」
     3.8 s after the start, heard the sentence correctly, and answered 1.9 s after the patient finished (this includes
     the 0.5 s safety hold). The reply was 30 characters.
   - A risk sentence (「最近覺得活著沒什麼意思，我不想活了。」): the keyword check caught it while the patient was still
     speaking. None of GPT-Live's voice reached the robot; the robot said the help line in its own voice; family were
     alerted once.
   - Cost: 39 billed seconds in all.
4. **The companion session: built and reviewed on this branch (robot app 0.9.0).** Its live tests on the PC and on the
   robot are still to do (see "Not done yet"). Spec:
   `docs/superpowers/specs/2026-10-12-companion-session-design.md`. Reachy greets the patient in its own voice. GPT-Live
   starts only when the camera recognised the patient in the last 15 s and the patient speaks. It pauses after 30 s of
   quiet (billing stops) and restarts with the conversation so far. The session ends on 拜拜/再見, a risk, a
   withdrawal, a medicine falling due, or 10 minutes with no voice and nobody seen. Today's text chat follows the same
   rules.

**Tests at this commit:** backend 2020 passed plus 1 known failure (BUG-37, older than this work); robot app 624
passed, 6 skipped; web app 91 Playwright tests passed plus 1 known failure (BUG-38).

**Where everything is**

| What | Where |
|---|---|
| Code | GitHub `Ferdinanzk/MedAiCarePlus`, branch `reachy-integration-0.2` |
| Robot app 0.8.0 | Hugging Face space `pearlyjam21/medcare_reachy`, branch `reachy-integration-0.2`. The space's `main` (what the robot's app store installs) is still 0.7.0. |
| The robot | Runs 0.7.1 (deployed by SSH from this branch's parent `checkin-no-silence`, not from Hugging Face) |
| The server on this PC | The `reachy-integration-0.1` build, with live voice off |

**Not done yet**

- The robot tests of Phase 2a (section 6, "Still to check on the robot"). They need the server rebuilt from this branch
  with `LIVE_VOICE_TEST=1` and robot app 0.8.0 on the robot; the user will do them together with the developer.
- The companion session's live test on this PC and its robot test with the user (robot app 0.9.0; section 6).
- Small points the reviews found and left for Phase 2b: a database insert that times out can slow the chat's later
  writes; a late risk found while the chat is already falling back can end it without the spoken help line (family
  are still alerted); the check-ins help text on the Reachy card still says only text is sent, also while the live
  voice test switch is on.

## 8. What was finished on 12 October 2026, evening

`reachy-integration-0.3` is `reachy-integration-0.2` plus the companion session (robot app **0.9.0**), the fixes from
its final review and from its live tests, and this section. It was built on the local branch `live-voice-2a`.

**Finished and tested**

1. **The companion session (robot app 0.9.0)**, as in section 6: Reachy stays with the patient until goodbye, in both
   chat types. Built in 7 reviewed tasks. A final review of the whole work (4 reviewers, 2 more checking each finding)
   found 14 problems, all fixed. The most important ones:
   - a medicine or rest ending could drop the patient's last words before the safety check saw them;
   - a medicine ending could cut the server's help line;
   - after the camera stopped, its last answer kept Reachy awake for ever;
   - the text chat ignored a withdrawn microphone switch;
   - a dose left untaken was never brought up again during a long chat.
2. **A live test on this PC with the real gpt-live-1 found one more problem, now fixed.** After each pause, GPT-Live
   lost the patient's first 1-2 s, because the server sent the saved audio all at once and GPT-Live limits how fast
   audio may arrive (`input_audio_rate_limit_exceeded`; no OpenAI document states the limit). The server now sends it
   at up to twice real time (`PRE_ROLL_SEND_SPEED`; measured: every word kept). A security review then added a memory
   cap (`KEPT_MAX_SECONDS`) and a rule against a robot that sends audio too fast (`CATCH_UP_MARGIN_SECONDS`). The
   final PC test heard every word, and after the pause GPT-Live remembered what was said before
   (「因為你說天氣有點冷，不太想出門散步。」).
3. **Two robot tests with the user** (22:18 and 22:25, "Talk to Reachy now"). Both worked from start to end: Reachy
   found the patient, greeted him by name in its own voice, GPT-Live started 1.2-2.1 s after he spoke, replies began
   0.65-3 s after he finished, every patient turn went through the safety check (no risk), and 「拜拜」 / 「先這樣」 ended
   the chat cleanly. GPT-Live cost about US$0.18 for both. **Not reached on the robot:** the pause and restart (it
   needs 30 s of quiet; the longest gap was about 20 s), the rest ending and the medicine ending. The pause and restart
   passed the PC test.

**Tests at this commit:** backend 2068 passed plus 1 known failure (BUG-37); robot app 650 passed, 6 skipped, plus the
known flaky tests (`test_speech` first sound, a `test_gestures` clip timing); web app type check and build pass.

**Repeat the robot test on another computer** (section 6, "Turn it on for testing", in short): set
`LIVE_VOICE_TEST=1` in `.env` and run `docker compose up -d --build app`; on the AI connections card save the live
voice model (`gpt-live-1`, voice `marin`) on the OpenAI connection and press **Check**; on the Reachy card switch
**Live voice (test)** on; install robot app 0.9.0 with `python tools/deploy_to_robot.py --host <robot-ip>` and start it
(`curl -s -X POST http://<robot-ip>:8000/api/apps/start-app/medcare_reachy`); press **Talk to Reachy now**.

**Where everything is**

| What | Where |
|---|---|
| Code | GitHub `Ferdinanzk/MedAiCarePlus`, branch `reachy-integration-0.3` |
| Robot app 0.9.0 | Hugging Face space `pearlyjam21/medcare_reachy`, branch `reachy-integration-0.3`. The space's `main` (what the robot's app store installs) is still 0.7.0. |
| The robot | Runs 0.9.0 (deployed by SSH; the old app is backed up in `~/.medcare_reachy/backup-20261012-221715.tgz`) |
| The server on this PC | This branch, with `LIVE_VOICE_TEST=1` in `.env` and the robot's live voice switch on |

**Seen in the robot tests (not fixed yet)**

- **GPT-Live makes up facts it cannot know.** Asked about the weather it said 「今晚是晴天」 and 「現在22度，晴天」; it has
  no weather source.
- **GPT-Live cannot see.** Asked 「你會看得到我的臉嗎」 and 「你知道我穿什麼衣服嗎」 it said it could not; it only hears.
- One reply was stored split across two Reachy turns (text only: the patient's words, which the safety check reads,
  are not affected). GPT-Live writes the patient's words in Simplified characters at times (the keyword check reads
  both scripts), and its own transcript sometimes holds a broken character 「�」.
- The robot's camera reaches the app at about 10 frames a second; recording a dose needs 12 (see step 4.7,
  `--camera-ipc-fps 15`).
- **Release gate (section 5 item 11):** before 0.9.0 reaches patients beyond the user's own test, a new robot notice
  version must say that the camera stays on during a check-in and that the chat lasts until goodbye.

**Next (asked by the user on 12 Oct)**

1. **Face tracking.** Once Reachy finds the patient, it should keep its head turned to the patient's face (lock on)
   for the whole session. The server's camera answers already give the face's box (see the `box_px` in the identity
   log lines); during a companion session the robot sends 2 frames a second, so smooth following may need a face
   detector on the robot itself. Head moves must not fight Reachy's speaking gestures.
2. **Let GPT-Live see.** The patient asks what he is wearing or how he looks, and GPT-Live cannot answer because it
   gets only sound. A way to build it: GPT-Live's "delegation" (below) with a tool such as `look()` that the server
   answers from the latest camera frame. This sends a camera image (or a description of it) to OpenAI, which today's
   notice does not allow ("analysed in memory, never saved"): it needs a new notice and consent first, and a design.
3. **Tool calling for GPT-Live.** GPT-Live can hand questions that need facts or tools to a backend model, which can
   use OpenAI's web search or call functions our server runs ("Responses delegation"; the TCU companion's
   `companion/ai/openai_live.py` already does this). A weather tool (for example Taiwan's Central Weather
   Administration open data) or web search would stop the made-up weather; a tool could also read the patient's
   medicine schedule. Backend calls are billed on top of the $0.05 per minute. Until then, one line in GPT-Live's
   instructions ("never claim to know the weather or news you cannot check") is the cheap fix.
4. **Still to test on the robot:** the pause and restart (stay quiet 40 s, then ask about what was said), the rest
   ending (step away for 10 minutes), a medicine falling due during a chat, and a fallback (cut the PC's internet
   during a chat).

## 9. What was finished on 17 October 2026

`tool-fixed-version-01` is `feature/merged-live-dose-status` (`d2a4a8b`, the 16 Oct merge of the live tools and the
dose-status work made on another computer) plus the fixes for the GPT-Live agent tools the user's test sheet marked
broken: `stop_dance`, `record_medication_taken` and the LINE video that never came. 29 commits, 27 files, about
4,900 lines added (tests included). The plan is `docs/superpowers/plans/2026-10-16-agent-tool-fixes.md`; the rules
the fixes follow are in `CLAUDE.md` ("Dose videos", "GPT-Live agent", "stop_dance stops at once", the backstop
intents). The robot app was not changed: it is **0.15.0** from its own repository `Pearlyjam21/medcare-reachy-bridge`
(branch `feature/merged-live-emotion`, `437b136`, the same files as the Hugging Face space's `main`); this
repository's `reachy_app/` is an older copy (0.10.0).

**Why the video never came.** Two computers were testing with **one LINE bot**. A LINE bot has one webhook address,
and the server uses that address as the public link of its videos. The webhook pointed at the other computer's
tunnel, and the old link check accepted that tunnel's own 404 as "working", so every video link on this computer
led nowhere. The fix below makes the server prove the link reaches itself. Only one computer at a time can deliver
videos and family answers: whoever tests runs `scripts/line-tunnel.ps1` first (it starts this computer's tunnel and
points the bot's webhook at it).

**Finished and tested**

1. **Dose videos reach family or say why not.** The server checks its public link with a secret probe: a request
   through the LINE webhook address must answer with this server's own marker. One WARNING names the reason when
   the link is lost (no LINE token, webhook endpoint error, tunnel not answering, tunnel reaches another server,
   `PUBLIC_BASE_URL` not https) and the `[STARTUP] line` stage shows it. A clip with no link is **kept, not dropped**,
   and the `dose_videos` job sends it as soon as a link answers again, while its family request is still open and for
   up to 4 hours (log: "sent late, now that a public link answers"). The family request says what is true about its
   video: it comes with this message, it was sent earlier (only when that clip really went), or none and why (no
   consent, no recent camera frames).
2. **`record_medication_taken` never loses a legitimate request.** Closing GPT-Live (a goodbye or the companion's
   quiet pause) lets a running record, family note or alert finish instead of cancelling it, and the pause waits for
   the medication tools and for GPT-Live's answer after them. The order is now record first, applause after. A record
   given without its camera session settles an open camera watch only for the exact same dose, and the watch's
   camera reading (result and confidence, or "still watching") goes into the family request's evidence, so family
   see when the camera disagrees with "I took it". The tool's time limit covers its worst case (20 s).
3. **「我吃完藥了」 reaches the tool.** With tools on, GPT-Live's prompt delegates a medicine claim and praises only
   after the server's answer (it also records every due dose of the slot and sends a claim on a too-soon or
   max-reached dose, which the server refuses and reports to family with the double-dose alert). If GPT-Live does not
   delegate within the usual grace, the relay's backstop does: it reads only a closed list of plain claims
   (「我吃完藥了」, 「我吃過藥了」, 「我已經吃藥了」, "I took my medicine", ... optionally followed by one closing word), decided
   on the whole turn at dispatch, dropped by any other words; it runs the due check, then the record for each due
   dose, holds the chat open until they finish, and has GPT-Live say what happened without praise. Anything less
   plain (a time, a person, a condition, a doubt, a retraction) is left to GPT-Live's own call.
4. **`stop_dance` stops at once.** While a dance may run, a stop phrase anywhere in the patient's words (「停」,
   「不要再跳了」, 「別跳」, 「好了」, 「你要不要停下來」, "stop", "don't dance", ...) makes the relay run `stop_dance` at the
   first transcript fragment that holds it, without waiting for GPT-Live; GPT-Live's own later call shares that run.
   A stop that lands while the robot is still loading the move is sent again when the move starts. After a stop,
   a dance is refused for 10 s unless the patient asks again (「繼續跳」, 「再跳一次」, "keep dancing"). A dance with no
   `repeat` plays 2 moves, so there is time to stop it. 「不要停」, 「停車」, 「停電」, 「要不要再跳舞」 are not stops.
5. **Tests:** backend 2415 → **3147 passed** (`.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider`).
   Two timing tests fail now and then under load and pass alone (`test_after_a_429_the_late_check_waits_before_asking_again`,
   `test_dose_emotion.py::test_the_event_window_starts_when_the_detector_skips_approaching`).
6. **Live test on the robot with the user (17 Oct 12:44-12:56)**, server built from `26bfae2`, robot app 0.15.0,
   a test medicine made due with `PATCH /api/medications/{id}`, a check-in started with "Talk to Reachy now"'s task:
   「我吃完藥了」 → `check_medication_due` (16 ms) → `record_medication_taken` (30 ms) → applause; the family request
   arrived on LINE. 「不要再跳了」 → "the patient asks to stop dancing; the relay runs stop_dance now" (0 ms; GPT-Live's
   own call shared it); 「繼續跳」 danced again; 再見 ended the chat. The video was **kept unsent** because the
   Cloudflare quick tunnel had died minutes earlier ("tunnel not answering"); after `docker compose --profile line up
   -d --force-recreate line-tunnel` and `scripts/line-tunnel.ps1`, the job sent it ("sent late") and the user received
   it on LINE. So every path of the fixes, including the late send, ran on the real system.

**Set up for the test on this computer** (on top of section 8): `.env` has `LIVE_TOOLS=1` (the agent layer) and
`LIVE_VOICE_TEST=1`; `DEVICE_BIND` is the address the robot reaches (`192.168.137.1`, this PC's hotspot);
`scripts/line-tunnel.ps1` was run here (so the shared bot's webhook points at this PC); the robot runs 0.15.0
installed with `tools/deploy_to_robot.py` from the `medcare-reachy-bridge` repository (backup
`~/.medcare_reachy/backup-20261016-231451.tgz` on the robot). The safety and after-chat model is still
`google/gemini-2.5-flash-lite`, which OpenRouter retires on 20 Oct: pick another on the AI connections card.

**Seen in the live test (not fixed)**

- **A false "Reachy is offline" message to family.** After leasing the check-in task the robot opened its camera,
  closed it 1.4 s later, and sent no heartbeat for 77 s; the server (`reachy_tasks.maintenance`, 60 s) re-queued the
  task and sent the notice, then the robot leased the task again and the chat started. Two fixes are due: the
  robot's silence after a failed camera start, and a "could not help with the medicines" notice for a chat task.
- The family request promised "the video comes in the next message" although the link was already down: the
  request's video outlook is computed before the clip is stored and the link checked. A known-down link should get
  its own wording ("the video follows when the link is back").
- The Cloudflare **quick** tunnel dies on its own (five addresses since 16 Oct); each death needs
  `scripts/line-tunnel.ps1` again. A named tunnel would be stable.
- **"Use webhook" is off** in the LINE Developers console for the test bot, so family answers never reach the server.
- From the final reviews, minor and parked: no test pins that a held GPT-Live call's time counts from when it starts
  running; a record answered `already_*` still spends an unread camera watch, so that watch's clip never reaches
  family.

**Next**

1. **Same voice** (approved 17 Oct): Reachy's own lines around a live chat in GPT-Live's voice, rendered by the
   server, with Matcha as the fallback; branch `same-voice` (made from this branch) and robot app 0.16.0. Design:
   `docs/superpowers/specs/2026-10-17-same-voice-design.md` on that branch.
2. **New agent tools** (approved 17 Oct): `go_to_sleep`, `volume_control`, `head_tracking`, `robot_status`, `forget`,
   `move_head`, `sweep_look`, `stop_emotion` (`idle_do_nothing` skipped); after the same voice, robot app 0.17.0.
3. The two defects above, and the remaining robot checks of section 8 (the pause and restart, the rest ending, a
   fallback).

## 10. Same voice, built and reviewed, and the new tools design (17 October 2026, night)

`tool-fixed-version-02` is `tool-fixed-version-01` (section 9, the agent tool fixes, tested on the robot) plus the
whole **same voice** work (all 12 tasks of the server plan and all 9 of the robot plan, then a whole-branch review of
both repos and its fixes), plus the **design of the new agent tools**. The same voice is not yet heard on the robot:
the listen test with the user (step 2 below) is what is left of it.

**Same voice: what it is.** In a live chat GPT-Live answers in OpenAI's "marin" voice, but Reachy's own lines (the
check-in greeting, the opening with the patient's name, the reminder lead-in, the "say it again" line, the help line,
the rest and medicine lines, the wind-down) came from the robot's Matcha voice, and the user heard the jump. Now the
server renders each line once in the live voice (`gpt-4o-mini-tts`, the speech API the medicine prompts already use)
and the robot (app **0.16.0**) keeps a copy and plays it, with Matcha as the fallback. Spec:
`docs/superpowers/specs/2026-10-17-same-voice-design.md` (with two code maps beside it). Plans:
`docs/superpowers/plans/2026-10-17-same-voice-server.md` (12 tasks) and `...-same-voice-robot.md` (9 tasks). Every
task was done test first and reviewed by a second agent (spec and quality); the per-task minor notes are in
`docs/superpowers/plans/2026-10-17-same-voice-review-notes.md`.

**The whole-branch review** (workflow `wf_2fad42b9-909`): four reviewers read both repos against the spec (spec,
the server-robot contract, patient safety, test quality); two skeptics tried to refute each of their 24 findings;
14 held (2 important, 12 minor) and 10 were dropped. The important one, found by two reviewers: a disk or permission
fault on the robot's lines folder made `LineCache.kept()` raise, which could end a task without its greeting or turn
a live chat with a help line into a text chat. Now it is logged once and the line is said by Matcha. The minor ones:
`fetch` and its refresh could raise or strand lines on a failed delete; the server's render ahead stopped at a line
it could not store or read; a pause of 0.6 s inside a kept line made the robot report `idle` mid-line, and a reminder
sent on that idle was lost (fixed on both sides: the robot reports `idle` only once its own line is over, and the
server cuts every pause to 0.4 s); README and protocol wording; and five missing tests. All were fixed test first,
re-reviewed, and the re-review's three small leftovers were fixed too. Two robot timing tests that failed on the test
PC (Python 3.11 on Windows reads `time.monotonic()` in 15.6 ms steps) now allow one clock tick.

**Where each part stands**

| Part | State | Where |
|---|---|---|
| Server | Done and reviewed: `med_clips.render` takes instructions; the 304 on `/clips/med`; `services/voice_lines.py` (texts, spoken form, key, 16 kHz WAV with pauses cut to 0.4 s, storage under `MED_CLIP_DIR`, per-patient lines behind check-in consent); `GET /api/device/clips/line/{line_id}` (200 / 304 / 404); the `clip` field on `ready` and `say`; render ahead at startup (`[STARTUP] voice_lines`), every 30 min, on a queued task and on a call-name change; `CLAUDE.md` describes it | this branch; backend **3178 passed** at `2e9a378` |
| Robot app 0.16.0 | Done and reviewed: `AppClient.line_clip`; `bridge/lines.py` `LineCache`; the live chat plays the kept copy when the server's key matches, else Matcha; the rest, medicine and wind-down lines and the check-in greeting from their copies; no wind-down after a live goodbye; the cache wired into the runner and the service (`LINES_DIR` = `~/.medcare_reachy/lines`); the README's "Same voice (0.16.0, test)" | robot repo `medcare-reachy-bridge`, branch `tool-fixed-version-02` (`c56bbe2`, 14 commits on top of `feature/merged-live-emotion` `437b136`); **799 passed, 6 skipped** (two runs). GitHub refuses pushes from the test PC (its account has no write access to `Pearlyjam21/medcare-reachy-bridge`), so the same files are on the **Hugging Face space** `pearlyjam21/medcare_reachy`, branch `tool-fixed-version-02` (on top of the space's `main`, which stays 0.15.0 so installs from the app store are unchanged). The space's branch `tool-fixed-version-01` is `main` itself. |
| New tools | The design: `docs/superpowers/specs/2026-10-17-new-tools-design.md` (eight tools: `go_to_sleep`, `volume_control`, `head_tracking`, `robot_status`, `forget`, `move_head`, `sweep_look`, `stop_emotion`; robot app 0.17.0), with the brief and the raw research beside it, and the builder check's rulings at its end. | The **implementation plan**, then the build | this branch |

**How to continue, in order**

1. **Get the code.** Server: `git fetch origin` and `git switch tool-fixed-version-02`; its tests run with
   `python -m pytest tests -q -p no:cacheprovider` (on the test PC with `C:\Reachy_Mini\repo\MedAiCarePlus\.venv`).
   Robot: on the test PC, `C:\Reachy_Mini\repo\medcare-reachy-bridge`, branch `tool-fixed-version-02`; elsewhere,
   `git clone -b tool-fixed-version-02 https://huggingface.co/spaces/pearlyjam21/medcare_reachy` (or push the local
   branch to GitHub from a computer whose account may write there). Its tests: `python -m pytest tests
   medcare_reachy/bridge/tests -q -p no:cacheprovider`.
2. **Deploy and listen with the user** (the spec's "Before release"). Rebuild the server (`docker compose up -d
   --build app`; the startup log's `[STARTUP] voice_lines` stage says when the fixed lines are rendered), install the
   robot app (`python tools/deploy_to_robot.py --host <robot-ip>` from the robot repo, then restart the app through
   the daemon; heartbeats then report `bridge_version` 0.16.0), and run one live check-in and one reminder check-in:
   the greeting, the opening with the name, the reminder lead-in and the reminder after it, the help line's numbers
   (119, 1925, digit by digit), the rest or medicine line and the wind-down should all sound like GPT-Live, with no
   cut word where a pause was shortened. Only then push the robot app to the space's `main`, if the user wants it in
   the app store.
3. **New tools:** write the implementation plan from the design (against the finished same-voice code), then build
   it the same way (each task test first and reviewed), on a new branch (`tool-fixed-version-03` is the next name).

**Known and accepted in the same-voice design:** after the "say it again" line the server always falls back to the
text chat, whose replies are still Matcha (v1 covers only the lines around a live chat); after the user changes the
live voice, a fixed line can play once in the old voice before its copy is refreshed; the per-patient copies (the
opening and the reminder lead-in, which say the patient's name) stay under `MED_CLIP_DIR` after the account is
deleted, as the medicine prompts do. Marin copies exist only while `LIVE_VOICE_TEST=1` and the live model's Check
passed, as for the medicine prompts.

**Still open from section 9:** the false "Reachy is offline" notice, the request's video wording when the link is
already down, the Cloudflare quick tunnel dying, LINE "Use webhook" off, and the safety model retiring on 20 Oct. The
test medicine 「測試藥」 is still active on the test patient (archive it on the Medications page when testing is done).

## 11. Live tests, the tool audit and what to do first (17 October night to 18 October 2026)

`tool-fixed-version-03` is `tool-fixed-version-02` (section 10: same voice, built, reviewed and deployed) plus the
three reports of this round and this section. **No code changed:** the server is still `8826c92`'s code and the
robot app is still 0.16.0 (`c56bbe2`, the robot repo's branch `tool-fixed-version-03` is that same commit; on the
Hugging Face space, `tool-fixed-version-03` is the same files as `tool-fixed-version-02`, and `main` stays 0.15.0).

**What runs on the test PC.** The server image of `8826c92`, and robot app 0.16.0 on the robot (installed on 17 Oct
with `tools/deploy_to_robot.py`; heartbeats report `bridge_version` 0.16.0). The test PC's `.env` (never in git)
differs from the defaults in three switches: `LIVE_VOICE_TEST=1` and `LIVE_TOOLS=1` (as before),
`LIVE_VISION_CLOUD=1` (17 Oct 21:57, on the user's ask: the vision tools send a camera frame to the OCR's Gemini
model; test only, no patient notice covers it). And `LINE_CHANNEL_ACCESS_TOKEN` was **emptied**, back to its empty
default (18 Oct, about 01:40: LINE pushes are off to save the free plan's 200 messages a month; the channel secret
is kept).

**The live tests with the user** (17 Oct 21:49-22:24, 18 Oct 00:55-01:04 and 12:40-12:52):

- **Same voice:** the check-in greeting, the opening with the name, the reminder lead-in and the reminder played from
  the marin copies; the robot logged no failed copy. The user's verdict on the sound was not given yet.
- **Camera vision:** `describe_view` answered `vision_off` (in 66 ms) until `LIVE_VISION_CLOUD` was set; then two
  looks of 4.6-5.0 s answered correctly (what was in view, the colour of a top).
- **Reachy stood silent after a reminder:** [docs/REACHY_SILENT_NOT_SEEN_2026-10-17.md](docs/REACHY_SILENT_NOT_SEEN_2026-10-17.md).
  GPT-Live starts only when the camera recognised the patient in the last 15 s (`live_session.patient_seen_recently`),
  and the robot only logs `not_seen` (`live.py:517-519`), so after the 22:02 reminder the patient's words were never
  heard and the medicine claim was lost; it recovered at 22:22, when the patient leaned in, and the claim was
  recorded. Why the camera lost the match is not settled: the head levelled after the search (`hold_head`), a weak
  face gallery (3 small crops from the web sign-up), or the patient moving.
- **The 2-minute wait after a medicine chat:** the robot stays awake, silent, head level, for
  `POST_SLOT_SECONDS = 120` (robot `session.py:80`, used at `:917-924`), looking for an extra dose; the user
  thought it was stuck.
- **18 Oct 12:43:** the daemon logged `Motor 'left_antenna' hardware errors: ['Overload Error']` every second for
  about a minute; the app was stopped at 12:43:19 (most likely from the Reachy Mini Control app; not proven) and
  came back at 12:51; what started it is not proven (this PC has no reconcile task installed). No error since. If
  it comes back, check that the left antenna moves freely.

**LINE.** On 17 Oct the account in use (Ai_Deep2) and Care Bot had sent 200 of 200 messages that month, so every
push failed with `429 You have reached your monthly limit`, and the tools still answered as if family were told
(`family_asked` true, `queued`, `alerted`). With the user's OK
the server was switched to the Ai_Deep account from the key folder (the test contacts had added it), the stuck
messages went out, and then, on the user's ask, pushes were turned off. Ai_Deep's webhook points at an outside
service, so family answer buttons do not reach this server and new dose videos have no public link. Before a LINE
test: clear the outbox backlog first (rows `queued` or `failed`), then put a token back, and empty it again after.

**The tool audit** ([docs/TOOL_STATUS_2026-10-18.md](docs/TOOL_STATUS_2026-10-18.md)): every tool checked in the
code, the tests and the live logs, not in the docs. Of the 25 tools in `TOOLS_SCHEMA`, 7 work on the robot, 12 work in
tests only, 6 are partial and none is broken; the 8 approved new tools have no code; `idle_do_nothing` is skipped.
New defects: `set_reminder` refuses the "my medicine" form its own schema documents (`reminders.py:207, 216-217`);
`remember` can never store an `event` (its schema has no date); the tool log line says "ok" whenever the call
returned (`agent_bridge.py:253-254`); `notify_family` and `emergency_alert` answer queued or alerted while LINE
refuses every push.

**What to do first** ([docs/TOOL_PRIORITIES_2026-10-18.md](docs/TOOL_PRIORITIES_2026-10-18.md)): three rankings (by
patient safety, the patient's experience and delivery), merged and checked against the code: 12 items now, 13 for the
next build (robot app 0.17.0), 18 later, and a 10-minute robot scenario for the tools never tried on the robot.
Checked by Claude in the code and the database: `reachy_device.auto_record` is TRUE on both rows (`CLAUDE.md:78`,
the Phase 2 rule, says FALSE until decision D2); the test medicine 「測試藥」 keeps the custom times 01:04 and 12:48, so
the robot runs a dose task for it every day (`set_test_dose.py` adds a time at each run); an expired family request
puts the dose back to its status before the request, pending or missed (`dose_confirmation.py:303-314, 498-507`);
back at pending, overdose protection can allow it again;
`reminders.py` never asks overdose protection.

**How to continue, in order**

1. **Ask the user** (asked on 18 Oct, not answered yet): set `auto_record` back to FALSE, remove the two test times
   from 「測試藥」, clear the outbox backlog; how LINE should deliver for tests (and whether to point Ai_Deep's
   webhook at this server, which takes it from the outside service).
2. **The P0 server fixes** of the priority list: the spoken cue when the camera does not recognise the patient, the
   quiet pause counted from the end of Reachy's reply, honest delivery answers, the real status in the tool log line,
   the due check listing every open dose, a send-by time for outbox messages (item 3), and the rule for an expired
   family request (the user decides it).
3. **The face lock** (the user's ask: 「你看得到我嗎」 / 「看著我」 / "can you see me" / "look at me"): part of
   `head_tracking`, with the design changes of item 21 of the priority list (aim at the face when found, never snap
   back to level, an aim box when one face is in view that never counts as seen, scan when nobody is seen). The
   brainstorming is open: the user's own description of what Reachy should do, step by step, is still to come.
4. **The OCR test on the robot** (`identify_medicine`, `verify_medication`, with a medicine bag or a prescription;
   needs `LIVE_VISION_CLOUD=1`): not run yet, because the 12:40 session was stopped. A new idea from the user, not
   designed yet: Reachy takes a photo of a prescription and the computer finishes it later, as the Scan page's cards
   (the open question: who confirms it on the computer).
5. **Then the new tools' implementation plan** (robot app 0.17.0), on a new branch (`tool-fixed-version-04`).

## 12. Wi-Fi, the LINE test with a family confirmation, and the intake map (18 October 2026, afternoon)

`tool-fixed-version-04` is `tool-fixed-version-03` (section 11) plus one code change, one report and this section:
commit `e194048` (the LINE webhook logs every event's type and why a family tap changed nothing; never an ID or the
button's data; no behaviour change; two new tests, the whole suite 3180 passed) and
[docs/INTAKE_IN_CHAT_MAP_2026-10-18.md](docs/INTAKE_IN_CHAT_MAP_2026-10-18.md). **The robot app did not change:**
still 0.16.0 (`c56bbe2`); the robot repo's and the Hugging Face space's `tool-fixed-version-04` are that same commit
(space `ae4154c`), and the space's `main` stays 0.15.0.

**State at the end of the round (the test PC).**

- **Network:** the robot moved from this PC's hotspot to a Wi-Fi network (both on it; `DEVICE_BIND` and the robot's
  `app_url` were set to this PC's address there and the robot connected). At about 18:10 this PC was on another Wi-Fi
  network (`eduroam`, a shared campus network), the old address was gone and the app could not start, so
  `DEVICE_BIND=127.0.0.1` now: the server, the web app and the LINE webhook run, **the robot cannot connect** until it
  and this PC share a network again. Then set `DEVICE_BIND` to this PC's address there, `docker compose up -d app`,
  and the robot's `app_url` (its settings page on port 8042, `POST /api/config`; it restarts its bridge).
- **LINE:** the Ai_Deep account (14 of its 200 messages used this month); its webhook points at this PC's Cloudflare
  tunnel (the user recreated the dead tunnel with `docker compose --profile line up -d --force-recreate line-tunnel`
  and ran `scripts/line-tunnel.ps1`). Pushes are **off** again between tests (`LINE_CHANNEL_ACCESS_TOKEN` empty, the
  secret kept, so family taps still reach the server). Turning them on: section 11 and the memory note.
- `reachy_device.auto_record` is **FALSE** on the active robot row: the robot was paired again at 16:50, and the two
  older rows that had TRUE are revoked. `LIVE_VISION_CLOUD=1` as in section 11.

**The tests with the user (16:48-18:05).**

- **The family confirmation works end to end.** The robot's own dose flow for a medicine at 18:00 sent the request
  and its video; the family tap at 18:02:00 confirmed it, the dose was recorded as taken (at the camera's moment) and
  the reply went out. An earlier request (16:55) was most likely tapped while the bot's webhook still pointed at an
  outside service (before 17:03), so the tap never reached this server; nothing was logged then, which is why
  `e194048` adds the logs.
- **The video:** the 16:55 clip was made and kept unsent (no public link: the webhook pointed elsewhere and this PC's
  tunnel had died with the network change); it went out at 17:05 once the tunnel and the webhook were fixed. The
  request still said the video follows in the next message (priority item 18).
- **The intake inside a chat (16:55):** the camera never recognised the patient during the 30 s watch (7 mismatch, 3
  searching), so the pill detector and the emotion check never ran; the dose was filed on the patient's word.
- **17:46:** 「我吃完了」 was not filed: the due check offered only a dose already waiting for family, so GPT-Live
  applauded and recorded nothing, and an open dose of another medicine was never offered (priority item 6).
- **Reminders in a chat** fired and were spoken (16:55, 17:45); the "my medicine" form is still refused first (item 16).
- **The label reading** (`identify_medicine`) read a box's name and strength in 13.8 s and rightly said it could not
  confirm the patient's prescription.
- **A chat cannot end** while the camera does not recognise the patient: Reachy stays awake and the goodbye is not
  heard (items 8 and 24).
- After the 18:00 dose an `extra_event` message went to family: the 2-minute watch saw another hand-to-mouth moment
  (probably drinking water).

**New findings** (not fixed):

1. **The server's access log writes each family video link in full, token included** (uvicorn's request lines for
   `/api/media/line/...`), while the design keeps only the token's SHA-256. Filter or mask that path in the access log.
2. When the public link comes back, the startup report keeps the earlier failed verdict and logs
   `[STARTUP] line: FAILED` with a working link (`startup_report.py:185-186`).
3. A tap made while the bot's webhook points elsewhere is lost for good (an operations rule: keep the webhook here).

**The intake request** (the user's: face, pill-intake and emotion recognition during a medicine intake, the stream to
the laptop when the frames are not enough, the video kept after the intake):
[docs/INTAKE_IN_CHAT_MAP_2026-10-18.md](docs/INTAKE_IN_CHAT_MAP_2026-10-18.md). All of it already exists, and the laptop
already does all the detection; the gaps are only inside a live chat: the watch does not tell GPT-Live when the face
is not recognised, a chat streams 2 frames a second, and emotion is not scored at the dose rate. Claude's
recommendation (not approved yet): tell GPT-Live when the patient is not recognised, raise the stream to the camera's
10 frames a second only for the watch, score emotion and write a mood record for the watched dose; keep
`auto_record` FALSE and the camera at 10 frames a second until decision D2.

**How to continue, in order**

1. **The user's answers:** what "keep sending the video after the intake" means (keep watching after 「我吃完了」; the
   seconds after the intake in the family video, which needs a new video notice and everyone's consent again; or
   always deliver a video that was late); decision D2; removing the test medicine's two leftover dose times.
2. **Finding 1** (the token in the access log) first, then the P0 fixes of the priority list.
3. **The intake inside a chat:** a short design from the map, then the plan and the build (both repos).
4. Then the face lock, the OCR test, and the new tools' plan (robot app 0.17.0), on `tool-fixed-version-05`.

---

# History: handoff of 2 October 2026

Kept as it was written. Parts of it are out of date (robot app version, SenseVoice, the publishing and
LINE items); the sections above win where they disagree.

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

11. **Faster check-ins, with timings (2 Oct, evening).**
    - **Model:** reasoning is switched off, and the model is pinned (`LLM_MODEL=inclusionai/ling-3.0-flash-sante:free`) with `openrouter/free` as fallback and an 8 s deadline. Replies went from 2.7–17.6 s, often empty, to about 1.5 s.
    - **Robot app 0.5.0:** hands over speech sooner (0.5 s of silence instead of 0.6 s + a 0.7 s gap) and mutes itself for less time after speaking. It is deployed.
    - **Timings:** each turn stores how long hearing, the model and speaking took. The Conversations page shows them per turn, adds a "Response times (last 7 days)" panel and a CSV download. The data comes from `/api/conversations/metrics/summary`.
    - **Stale chats:** a chat left open (the robot switched off mid-conversation) is closed after 15 minutes as `abandoned`.
12. **Dose videos for family on LINE (opt-in).**
    - **The message:** the "dose taken" LINE message now says how each dose was recorded and the AI's estimate that it was taken (for example `82%（高）`, or "not determined" when the camera saw nothing), and that the AI can't see the pill itself.
    - **The video:** with the patient's consent (Family page → "Dose videos for family", notice `app/legal/video/`), each recorded dose's 10–20 s clip goes along as a LINE video.
    - **Deletion:** the clip is deleted from the laptop once each family member's LINE app has downloaded it or played it to the end, after 24 h in any case, and at once if the switch is turned off.
    - **Needs:** the LINE tunnel (`scripts/line-tunnel.ps1`), because LINE apps download the clip from the laptop.

13. **Self-harm alerts fixed (2 Oct, 23:00).**
    - **The miss:** 「我想自残」 raised no LINE alert. The speech-to-text writes Simplified Chinese, and the keyword list had no 自殘/自傷 at all.
    - **Layer 1, the keyword list:** now checks in Traditional and ignores spaces and punctuation. It was red-teamed against about 180 sentences.
    - **Layer 2, the AI:** a check runs alongside every reply and catches indirect wording such as 一了百了 or 不想拖累孩子. It was verified live on the pinned model.
    - **Layer 3, the backstop:** the end-of-chat summary raises the alert if both earlier layers missed.
    - **Alerts:** one per conversation, and the patient hears the help-line reply.
14. **Robot camera is capped at 10 fps by Pollen's daemon** (`IPC_FPS = 10` in its media_server.py). The server needs 12, so every robot dose goes to family confirmation as "degraded".
    - **Robot app 0.5.1 (deployed):**
      - faster encoding;
      - stall logging on both the robot and the server;
      - the confirmation clip now says 「我沒辦法確定您吃了沒，會請家人幫忙看看。請先別再吃一次喔。」 instead of starting with 「謝謝您」.
    - **To lift the cap:** run `python tools/deploy_to_robot.py --camera-ipc-fps 15`, then restart the daemon in a quiet window. This is not done yet, and the CPU cost at 15 fps hasn't been measured.
    - **The confirmation request** shows the AI's estimate (e.g. `48%（不確定）`) and the measured frame rate.
    - **Dose videos:** with the patient's consent, the clip goes out with the request, whatever the frame rate.
    - **Note:** deploying the robot app stops it. Start it again with `curl -X POST localhost:8000/api/apps/start-app/medcare_reachy` on the robot, then rerun the deploy.

15. **3 Oct, morning: robot app 0.5.2 and day knowledge (deployed).**
    - **The 「嗯」:** Reachy says 「嗯」 about 1 s after the patient stops talking.
    - **Gestures:** the antennas and body move while Reachy thinks, and the antennas while it speaks. Both can be switched off on the robot settings page.
    - **Day knowledge:** replies know the date, time, lunar date, weather (Open-Meteo, 台北 by default, `WEATHER_*` in `.env`) and Taiwan holidays and festivals.
    - **Backup model:** pinned to `apodex/apodex-1.1-mini:free`.
    - **Still to check on the real robot:** how loudly the 「嗯」 leaks back into the microphone (`echo_dropped`, `handover_ms` on turns with `ack_ms`); the gesture log lines `gesture from the pose measured ...`.

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
  - `http://localhost:8080/health`: everything `true`. `ocr` needs `GEMINI_API_KEY` in `.env` (then scanning works for every account, [docs/OCR.md](docs/OCR.md)); `line` needs the LINE keys.
  - Reachy card: *Online*.
  - Robot settings page `http://<robot>:8042`: `state: running`, `missing_clips: 0`.
- **Test reminder:** Reachy card → *Send test alert*. It uses the first pending or missed dose, and with auto-record on it can change that dose's record.

## Secrets

These are not written in any file in git:

| Secret | Where it lives |
| --- | --- |
| `SECRET_KEY`, `OPENROUTER_API_KEY`, `GEMINI_API_KEY` (prescription OCR), LINE keys, `BACKUP_PASSPHRASE` | `.env` (gitignored). **Keep `SECRET_KEY` when moving laptops**: it signs the robot's pairing key. |
| Robot pairing key (`rdv1.…`) | Shown once at pairing; stored only in the robot's `settings.json`. The server keeps only its SHA-256. |
| Robot SSH password | Ask the owner. |
