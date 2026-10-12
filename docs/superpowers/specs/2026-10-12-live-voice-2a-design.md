# Live voice check-ins with GPT-Live, first version (Phase 2a)

Date: 2026-10-12. Branch: `live-voice-2a`, from `reachy-integration-0.1` (`e674aea`).
Design approved by the user on Telegram ("Approve", 2026-10-12), after five decisions: Q1 A (a minimal version first,
for testing), Q2 A (hold every reply about 0.5 s), Q3 A (GPT-Live answers by itself), Q4 A (Reachy calls the patient by
name), Q5 A (when OpenAI ends a session, our own safety check decides).

## 1. Why

A check-in today goes one turn at a time: the robot turns speech into text on the robot (Whisper Base, about 9 s), the
server's model writes a reply (about 1 s), and the robot speaks it with its own voice (first sound about 2 s). A turn
takes 12-14 s, and the user said "it's not fast like gpt-live-1".

gpt-live-1 is OpenAI's full-duplex voice model: it listens to a live audio stream and speaks back by itself, over its own
Live WebSocket (`wss://api.openai.com/v1/live/sessions`). The TCU companion on this robot already talks with it.
gpt-live-1 can't be a text model (Test answers "not a chat model"), so MedCare needs a separate live path.

Measured in the Phase 2 spike (11 Oct, 51 GPT-Live sessions, two skeptics per claim):
- GPT-Live answered 「沒關係，天冷就別勉強自己…」-style replies in good Traditional Chinese; no medical advice in 4 of 4
  medical questions; half the full replies were longer than the 30-character rule.
- GPT-Live has **no event that stops its speech**; a server that relays the audio can stop a reply by not forwarding it.
- MedCare's `conversation.screen()` on GPT-Live's growing input transcript fired **0.80-2.31 s before** the reply was
  audible in 5 risk runs; for a calm-sounding sentence the reply could start at the same moment the words arrived.
- OpenAI's moderation **closed the session on 9 of 9** clear self-harm statements (`content_filter` error, then
  `session.closed` with reason `content`), sometimes mid-reply; it did not on 2 harmless controls.
- One rate covers both directions; a 16 kHz session sends 16 kHz back. The robot's audio runs at 16 kHz.
- The Reachy Mini Wireless has hardware echo cancellation (XMOS XVF3800), switched on; residual echo is unmeasured.
- A muted session with no audio sent is not billed; GPT-Live costs about US$0.05 per session minute.

## 2. Scope

In (2a):
- **A.** The server's GPT-Live client and the live relay on the robot's device port.
- **B.** Live safety: screening, the 0.5 s hold, the help line in the robot's own voice, OpenAI cut-offs, layer 2 in the
  background; layer 3 unchanged.
- **C.** Saving the live chat as conversation turns; endings; fallback to today's text chat.
- **D.** Reachy calls the patient by name (both chat types).
- **E.** The test switch and the live settings on the cards.
- **F.** Robot app 0.8.0: a live audio mode.

Out (Phase 2b, before any real patient uses it):
- The patient opt-in notice and its consent scope; the card switch for patients.
- Checking the face again during the chat (the camera ends when the chat starts, as today).
- Reconnecting a dropped live session (2a falls back to the text chat instead).
- A daily live budget per patient.
- A second, independent transcript for the safety check; a server-side echo guard on transcripts.

## 3. Design

### A. Server: GPT-Live client and the relay

**A1. GPT-Live client** (new `app/services/live_client.py`): a port of the TCU companion's
`reachy-mini-tcu/companion/ai/openai_live.py` without its Responses backend, tools and web search.
- Connects to `wss://api.openai.com/v1/live/sessions` with `Authorization: Bearer <OpenAI key>` from the card (see E2).
- Sends `session.start` with: model (default `gpt-live-1`), `store: false`, output voice (default `marin`),
  `audio.format {type: "audio/pcm", rate: 16000}`, and `instructions` (A3).
- Sends input audio (`session.input_audio.append`, base64 PCM16 mono 16 kHz), `session.input_audio.mute`/`unmute`
  (waiting for the acknowledgement), `session.commentary.append` (Reachy's opening), `session.close` (and waits up to
  2 s for `session.closed` with usage).
