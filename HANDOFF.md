# Handoff — MedAiCarePlus + Reachy Mini

Status as of **12 October 2026**, GitHub branch **`reachy-integration-0.2`** (sections 6 and 7 are new in it;
sections 1-5 describe `reachy-integration-0.1`, which 0.2 builds on). No passwords, keys or tokens are written here;
[step 4.3](#43-create-env-and-put-in-the-credentials) says where each one goes.

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
   6). Phase 2b comes before any real patient uses it: the patient notice and opt-in, the camera kept on during the
   chat to keep checking the face, reconnecting, a daily budget, and a second transcript for the safety check.

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
(billing stops) and starts again with the last 20 turns when the patient speaks. A talking stretch pauses at the next
gap in the talk after 10 minutes, and at 11 minutes whatever is happening. Reachy rests after 10 minutes with no voice
and nobody seen, and ends the chat for a medicine that falls due. This settles the old question about the live chat's
45 s silence ending: only robots older than 0.9.0 keep it. Every timing is one constant (for example
`QUIET_PAUSE_SECONDS` and `REST_AFTER_SECONDS`), to change after trying. Still to check on the robot with the user:
greet, talk, stay quiet (pause), speak again (restart), say 拜拜.

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
