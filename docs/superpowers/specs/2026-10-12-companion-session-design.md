# Companion session: Reachy stays until goodbye (Phase 2a follow-up)

Date: 12 Oct 2026. Branch: `live-voice-2a` (after Phase 2a's tasks 1-11, the controller tasks 8b/8c and the final
review fixes). Builds on `docs/superpowers/specs/2026-10-12-live-voice-2a-design.md` ("the live spec") and robot app
0.7.1 (a check-in no longer ends on silence).

## 1. Why

The user tried Reachy and decided that a companion must not leave because the patient is quiet or steps away. In
their words: GPT-Live should start "when [Reachy] has seen the user and his speaking voice is received", and "Reachy
stays awake and keeps the user company until the user says 拜拜 or 再見, the words that end the companion session".

Decisions the user made on 12 Oct (each asked as a question):

| Question | Decision |
|---|---|
| What happens to GPT-Live while the patient is quiet? | Pause it (billing stops); Reachy stays awake and listens on the robot; GPT-Live restarts with the conversation so far when the patient speaks. |
| When does Reachy rest without a goodbye? | After 10 minutes with no voice **and** nobody seen by the camera. While the patient is in view, Reachy stays awake. |
| What does Reachy do when it first sees the patient? | Greets in its own voice (free, on the robot), then listens; GPT-Live starts when the patient answers. |
| What starts a companion session? | The same as today: a check-in task (after the last medicine of a slot, or "Talk to Reachy now"). No always-on watching. |
| How is pause and restart built? | One connection for the whole session; the server pauses and restarts GPT-Live inside it. |
| Camera or voice first? | Camera first: GPT-Live starts or restarts only when the camera recognised the patient within the last 15 s **and** the patient speaks. |
| Timings | "Try it first, keep it open for changes later": every timing is one named constant. |

## 2. What the patient experiences

1. A check-in starts as today. Reachy wakes and looks for the patient with the camera (the check-in search, which
   verifies the patient by face alone, as in CLAUDE.md "Finding the patient before a check-in").
2. When the patient is verified, Reachy says its greeting **in its own voice** (Matcha on the robot), with the call
   name from the Reachy card (「王奶奶，今天感覺怎麼樣？想跟我聊聊嗎？」, `memory.opening_line`). It then holds its head up
   and listens. Nothing is billed.
3. The patient speaks. If the camera recognised the patient within the last 15 s, GPT-Live starts within about 1-2 s,
   gets the patient's first words (kept meanwhile) and answers. It knows Reachy already greeted.
4. After 30 s with no patient speech (and GPT-Live not talking), GPT-Live is paused: billing stops. Reachy stays awake,
   head up, listening. When the patient speaks again (and was recognised in the last 15 s), GPT-Live restarts with the
   conversation so far.
5. The session ends when:
   - the patient says goodbye (`conversation.GOODBYES`: 拜拜, 再見, 掰掰, 不聊了, …): Reachy answers, then sleeps;
   - a risk is found (the help line in Reachy's own voice, family alerted once, as today);
   - consent is withdrawn, the robot is unpaired, or the robot's live switch is turned off (the 10 s access re-check);
   - for 10 minutes there is no patient voice and nobody seen: Reachy says 「我先休息一下，有需要再叫我喔。」 and sleeps;
   - a medicine task falls due: Reachy says 「該吃藥囉，我們等一下再聊。」, ends the session and starts the medicine task;
   - someone stops the robot from the app (`stop_all`), or the microphone consent is withdrawn (as today).
6. When live voice is off or GPT-Live is not available, **today's text chat follows the same rules** (stays until
   goodbye, the same rest and medicine rules). Only the "brain" differs.

## 3. Server

### 3.1 The live relay starts paused (`app/services/live_session.py`)

- **Opening.** After the start checks (unchanged: `LIVE_VOICE_TEST`, rate, check-in consent, the leased task, the
  robot's live switch, a usable live model), the server writes the conversation row (`mode` live) with Reachy's
  opening as turn 1 and answers `{"type": "ready", "conversation_id", "speech_text"}`: `speech_text` is the opening in
  the voice's Simplified characters (`conversation.speech_text`), which the robot says itself. **GPT-Live is not
  started.** The relay is *paused*.
- **Robot to server: `{"type": "speech"}`** when the robot's voice detector hears the patient begin speaking (3.2 and
  4.3). The microphone frames keep flowing as today.
- **While paused,** the server keeps the last `PRE_ROLL_SECONDS` (2 s) of microphone audio and sends nothing to OpenAI.
- **Restart (also the first start).** On `speech` while paused, and only when the camera-first check passes (3.2), the
  relay starts a new GPT-Live session within `CONNECT_TIMEOUT` with: the live rules and the call name (as today); the
  memory block when memory consent is current (as today); the conversation so far, the last `HISTORY_TURNS` (20) stored
  turns, given as quoted transcript data (never as instructions); and the note that Reachy has already greeted and
  should answer what the patient says now. Then it sends the kept pre-roll audio, then the live microphone. A start
  that fails falls back to today's text chat in the same conversation (`fallback` with the conversation id), as a
  mid-chat GPT-Live failure does today.