- Delivers events to the relay: output audio deltas, `session.input_transcript.delta`, `session.output_transcript.delta`,
  errors (with OpenAI's `code`), `session.closed` (with `reason`) and usage seconds.
- Errors become machine codes: `live_auth` (401/403), `live_unavailable` (connect/5xx/timeout), `rate_limit_exceeded`,
  `content_filter`, `live_closed`.
- Never logs the key; uses `ai_crypto.redact` for any error text it stores.

**A2. The relay WebSocket** (`app/routers/api_device.py`, new `app/services/live_session.py`):
- Route `GET /api/device/conversations/live` (WebSocket) on the **device port only**. The device-port rule in
  `app/main.py` covers only HTTP today (flaw F22): make the rule also refuse WebSocket scopes on the wrong port (pure
  ASGI middleware, or a port check in the route), with a test that walks every WebSocket route.
- Auth: a WebSocket version of the robot-key check (`device_auth.get_device_ws`): read the `Authorization` header,
  same lookup, revoked and consent checks as `get_device`. On failure: accept, then close with 4401 (bad key) or 4403
  (consent).
- One live chat per patient: a second connection closes with 4409.
- **Robot → server:** first a JSON `start` message `{task_id, language, rate: 16000, bridge_version}`; then binary frames
  of PCM16 mono 16 kHz (20-40 ms each); JSON `end` when the robot stops (`reason`), and `playing`/`idle` reports (F3).
- **Server → robot:** JSON `ready {conversation_id}`; binary PCM frames (GPT-Live's voice, after the hold, B2); JSON
  `flush` (drop queued audio now); `say {speech_text, reason}` (the robot speaks this with its own voice, e.g. the help
  line); `fallback {reason, conversation_id|null}`; `end {reason, risk}`.
- On `start` the server checks, in this order, and answers `fallback` (and closes) at the first failure, before any
  conversation row exists: check-in consent (`reachy_tasks.checkin_allowed`, as `/conversations`), the leased check-in
  task, live enabled for this robot (E1), a usable live connection (E2). Then it connects to GPT-Live within 8 s
  (measured start about 1.2 s); a failure → `fallback {reason: live_unavailable}`.
- Only after GPT-Live is connected: insert the conversation row (`mode = 'live'`, `model = <live model id>`), add
  Reachy's opening as its first turn, send `ready`, and give GPT-Live the opening line through
  `session.commentary.append` so it says it in its own voice.

**A3. Instructions for GPT-Live** (built by the server for each session):
- `conversation.reply_prompt(language)` (Reachy's rules, the day's background), plus live rules: answer in one or two
  short sentences (**at most 30 characters** — stated firmly, since GPT-Live broke the rule in half its replies),
  answer everything yourself (no lookups), if asked say you are Reachy, a robot with an AI voice; never mention
  medicines, doses or illness; if the patient talks about harming themselves, say you are worried and that you will
  get help (the server plays the help line, B3).
- The memory block (`memory.build_block`) only when memory consent is current, as the text chat does.
- The patient's call name (D) when set.

### B. Live safety

**B1. Layer 1, screening** (`live_session.py`): `conversation.screen()` runs on every `session.input_transcript.delta`
over the patient's open turn (C1). A match:
1. **holds** at once: the server stops forwarding GPT-Live audio and sends `flush`;
2. **confirms** when the patient's text has settled (no new input fragment for 400 ms): `screen()` again on the whole turn
   (its NOT_RISK rules can clear a partial match such as 「真是想死」 before 「他」 arrives). Still a match → B3. Cleared →
   forwarding resumes with the audio held meanwhile (if the reply has moved on, the held audio is dropped).

**B2. The reply hold** (Q2 A): the start of every GPT-Live reply is held **500 ms** before the first frame goes to the
robot. A reply starts when output audio turns non-silent (any sample above 120, as the TCU companion detects it) after
the patient spoke. After the onset, audio flows through as it arrives. The hold gives B1 time to act before anything is
heard. Measure `hold_ms` and `first_audio_ms` per Reachy turn.

**B3. Blocking a risky chat** (`block(reason)`), used by B1, B4 and B5:
- stop forwarding, send `flush`; mute GPT-Live and send `session.close`;
- flag the patient's turn, add a Reachy turn with `conversation.HELPLINE[language]`;
- send the robot `say {speech_text: <HELPLINE in speech form>, reason: "risk"}` — the robot speaks it with **its own
  voice** (never GPT-Live's: OpenAI's moderation cut it off 9 of 9 times);
- `conversation.alert_family()` as today (one alert per conversation), end the conversation with reason `risk`, send
  `end {reason: "risk", risk: true}`.

**B4. OpenAI ends the session** (`content_filter` error, or `session.closed` with reason `content`), Q5 A: the server
runs `conversation.classify_risk()` (the card's safety job) on the patient's last turn at once. `SELF_HARM`/`OVERDOSE`
or `unknown` (no answer) → `block()`. `NONE` → the chat continues as today's text chat: `fallback {reason:
"live_ended", conversation_id}` (C3).

**B5. Layer 2:** each closed patient turn gets `classify_risk()` in the background (as today, with `_late_risk_check` on
`unknown`); a risk → `block()`. It does not delay replies.

**B6. Layer 3** (the summary with its RISK line, memory facts) is unchanged: it reads the stored turns after the chat.

### C. Turns, endings, fallback

**C1. Turns** (amended after the Task 7 review, see the spike's 3b). Transcript fragments are grouped per role.
GPT-Live is full duplex: it can start its reply before its transcript of the patient is complete (in the spike the
last input fragment came 0.84-1.28 s after the patient stopped and the reply became audible 0.00-0.31 s after it; in
the idiom run 「會想」 arrived with 「他」), and OpenAI's Live guide says input transcript deltas "do not define complete
turns". The earlier rule, "a role switch closes the other role's group", split one utterance into several turns and
missed a goodbye split by GPT-Live's early answer. The rule:
- A patient turn is closed by a pause over `TURN_GAP_SECONDS` (1.5 s) since its last fragment, by `MAX_GROUP_CHARS`, or
  by the chat's end. **GPT-Live's words never close it.** A patient fragment that arrives while the patient's group is
  still open (within 1.5 s of its last fragment) continues that turn, even if GPT-Live has started answering.
- A patient fragment that **starts** a new patient group closes Reachy's open group (the reply it interrupted or
  followed), except GPT-Live's spoken opening while its sound is still playing (below).
- A fragment that **continues** the patient's group never closes Reachy's group, and re-arms the hold (B2) and moves
  `_reply_due_at` only while GPT-Live has not started a reply since that patient group began. Once a reply's onset
  came after the group began, continuing fragments leave `_armed`, `_reply_due_at` and the reply's metrics alone: the
  same reply's next sound after a pause is not held again. A sound that a screening hold (B1) dropped before any of
  it went to the robot started no reply: the patient heard nothing, so GPT-Live's next sound is still the reply's
  start, held 500 ms (B2), with its timings from the turn's latest fragment.
- Turns are stored in the order their groups began: before Reachy's group is stored, a patient group that began
  earlier and is still open is stored first.
- The spoken opening: Reachy's group created before the patient's first turn began and before the opening was spoken
  is the opening group (`_Group.opening`, decided when the group is created, not when it is closed). While
  GPT-Live's sound is still playing, a new patient group does not close it; it is closed when GPT-Live's sound ends
  (`_sound_ended`) if the patient talked over it, else at the next new patient group after that, or at the end, and
  it always replaces turn 1's text. So an opening with a pause between its sentences stays whole while nobody speaks.
  The patient's words spoken over it are stored as their own turn after it.
- The goodbye check (`conversation.wants_to_end`, C2) runs on the closed patient turn, which is the whole utterance.

Each closed group becomes one `conversation_turn` row (`role` patient or reachy) with `metrics.live`: patient turns
`transcript_ms` (first to last fragment) and the risk result; Reachy turns `first_audio_ms`, `hold_ms`, `cut` (true if
blocked or flushed). GPT-Live's text is the transcript of its speech.

**C2. Endings.** The chat ends on: a goodbye in a closed patient turn (`conversation.is_goodbye`-style check, the
`GOODBYES` list), after which GPT-Live may answer once and the server ends 3 s after that reply's audio ends; 45 s
with no patient speech; `LIVE_MAX_SECONDS = 600`; the robot's `end`; a dropped robot connection; B3. On every ending:
`session.close`, store `conversation.live` = `{usage_seconds, close_reason, fallback_reason}`, end the conversation with
its reason, and run the after-chat work exactly as `/conversations/{id}/end` does (summary, memory, layer 3).

**C3. Fallback.**
- Before a row exists (consent, settings, connect failure): `fallback {reason, conversation_id: null}`; the robot
  starts today's chat with `POST /conversations`.
- After the chat started (GPT-Live error, `live_ended` from B4, an unexpected close): `fallback {reason,
  conversation_id}`; the robot continues the same conversation with `POST /conversations/{id}/turn` (today's path,
  which reads the stored turns as history). `conversation.mode` stays `live`; `live.fallback_reason` records why.
- The robot shows nothing new; it simply goes on talking the old way.

### D. Reachy calls the patient by name (Q4 A)

- Stored on the patient's account: `"user".reachy_call_name VARCHAR(40) NULL`. NULL means "use the registered name"
  (`"user".name`). Validation: 1-40 characters, the same character rules as `memory.valid_name`.
- The Reachy card gets "What should Reachy call you?" / 「Reachy 要怎麼稱呼您？」, prefilled with the registered name;
  saved through `PATCH /api/reachy/settings` (new optional field `call_name`; an empty string resets to NULL).
- **Opening line, both chat types:** `memory.opening_line(language, name)` gets the call name (or the registered name)
  whenever the patient has one, not only with memory consent. A patient-entered memory `name` fact still wins when
  memory is on (it is the patient's own newer choice).
- The name reaches the AI company as part of "the text of the conversation" (Reachy's opening line), which the robot
  notice already covers; the live instructions include it for the live chat.

### E. Settings

**E1. Live voice per robot (test)** (`reachy_device.live_voice BOOLEAN NOT NULL DEFAULT FALSE`):
- The Reachy card shows a "Live voice (test)" / 「即時語音（測試）」 switch **only** when the server's `.env` has
  `LIVE_VOICE_TEST=1`; saved through `PATCH /api/reachy/settings` (new optional `live_voice`).
- The task payload gets `live: true` only when `LIVE_VOICE_TEST=1`, the switch is on, the check-in is allowed, and a
  live connection is usable (E2). Otherwise `live: false` and nothing changes.

**E2. The live model on the AI connections card** (one-row table `ai_live`: `connection_id` → an OpenAI connection,
`model_id` default `gpt-live-1`, `voice` default `marin`, `tested_at`, `test_ms`, `last_error`):
- A "Live voice (test)" section on the card, shown only with `LIVE_VOICE_TEST=1`: pick the OpenAI connection, the model
  id and the voice (the TCU companion's list: marin, cedar, alloy, ash, ballad, coral, echo, sage, shimmer, verse).
- **Check** opens a session (`session.start`, wait for `session.started`), closes it, and saves the time or the error
  code. A live model counts as usable when its connection has a readable key and the last Check passed.
- Every `/api/ai` rule from part 1 applies (consented user, keys never returned, 422 `invalid_request`).

### F. Robot app 0.8.0 (`reachy_app/`)

**F1. Live chat module** (new `bridge/live.py`), built from the TCU companion's tested audio code
(`companion/audio/reachy_audio.py`, `base.py`, `protected_playback.py`):
- A WebSocket client to `ws://<server>:8001/api/device/conversations/live` with the robot key (the robot already has
  `websockets` in its apps environment).
- **Microphone:** one channel of the SDK's `get_audio_sample()` (16 kHz; both channels carry the processed beam), sent
  as 20-40 ms PCM16 frames. While the robot plays its own clip (`say`), it sends silence.
- **Speaker:** one audio owner during the chat: `start_playing()` once, push 20-40 ms blocks on an absolute-deadline
  clock, a jitter buffer of about 300 ms (growing toward 1 s after underruns; GPT-Live's deltas came up to 0.37 s
  apart). `flush` fades out briefly and clears the player. `say` speaks the text with the robot's own Matcha voice
  through the same queue (never `play_clip` in parallel).
- **Reports:** `playing` when the first non-silent block is pushed, `idle` when the queue drains.
- **Gestures:** the existing "speak" gesture while `playing`; still during a risk `say`.

**F2. Session wiring** (`bridge/session.py`): in `CHECKIN`, if the task payload says `live: true` and the robot app
supports it: pause the speech listener (`voice.py`; one audio owner), run the live chat, and on `fallback` continue
with today's chat (with the given `conversation_id`, or a new `POST /conversations`). A dose task's check-in follows the
same rule. Without `live: true`, nothing changes.

**F3. Version** `0.8.0` (`pyproject.toml`, `bridge/__init__.py`), deployed with `tools/deploy_to_robot.py`; heartbeats
report `bridge_version` 0.8.0.

## 4. Data and API changes

- `sql/init.sql` (idempotent): `"user".reachy_call_name`, `reachy_device.live_voice`, `conversation.mode VARCHAR(10)
  NOT NULL DEFAULT 'text'` with a CHECK (`text`, `live`), `conversation.live JSONB`, table `ai_live`.
- `PATCH /api/reachy/settings`: optional `call_name`, `live_voice` (and `auto_record` becomes optional; a field left out
  keeps its value). `/api/reachy/status` returns `call_name` (or the registered name) and `live_voice`, and
  `live_voice_test` (whether `.env` allows it).
- `/api/ai/settings` returns the `ai_live` row (no key); new `PUT /api/ai/live` and `POST /api/ai/live/check`.
- Task payload: `live` (bool).
- `.env.example`: `LIVE_VOICE_TEST=0` with a comment.

## 5. Errors and safety

- Any failure before the chat starts falls back to today's chat; the patient never waits more than the 8 s connect
  budget plus today's start.
- The OpenAI key stays on the server; the robot never sees it. Keys never appear in logs, answers or turns.
- The help line is always spoken by the robot's own voice, never by GPT-Live.
- A blocked chat alerts family once (`alert_family`'s existing guard).
- Without `LIVE_VOICE_TEST=1`, no live path is reachable: no card section, no task flag, and the WebSocket answers
  `fallback {reason: "live_disabled"}`.
- Known gaps, accepted for testing only (2b): no patient notice; transcripts come only from GPT-Live (it misheard
  「一整盒」 once); no server-side echo guard; no reconnect; no daily budget.

## 6. Testing

- **Server** (pytest, no network): a fake GPT-Live (an in-process object or local WebSocket server that plays scripted
  events) drives the relay:
  - start checks and each fallback reason; the row is created only after connect;
  - the 500 ms hold (no frame reaches the robot earlier);
  - a risk fragment holds and flushes; a cleared partial match releases; a confirmed match runs `block()` (help line
    `say`, flagged turn, alert, `end`);
  - `content_filter` → classify → risk/unknown blocks, `NONE` falls back with the conversation id;
  - turn grouping and storage; endings (goodbye, 45 s silence, max length, robot end, disconnect) all run the
    after-chat work;
  - the WebSocket port rule and auth close codes; one chat per patient (4409);
  - call name: validation, the opening line with and without memory consent, the memory `name` fact winning.
- **AI card:** `ai_live` storage, Check (fake), no key in answers, hidden without `LIVE_VOICE_TEST`.
- **Robot app** (pytest with fakes): the live client against a fake server: frames sent at the right size, playback
  queue and jitter buffer, `flush`, `say` through the same queue, `playing`/`idle`, fallback to today's chat, the
  listener paused and resumed.
- **Frontend:** Playwright for the name field, the live switch (shown only when allowed), and the card's live section;
  screenshots at 390 and 1280 px.
- **Live, on this PC:** a Check against the real gpt-live-1; one relay session with a recorded patient sentence (no
  robot).
- **On the robot, with the user:** one live check-in (latency per turn, the name greeting, an echo check: Reachy's own
  voice must not become patient turns), one goodbye, one fallback (switch off the PC's Wi-Fi briefly or stop the
  connection) — measured, not assumed.

## 7. Review focus

1. Audio ownership on the robot: the speech listener and the live loop must never use the microphone or speaker at the
   same time; a `say` must not overlap GPT-Live audio.
2. The hold and the flush must also cover audio already queued on the robot (a `flush` clears the robot's queue).
3. A `block()` racing an ending or a fallback must alert family at most once and end the conversation once.
4. The fallback after a mid-chat failure must not lose the patient turns already stored, and the text chat's turn limit
   must count from the fallback.
5. The WebSocket must be unreachable on the public port, with any key.