- **Pause.** When no patient speech has been heard for `QUIET_PAUSE_SECONDS` (30 s) and GPT-Live's sound has ended,
  the relay stores the open turns and closes GPT-Live (session usage added up), then is paused again. A talking
  stretch also pauses at `STRETCH_MAX_SECONDS` (600 s); the patient's next words restart it.
  *(Amended after the review: a stretch older than 600 s pauses at the patient's next turn gap, when nobody is talking
  and GPT-Live is silent; `STRETCH_HARD_MAX_SECONDS` (660 s) pauses it anyway, with a `flush` if GPT-Live was
  speaking. Every pause first handles the closed stretch's queued events: the patient's words are screened and stored,
  and OpenAI's cut-offs are judged. After a pause at the hard limit with recent speech, the next stretch starts at
  once.)*
- **Not used for robot app 0.9.0:** `SILENCE_END_SECONDS` (the 45 s ending) and the whole-chat `LIVE_MAX_SECONDS`
  limit. The robot's rest rule and the goodbye end a session. (Both stay in the code for older robots, below.)
- **Unchanged while a stretch runs:** the 0.5 s hold of each reply, screening of every transcript fragment, `block()`
  with the robot's help line, the judgement of OpenAI's cut-offs (B4, only an explicit none falls back), layer 2 on
  every stored patient turn, turns as amended in C1, goodbye detection (the chat ends `GOODBYE_GRACE_SECONDS` after
  GPT-Live's answer), the bounded database calls.
- **Unchanged while paused too:** the 10 s access re-check (robot, live switch, robot consent, check-in consent,
  memory consent) runs whether a stretch is open or not. After a risk (`self._risk`), nothing restarts.
- **Cost record:** `conversation.live` keeps `usage_seconds` as the sum over all stretches and adds `stretches` (how
  many times GPT-Live was started).
- **Old robots:** a `start` whose `bridge_version` is below 0.9.0 gets today's behaviour (GPT-Live starts at once and
  says the opening; the 45 s silence ending and the 600 s limit apply). The constants for that path stay.

### 3.2 Camera first (the server enforces it)

The relay restarts GPT-Live on `speech` only when the patient's robot monitor session in the in-memory
`MonitorRegistry` shows the patient verified within `SEEN_RECENT_SECONDS` (15 s) (`MonitorSession.verified_at`, the
same check-in search session the robot keeps open, 4.2). Otherwise it stays paused and answers
`{"type": "not_seen"}` (the robot logs it; Reachy keeps listening). A stretch already running is not cut when the
patient steps out of view. So only the identified patient's voice starts a cloud session.

*(Added after the review: `/api/device/monitor/start` accepts a `companion` flag. A companion `observe` session uses the
check-in face rule, the OpenVINO match alone, so the patient is recognised from across the room even after a dose.)*

### 3.3 Today's text chat (`app/routers/api_device.py`)

- The patient-turn limit (`conversation.MAX_PATIENT_TURNS`, 6) no longer ends a check-in chat from robot app 0.9.0;
  only a goodbye or a risk does (the robot ends it for rest, medicine or a stop). An older robot keeps the limit
  (the robot's version is known from its heartbeat's `bridge_version`).
- `/conversations/{id}/end` accepts the new reason `medicine` (the 10-minute rest uses the existing `patient_left`).

### 3.4 Heartbeat: medicine first

The heartbeat answer gains `task_due` (bool): true when this robot could lease a different open task for the patient
now (the same rule as `reachy_tasks` leasing: a queued task whose doses are startable). The robot then ends the
companion session (4.5).

*(Added after the review: the heartbeat accepts a `companion` flag while a companion session runs, and the server then
keeps the held in-progress task from expiring (`reachy_tasks.extend_for_companion`, `COMPANION_TASK_EXTEND_SECONDS` =
600 s past each heartbeat). `task_due` is read after the heartbeat's write transaction; if that read fails, the answer
is false.)*

### 3.5 End reasons

The relay accepts `patient_left` and `medicine` from the robot's `end` (added to `ROBOT_END_REASONS`).

## 4. Robot app 0.9.0

### 4.1 One companion controller for both brains (`bridge/session.py`)

The check-in becomes a companion session with one controller. On entering it: say the opening in Reachy's own voice
(the live `ready`'s `speech_text`, or today's `conversation_start` answer), keep the camera (4.2), then run the brain:
the live chat (4.3) or today's text chat. `CHECKIN_MAX` (5 minutes) is removed; the session ends only as in section 2.

### 4.2 The camera stays on

The check-in search's monitor session (observe mode, check-in search) is kept open for the whole companion session,
instead of being ended before the chat. Frames are sent at `COMPANION_CAPTURE_FPS` (2 per second) to save the robot's
and the server's CPU. The robot tracks when the server last reported the patient verified (`seen_at`, already kept).

### 4.3 Hearing the patient start (`bridge/live.py`)

During a live companion session, `LiveChat` runs its own silero voice detector (the model the listener already uses,
`silero_vad.onnx`) on the microphone frames it already reads. It sends `{"type": "speech"}` when speech begins outside
Reachy's own playback and its `MUTE_TAIL_SECONDS` tail (the robot's echo canceller also helps), at most once per
`SPEECH_SIGNAL_GAP_SECONDS` (1 s). (Amended after the review: a signal is also held back while Reachy says its own line and for `SAY_TAIL_SECONDS` after it, and within `SPEECH_ECHO_GUARD_SECONDS` (0.75 s, measured from the push) of the last block with sound pushed to the speaker. `MUTE_TAIL_SECONDS` is no longer used as a gate here.) In today's text chat the listener (`voice.py`) already hears the patient.

### 4.4 Rest

The controller ends the session as `patient_left`, after Reachy says its rest line, when for `REST_AFTER_SECONDS`
(600 s) there was neither patient speech (the voice detector, or a text-chat turn) nor a verified sighting.

### 4.5 Medicine first

When a heartbeat answer has `task_due` true, the controller waits for Reachy to finish its current sentence, says its
medicine line, ends the session as `medicine` (live: `end`; text chat: `/end`), and the runner leases the next task.

### 4.6 Version

0.9.0. Server and robot are updated together; an older robot keeps today's behaviour (3.1).

## 5. Safety and privacy

- Every layer of the live spec stays (B1-B5) and the text chat's three layers stay.
- Camera first means a cloud session starts only for the identified patient's voice; the pre-roll makes sure the first
  words are screened too.
- The access re-check keeps running while paused; after a risk nothing restarts; the help line is always Reachy's own
  voice.
- No new data leaves the home: the same audio, name and notes as Phase 2a, only less often (paused while quiet). Live
  voice stays test-only behind `LIVE_VOICE_TEST` (no patient notice covers it yet; Phase 2b).

## 6. Tunable constants (one place each, easy to change after trying)

| Constant | Value | Where |
|---|---|---|
| `QUIET_PAUSE_SECONDS` | 30 | live_session.py |
| `STRETCH_MAX_SECONDS` | 600 (amended after the review: the next turn gap after it) | live_session.py |
| `STRETCH_HARD_MAX_SECONDS` | 660 (added after the review) | live_session.py |
| `SEEN_RECENT_SECONDS` | 15 | live_session.py |
| `PRE_ROLL_SECONDS` | 2 | live_session.py |
| `HISTORY_TURNS` | 20 | live_session.py |
| `REST_AFTER_SECONDS` | 600 | robot session.py |
| `COMPANION_CAPTURE_FPS` | 2 | robot session.py / runner.py |
| `SPEECH_SIGNAL_GAP_SECONDS` | 1 | robot live.py |
| `SPEECH_ECHO_GUARD_SECONDS` | 0.75 (added after the review) | robot live.py |

## 7. Testing

- Server (fakes, fake clock): starts paused (no GPT-Live until `speech`); camera-first refuses `speech` without a
  recent sighting (`not_seen`) and accepts it with one; the pre-roll reaches GPT-Live first; the restart's instructions
  carry the last 20 turns as quoted data and the "already greeted" note, and GPT-Live does not greet again; pause after
  30 s quiet closes GPT-Live and billing stops (usage summed, `stretches` counted); `STRETCH_MAX_SECONDS` pauses; no
  restart after a risk; the access re-check runs while paused; an old robot (bridge_version < 0.9.0) gets today's
  behaviour; the text chat no longer closes at 6 turns for check-ins; `/end` accepts `medicine`; the heartbeat's
  `task_due`.
- Robot (fakes): the opening said in Reachy's voice; `speech` sent on the patient's voice and never on Reachy's own;
  the camera kept at 2 fps during the session; rest after 10 min with no voice and no sighting (and not while the
  patient is seen); the medicine interrupt; the text chat under the same controller; `CHECKIN_MAX` gone.
- Live on this PC (real gpt-live-1): one session with a pause and a restart: the second stretch remembers the first.
- On the robot, with the user: greet, talk, stay quiet (pause), speak again (restart), say 拜拜.

## 8. Not in this change

- Always-on companion (Reachy starting a session by itself whenever it sees and hears the patient): its own phase,
  with its own consent.
- A patient notice for live voice (Phase 2b).
- Keeping GPT-Live open through quiet time (the user chose pause).
