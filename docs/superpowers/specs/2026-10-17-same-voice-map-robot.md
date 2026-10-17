# Same voice: the robot side today (medcare-reachy-bridge)

Read-only map of `C:\Reachy_Mini\repo\medcare-reachy-bridge`, branch `feature/merged-live-emotion`, HEAD `437b136`,
`__version__ = "0.15.0"` (`medcare_reachy/bridge/__init__.py:3`), working tree clean. All paths below are relative
to `medcare_reachy/bridge/` unless they start with `tools/` or `tests/`. Server lines are quoted only where the robot
depends on them (`C:\Reachy_Mini\repo\MedAiCarePlus`, branch `agent-tools-fixes` as checked out right now).

[checked: corrected] Two paths break the rule above: `service.py` is `medcare_reachy/service.py` (one level above
`bridge/`), and `tests/fakes.py` is `medcare_reachy/bridge/tests/fakes.py` (the repo-root `tests/` holds only the deploy,
app, settings and vision-accuracy tests). The server was checked at `8215ff0`. Its HEAD moved from `d37502d` while this
check ran, because another job is committing, and `13a6cbe` changed `live_session.py` (the help-line send moved from
line 2116 to 2120). So the server line numbers below can drift again. Server line numbers in this file are at
`8215ff0`. By the end of the check HEAD was `792bf8b`, which shifts `live_session.py` by +1 after line 1013 and by
+2 after line 1052 (goodbye 1046, help-line send 2122, `end` send 2243); nothing before 1013 and no other cited server
file changed.

**Short answer.** Inside a live chat every Reachy line goes through one path: `LiveChat._start_say` ->
`Speaker.synth` (Matcha on the fly, one chunk at a time) -> `Player.add_say` (live.py:656-679). That one path is
where a kept marin WAV can be swapped in. Around the chat, the check-in greeting is Matcha on the fly through
`Speaker.say` -> `robot.play_clip` (session.py:295-297, 1050-1066), and the wind-down is a Matcha WAV that the deploy
tool rendered, played by `ClipPlayer.play` (session.py:920-928). The reminder itself already arrives as marin PCM
from the server, but it is treated as GPT-Live audio, not as a Reachy line.

---

## 1. Every place the robot speaks its own line during or around a live chat

### 1a. The SAY path inside a live chat (live.py)

All of these share one mechanism.

| Step | Where | What happens |
|---|---|---|
| Entry | live.py:656-665 `_start_say(text, reason)` | Splits the text with `speech.chunks` (speech.py:75-102). It gives up, with a warning, when there is no text **or `speaker.available` is False** (live.py:658-661). It sets `still = reason == "risk"` (live.py:663) and starts the `_say` task. `_saying += 1` (live.py:662). |
| Synthesis | live.py:667-679 `_say` | For each chunk it calls `run_blocking(self.speaker.synth, piece)`, which is Matcha on the fly (speech.py:203-220: float32 mono at Matcha's own rate, `SPEED = 0.85`, speech.py:24). Each chunk is resampled to 16 kHz and turned into PCM16 (`resample` media.py:153-158, `to_pcm` live.py:115-117), then queued with `player.add_say(pcm, still)`. A synthesis error is logged and the line is lost (live.py:676-677). |
| Queue | live.py:163-165 `Player.add_say` | It takes **PCM16 mono at RATE = 16000** (live.py:56), kind SAY or STILL (live.py:89). Reachy's lines skip the jitter buffer: only LIVE waits on `_may_play` (live.py:194). They play after whatever was queued before them (live.py:167-170). |
| Playback | live.py:742-767 `_play_loop` | 20 ms blocks go to `robot.push_audio` (media.py:330-341, float32 mono at `LIVE_RATE = 16000`, media.py:42). |

**The SAY rules, as the code enforces them today:**

- **No GPT-Live audio mixed in.** `_audio` drops every binary frame while `_say_active()` (live.py:639-643; `_say_active`
  is live.py:681-682, which is `_saying > 0` or a SAY/STILL item queued). It counts the drops in `dropped_audio`.
  This also drops the server's marin **reminder** PCM if it arrives during a SAY (see 1b, reminder).
  [checked: corrected] The server tries to avoid that: `speak()` waits while `_robot_playing` is set (server
  live_session.py:806-813; set by the robot's `playing`, cleared by `idle`, 1127-1134). But the robot sends `playing`
  only at its first block with sound. While Matcha synthesises a line's first chunk (`_saying > 0`, nothing played
  yet), the server sees a quiet robot. A reminder that fires in a running chat at that moment (server
  reminders.py:284-299, no `after_opening` wait), for example during the opening, is dropped on the robot, in whole or
  in part, while the server stores it as said and marks it `fired`. A kept marin clip shortens that window (no
  synthesis), but does not close it.
- **Microphone tail / echo.** While a line is being made or played, and for `SAY_TAIL_BLOCKS` after it, the
  microphone sends zeros (live.py:700-701). The tail is `SAY_TAIL_SECONDS = 0.6`, which is 30 blocks (live.py:67-70)
  and is re-armed by every SAY/STILL block (live.py:771-774). `_hearing()` is False then, and also within
  `SPEECH_ECHO_GUARD_SECONDS = 0.75` of the last block with sound (live.py:86, 734-740), so no `speech` signal goes
  to the server. No Whisper is involved here: the speech listener is suspended for the whole live chat
  (session.py:640-641 `voice.suspend`, 648-649 `voice.resume`; voice.py:323-338).
- **Still posture.** A STILL block sets the gesture to None and a SAY block sets it to "speak" (live.py:778). Only
  `reason == "risk"` (the help line) is STILL (live.py:663).
- **playing / idle** go to the server for SAY sound too (live.py:775-787). The server uses them for `after_opening`
  and its own waits.
- **A server `flush` drops queued SAY items too** (live.py:485-486, 789-801; `Player.flush` live.py:175-180). So
  does `stop()` without a line (live.py:431-432). [checked: corrected] A flush does not cancel a `_say` task still
  synthesising: its later chunks are queued after the flush (live.py:670-675). Today a flush therefore cuts only the
  chunks already queued. A kept WAV queued as one item would be dropped whole.
- **Final drain.** After the server's `end` or `fallback`, a dropped socket, or `stop(say=...)`, whatever is queued
  plays for up to `DRAIN_LIMIT_SECONDS = 30` (live.py:75, 383-386, 803-812). This is how the help line and the
  robot's last line get heard.

### 1b. Line by line

| Line | Where it is triggered (file:line) | Where the text comes from | How it is synthesised | How it is played |
|---|---|---|---|---|
| **Check-in greeting (ANNOUNCE)** for a `reason == "checkin"` task, including reminder check-ins (server `reminders.py:328` queues them as "checkin") | session.py:295-297 `_announce` -> `_say(CHECKIN_GREETING[...])` | A local constant in Simplified: `CHECKIN_GREETING` session.py:74, `{"zh-TW": "您好！我来找您聊聊天。", "en": "Hello! I came to have a chat with you."}`. The server has no Traditional original. | Matcha on the fly: `Speaker.say` (speech.py:145-201). Chunks are synthesised ahead and each is written to a temporary WAV at Matcha's rate. | `robot.play_clip(path)`, blocking, per chunk (speech.py:193). The state is ANNOUNCE, so `_say` takes the plain branch (session.py:1058-1059): `_speak` holds/releases the listener (session.py:1017-1025) and `voice.note_spoken(text)` (session.py:1056-1057). The live audio has not started yet. |
| ANNOUNCE for a dose task (not a live line, listed for completeness) | session.py:299 | Manifest `reminder` (missed_retry) or `wake_greeting` | A pre-rendered Matcha WAV from the deploy | `ClipPlayer.play` -> `robot.play_clip` (clips.py:73-79) |
| **Opening** (companion relay, bridge >= 0.9.0) | live.py:475-484: `ready.speech_text` -> `_start_say(opening, "opening")` | Server: `ready {conversation_id, speech_text}` (server live_session.py:780-781), built from `memory.opening_line` (server memory.py:148-152) and converted to Simplified with `conversation.speech_text` | Matcha on the fly (SAY path, 1a) | `player.add_say(..., still=False)`, kind SAY, "speak" gesture |
| **Reminder lead-in** (a reminder's check-in) | The same `ready` handler, live.py:480-484 | The server sets `self.opening = REMINDER_OPENING[...]` (server live_session.py:724-727, 175-176: "{name}，我來提醒您一件事。") and sends it as `ready.speech_text` | Matcha on the fly (SAY path) | As the opening. **The reminder that follows** is sent by the server as **binary marin PCM16 16 kHz** (`speak()`, server live_session.py:793-835 [checked: corrected: was 793-829]; `_say_reminder` 837-848; `reminders.audio` -> `to_pcm16k`, reminders.py:404-453). The robot treats it as GPT-Live audio: `player.add_live` (live.py:643), which goes through the jitter buffer, has no SAY tail, and is **dropped if a SAY is active** (live.py:639-643). The server waits `after_opening` before sending it. So today the patient hears a Matcha greeting, a Matcha lead-in, then the marin reminder. |
| **Reminder that fires during a running live chat** [checked: corrected: missing from the map] | Server reminders.py:284-299 `fire` -> `find_relay` -> `relay.speak(text, pcm, reason="reminder")`, without `after_opening` | The reminder row's own text (Traditional), rendered by `reminders.audio` (reminders.py:426-453: `med_clips.render` in the live voice, then `to_pcm16k`) | Server, gpt-4o-mini-tts in the live voice (marin) | Binary PCM16 16 kHz on the live socket, `player.add_live` (live.py:643), as the reminder above. No lead-in and no Matcha, so no voice jump. The drop window in 1a applies. |
| **Repeat line** | live.py:487-488: `say {speech_text, reason: "repeat"}` | Server `REPEAT_LINES` (server live_session.py:291-292), sent at live_session.py:1311-1321 | Matcha on the fly (SAY path) | SAY, "speak" gesture |
| **Help line (risk)** | live.py:487-488: `say {speech_text, reason: "risk"}`, then the server's `end {risk: true}` (live.py:512-514), played out by the drain | Server `conversation.HELPLINE` (server conversation.py:77-80), sent at live_session.py:2120-2122 in `_end_blocked` (2113-2122), after the block's `flush` and `_finish("end", "risk")` (2108-2111) [checked: corrected: was 2086-2095] | Matcha on the fly (SAY path) | **STILL** (live.py:663, 778). The microphone stays silent for the tail. The drain plays it whole (live.py:803-812). The session never asks for its own ending once `chat.closing` is set (session.py:712-713). |
| **Rest line** (nobody heard or seen for `REST_AFTER_SECONDS = 600`, session.py:59) | session.py:714-720: `chat.stop("patient_left", say=self._line(REST_LINE))` -> live.py:345-358 -> live.py:426-434 `_start_say(self._last_line, self._stop_reason)` | Local constant `REST_LINE` session.py:75 (Simplified) | Matcha on the fly (SAY path) | SAY, queued after what is playing. `end` goes to the server at once (live.py:433). `run()` returns after the drain. In today's text chat the same constant goes through `_end_companion` -> `_say` -> `Speaker.say` (session.py:600-601, 821-828). |
| **Medicine line** (heartbeat `task_due`, runner.py:670-672 -> `medicine_due` session.py:222-231) | session.py:714-720 `chat.stop("medicine", say=self._line(MEDICINE_LINE))`. It waits while the patient speaks (`_still_talking`, session.py:715-716, 792-796) | Local constant `MEDICINE_LINE` session.py:76 (Simplified) | Matcha on the fly (SAY path) | As the rest line. Text chat: session.py:594-596 -> `_end_companion`. |
| **Wind-down** after the live chat | session.py:920-928 `_wind_down` -> `_play("wind_down")` (see section 3) | Manifest `wind_down` "這次就到這裡，請多保重！" (`clips/manifest.json`). The deploy speaks it in Simplified (tools/deploy_to_robot.py:57). | A **pre-rendered Matcha WAV** from the deploy (section 2c) | `ClipPlayer.play` -> `robot.play_clip(path)`, blocking (clips.py:73-79, media.py:280-306). It runs after `stop_live_audio` and waits out `LIVE_TAIL_SECONDS = 0.3` (media.py:45, 372-377, 293-294). |
| **「嗯」 (ack)** - text chat only | session.py:833-855 `_answer_pause` -> `_start_ack` -> `clips.start(ACK_CLIP)` (ACK_CLIP session.py:77) | Manifest `ack` | Pre-rendered Matcha WAV, Whisper-checked so that its echo is recognised (tools/deploy_to_robot.py:59-61, 109-111, 137) | `ClipPlayer.start` -> `robot.play_clip(path, wait=False)` (clips.py:81-89). No listener hold. Its echo is handled by `voice.note_ack(seconds)` (voice.py:372-385). Never in a live chat: the listener is suspended. |
| **Thinking phrases** - text chat only | voice.py:398-421 `_maybe_think`/`_think_aloud` -> `clips.think_aloud` (clips.py:91-94), wired at `medcare_reachy/service.py:97` [checked: corrected: path] | Manifest `thinking_1..3` (variants group `thinking`) | Pre-rendered Matcha WAVs at speeds 1.0/1.1/0.9 (tools/deploy_to_robot.py:65-67, 73) | `ClipPlayer.start` (wait=False). The echo window comes from `note_filler` (voice.py:387-396). `voice.suspend` clears `_think_at` (voice.py:329), so a thinking phrase never plays during a live chat. |
| Text-chat opening / replies / help line (the fallback chat) | session.py:603-617 `_open_chat` (POST /conversations `speech_text`), session.py:587 reply, `lively=not risk` | Server | Matcha on the fly, `Speaker.say` | `play_clip` per chunk, with hold/release and `note_spoken`. [checked: corrected] This also follows a live chat directly: a fallback without a conversation id (every fallback the robot decides itself: `no_audio`, `connect_failed`, a socket close or its close code, `robot_error`, `no_answer` at live.py:370, 377, 444, 460, 464, and `detector_failed`, `camera_failed`, `error` at session.py:706, 742-744) makes `_live_checkin` call `_open_chat` (session.py:656-659), so Reachy says the text chat's opening in Matcha right after the live part. A server `fallback` always names the conversation (live.py:509), and then Reachy says nothing new (session.py:656-657). |
| Dose refusal explanation | session.py:524-538 `_explain` -> `_say(exc.speech_text)` | Server `DoseRefused.speech_text` (app_client.py:56-59) | Matcha on the fly | `Speaker.say` |
| **Medicine prompt** (dose flow) | session.py:378-384: `_fetch_med_clip`, then `_speak(self.clips.play_med_prompt, med_id, position)`, then `say_when_done` | A server clip (already marin when the live voice is marin), else `med_<id>.wav`, else `med_prompt_generic` | The server clip is rendered by gpt-4o-mini-tts. The other two are Matcha WAVs. | `ClipPlayer.play` -> `play_clip`, blocking. **It is followed at once by the Matcha `say_when_done` clip** (session.py:382-383), so the voice already jumps inside the dose flow today. [checked: corrected] Only when `_may_listen()` holds (a listener, and the patient's microphone consent: session.py:1028-1029); otherwise the prompt is followed by nothing. |
| Other dose-flow manifest clips (all pre-rendered Matcha, played by `ClipPlayer.play` -> `play_clip` with listener hold) | `searching` session.py:339-341; `thanks` 441; `help` (the dose watch timeout, not the helpline) 460-462; `confirm_with_caregiver` 501; `already_taken` 916-918; `waiting_for_tablet` 933-935; `waiting_for_patient` 1141-1143 | Manifest | Deploy-rendered Matcha | `play_clip` |

### 1c. Message fields the robot reads today [checked: corrected: missing from the map]

- **`ready`**: only `conversation_id` and `speech_text` (live.py:475-484). **`say`**: only `speech_text` and `reason`
  (live.py:487-488). Any other field is ignored, so a new `clip` field does not break a 0.15.0 robot. The server
  already branches on `start.bridge_version` (`companion_robot`, server live_session.py:369; `COMPANION_VERSION`
  live_session.py:219), so it can send `clip` only to 0.16.0 and later.
- **`say.reason`** values the server sends: only `repeat` (live_session.py:1320-1321) and `risk` (2121-2122). The robot
  itself uses `opening` (live.py:484) and the stop reason (`patient_left`, `medicine`; live.py:429). Only `risk` is
  said still (live.py:663).
- **Heartbeat answer**: only `stop_all`, `microphone` and `task_due` (runner.py:664-672). Other keys are ignored.
- **Task payload**: `_refresh_doses` copies only `auto_record`, `microphone`, `checkin`, `live` and `expires_at` from
  `tasks_current` (session.py:1152-1156). A new task field (for example line keys) is read only from the leased task
  unless it is added there.

The `say` docstring at live.py:13-15 ("Reachy says it with its own Matcha voice") and the constants' comment at
session.py:73 ("Spoken (not prerecorded) lines, already in the voice's Simplified characters") describe today's
behaviour and will need rewording.

---

## 2. Medicine clips today: fetch, key, cache, and the WAV format the player needs

### 2a. Fetch and key

- **Trigger.** `_med_prompt` calls `_fetch_med_clip(dose, position)` right before the prompt (session.py:378-381). The
  position is `self.index + 1`.
- **Fetch** (session.py:386-409). It is skipped when there is no `med_id` or the app has no `med_clip`
  (session.py:393-394). It reads the kept key with `clips.med_clip_key(med_id, position)` (session.py:396) and calls
  `app.med_clip(med_id, position, language, key)` under `asyncio.wait_for(..., MED_CLIP_FETCH_SECONDS = 20)`
  (session.py:55, 397-398). Any exception, timeout included, is logged and the prompt goes on (session.py:399-401).
  `None` or `"unchanged"` keeps what is there (session.py:402-403). Otherwise it calls
  `run_blocking(clips.save_med_clip, ...)`, and a save failure is only logged (session.py:404-409).
- **HTTP** (app_client.py:180-194). The request is `GET /api/device/clips/med/{med_id}?position=&language=`, with
  `If-None-Match: "<key>"` when a key is kept (app_client.py:184) and a 40 s timeout. 304 gives `"unchanged"`.
  Non-200 or an empty body gives `None`. 200 gives `(bytes, ETag stripped of quotes)`. An `httpx.HTTPError` gives
  `None`. It uses `self._http.get` directly, not `_request`, so it **never raises and never starts an outage**.
- **Server key and 304** (for the server map). The key is `sha256("gpt-4o-mini-tts|voice|text")[:20]` (server
  med_clips.py:65-67). Instructions are not part of it. The route `api_device.py:247-260` always answers 200 with the
  bytes and **never compares If-None-Match**, even though its docstring says it does. So today every prompt
  downloads the clip again and rewrites it on disk. The plan's "fix the missing 304" is confirmed.

### 2b. Cache on the robot

- **Folder.** `ClipPlayer.dir = <CLIPS_DIR>/<language>` (clips.py:33). `CLIPS_DIR` is
  `medcare_reachy/bridge/clips` (`medcare_reachy/service.py:21, 93` [checked: corrected: the file is not under
  `bridge/`]). On the robot `clips/zh-TW` is a symlink to
  `~/.medcare_reachy/clips/zh-TW` (tools/deploy_to_robot.py:38, 344). So the server clips sit beside the deploy's
  Matcha WAVs. `en` has no deploy WAVs at all. [checked: corrected] The evidence is the deploy, which renders and links
  only zh-TW (tools/deploy_to_robot.py:38, 91, 344); `test_english_has_no_clips_yet_so_nothing_is_said` only shows that
  an empty folder plays nothing. Two consequences: in English, `clips/en` is a plain folder inside the installed
  package (no symlink), so a clip kept there is lost whenever the package is replaced; and the English `wind_down`
  is silent today (its WAV is missing, `ClipPlayer._file` returns None).
- **Files.** `med_<id>_<position>.wav` plus a key sidecar `med_<id>_<position>.json` = `{"key": "<key>"}`.
  - The key is read by `med_clip_key`, which returns None on OSError or ValueError (clips.py:104-110).
  - The clip is written by `save_med_clip`: it writes `.part`, then `replace`s it, then writes the sidecar and drops
    the id from `missing` (clips.py:112-120).
  - The WAV is not validated before it is kept.
- **Lookup order.** `play_med_prompt` tries `med_<id>_<n>`, then `med_<id>`, then `med_prompt_generic`
  (clips.py:96-102).
- **Id whitelist.** `ClipPlayer._file` **raises KeyError for any id that is neither in the manifest nor starts with
  `med_`** (clips.py:45-46; pinned by test_clips_media_config.py::test_player_plays_existing_wav_and_counts_missing).
  A `line_<id>` cache needs its own lookup or a widened whitelist. [checked: corrected] If the whitelist is widened, a
  `line_` id whose WAV is absent is also added to `missing` by `_file` (clips.py:48-52), which the heartbeat reports as
  `missing_clips` (runner.py:650).
- **Deploy tool.** `wav_clips` lists every `.wav` in that folder (tools/deploy_to_robot.py:263-267). However,
  `plan_clips` and `absent_clips` look only at manifest ids (187-198, 240-242), so extra `med_`/`line_` files are
  harmless to the deploy.
- **Heartbeat.** It reports `missing_clips = clips.missing_count` (runner.py:650). `audit()` counts only manifest
  clips (clips.py:122-129).

### 2c. WAV formats

| Consumer | Needs | Where |
|---|---|---|
| `robot.play_clip` (every clip outside a live chat) | **16-bit PCM only**: `read_wav` raises ValueError otherwise (media.py:121-130). **Any channel count** (it downmixes by averaging, media.py:128-129) and **any rate** (it resamples linearly to `media.get_output_audio_samplerate()`, which is 16 kHz on this robot per media.py:340). It pushes 1024-sample chunks and blocks for the clip's length with `wait=True`. A clip never starts before the previous one ends (`_quiet_at`). | media.py:280-306 |
| `ClipPlayer.start` | `wav_seconds` reads the frame count and rate from the header (media.py:148-150) | clips.py:81-89 |
| `Player.add_say` (live chat) | **PCM16 little-endian, mono, 16 kHz.** No header. Today's conversion from float32 at any rate: `to_pcm(resample(samples, rate, RATE))` (live.py:674). A kept WAV would go `read_wav` -> `resample(..., RATE)` -> `to_pcm`; all three helpers exist. | live.py:56, 115-117, 163-165 |
| Live speaker | float32 mono at `LIVE_RATE = 16000` | media.py:42, 330-341 |

- **Deploy Matcha WAVs.** Mono, 16-bit, at Matcha's own rate (`audio.sample_rate`; the vocoder is
  `vocos-22khz-univ.onnx`, speech.py:23). Leading silence is trimmed to 10 ms at about -50 dBFS
  (tools/deploy_to_robot.py:149-159). The manifest note says "mono, 16-bit".
- **Server medicine clips.** `normalize_wav` keeps the API's own channels and rate and only fixes the header sizes
  (server med_clips.py:74-101). I did not verify the rate from a real file in this session. OpenAI's WAV output is
  expected to be 24 kHz mono, which the server's reminder path converts with `to_pcm16k` (reminders.py:404-419).
- **Failure risk.** A malformed or non-16-bit WAV raises inside `play_clip`. Nothing in `ClipPlayer.play` catches
  it, so it reaches `tick()`'s catch-all and the slot shuts down with `bridge_error` (session.py:278-280). Inside the
  live chat a load error in `_say` is caught (live.py:676-677) but the line is lost. The new line cache should check
  each WAV before keeping it, and on a read failure fall back to Matcha.

---

## 3. The wind-down after a live goodbye: where it is triggered

1. Server: `_finish("end", "goodbye")` (server live_session.py:1045) sends the robot `end {reason: "goodbye", risk: false}`.
   [checked: corrected] `_finish` only decides the ending (928-936); the wrap-up's `_end_chat` sends `end`
   (2221-2242, the send at 2241). The goodbye is the patient's: a `GOODBYES` word in a closed patient turn
   (`_goodbye_said`, 1945-1951). The chat ends `GOODBYE_GRACE_SECONDS = 3` (181) after GPT-Live's answer stops
   (1440-1443), or 3 s after the goodbye when GPT-Live makes no sound then and starts none (1951, 1437-1438). So the
   plan's premise "GPT-Live already said bye" holds only when GPT-Live answered; a goodbye with no answer ends in
   silence if the wind-down clip is skipped.
2. Robot: live.py:512-514 returns `LiveOutcome("ended", "goodbye", cid)`. `run()` drains what is queued, then stops
   the live audio (live.py:383-419).
3. session.py:688-745 `_run_live` returns `("ended", "goodbye", cid)`. session.py:661-663 sets
   `outcomes["live"] = "ended"` and `outcomes["checkin"] = "goodbye"`, then calls `_after_checkin()`.
4. session.py:899-905 `_after_checkin` sets `rest = self._is_checkin_task() or outcomes["checkin"] in COMPANION_ENDINGS`
   (`COMPANION_ENDINGS = ("patient_left", "medicine")`, session.py:63).
   - **Check-in task:** WIND_DOWN at once.
   - **Dose task's check-in:** POST_SLOT_OBSERVE first. After `POST_SLOT_SECONDS = 120` it goes to WIND_DOWN
     (session.py:78, 907-914), so the clip comes about 2 minutes after the goodbye.
5. **session.py:923-924**: `if self.outcomes.get("checkin") not in COMPANION_ENDINGS: await self._play("wind_down")`.
   This is the only gate today. Then the task is marked `completed` and `robot.sleep` runs (session.py:926-928).

Today's wind-down rule:

- The clip is skipped only after `patient_left` and `medicine`, because Reachy has just said why it stops.
- After a live chat it **also plays** for `goodbye`, `risk` (right after the help line), `silence`, `limit`,
  `disconnected`, `error` (the server's `end` reasons, server live_session.py:945-1126) and the robot's own
  `microphone_withdrawn` (session.py:698-700 -> WIND_DOWN).
  [checked: corrected] `silence` and `limit` never end a companion chat: the server's companion branch pauses
  instead (live_session.py:1046-1060), and robot 0.15.0 always starts a companion chat (`COMPANION_VERSION` 0.9.0,
  live_session.py:219). The server's `end` reasons that can reach this robot are `goodbye` (1045), `risk` (2111,
  outside the range above), `disconnected` (945, 2294, 2302), `error` (958), `access_unverified` (1105),
  `access_withdrawn` (1114) and `stopped` (1117, the conversation ended elsewhere). The clip plays after each of them.
  `microphone_withdrawn` goes through `_after_checkin` like any ending (session.py:661-663, 904-905): WIND_DOWN at once
  for a check-in task, POST_SLOT_OBSERVE first for a dose task. The robot's `stop()` reasons do not wait for a server
  `end` (live.py:433-434).
- A `stop_all` or a shutdown goes through `_shutdown`, which plays no clip (session.py:1247-1271).
- A goodbye in today's text chat also gets the clip (session.py:590-591 -> `_close_chat` -> `_after_checkin`).

To skip it after a live goodbye only, the gate at session.py:923 can check `outcomes.get("live") == "ended"` together
with the checkin reason. Whether `risk` should also skip it is a decision the plan does not cover yet.

---

## 4. `_may_go_live` and the Matcha requirement

- **session.py:1040-1048.** The task must have `live` and `microphone`, `self.speaker is not None and
  self.speaker.available` must hold, `speech_models_dir(self.voice)` must not be None (silero_vad.onnx,
  session.py:120-126), and `self.monitor` must not be None (the companion's camera session, opened by `_watch()` just
  before, at session.py:561).
- It is checked once, on entering CHECKIN (session.py:562). Without it the session falls back to today's chat.
- `open_live_chat` (session.py:129-150) checks the live audio calls, the `live` module and the voice detector. It
  does not check the speaker.
- **`speaker.available` is the Matcha requirement.** It means all of `TTS_FILES` exist (speech.py:21-23, 119-124).
  It turns False when the engine fails to load in `preload` (speech.py:133-143).
- `_may_chat` (session.py:1034-1038) has the same speaker condition for today's chat.
- **The second gate.** `LiveChat._start_say` refuses any line when `not self.speaker.available` (live.py:658). If a
  marin clip should play even when Matcha is off, the clip branch has to come before that check. With
  `_may_go_live` still requiring Matcha (plan item 5) this cannot happen at the start of a chat. It only matters if
  Matcha's load fails mid-chat (`_safe_load` sets `available = False`).

---

## 5. Tests that pin each of these

Ran the 11 files below read-only, using `MedAiCarePlus/.venv` (Python 3.11.9, pytest 9.1.1) with
`-p no:cacheprovider` and `PYTHONDONTWRITEBYTECODE=1`. Result: **248 passed, 1 failed**. The failure is
`test_speech.py::test_say_tells_when_its_first_sound_is_about_to_play`: a timing check missed by 3 ms
(`191443.89 + 0.05 <= 191443.937`). On three reruns of that test alone it passed twice and failed once, so it is
flaky on Windows and unrelated to this work. `git status` was clean afterwards.
[checked: corrected] The 249 tests match 11 of the 14 files this section cites: `test_live`, `test_session_live`,
`test_session_checkin`, `test_session`, `test_medication_prompts`, `test_clips_media_config`, `test_live_media`,
`test_speech`, `test_thinking_fillers`, `tests/test_deploy_tool.py` and `tests/test_deploy_fillers.py`. It left out
`test_session_refusal.py` (17), `test_filler_echo.py` (94) and `test_runner.py` (45), which the section also cites.
Re-run on all 14 files with the same venv and flags: **404 passed, 1 failed**, the same timing test
(`192119.156 + 0.05 <= 192119.203`, missed by 3 ms). `git status` was clean afterwards.

### Live SAY path (tests/test_live.py, under medcare_reachy/bridge/tests/)
- `test_live.py::test_reachys_own_line_plays_at_once_and_after_what_came_before_it` (150): `add_say` kinds, no buffering, order.
- `test_live.py::test_reachys_own_line_follows_the_voice_before_it_and_gpt_live_never_talks_over_it` (275): the risk `say`, `synthesised == chunks(HELP)`, `dropped_audio == 1`, still gesture.
- `test_live.py::test_the_microphone_sends_silence_while_reachy_says_its_own_line_and_just_after` (299): the microphone tail, `SAY_TAIL_BLOCKS`.
- `test_live.py::test_the_opening_is_said_in_reachys_own_voice_before_anything_else` (644): `ready.speech_text` gives `synthesised == chunks(OPENING)`, the microphone stays silent, no `speech` signal.
- `test_live.py::test_speech_goes_to_the_server_when_the_patient_begins_to_speak_never_on_reachys_own_sound` (666): the 0.75 s echo guard.
- `test_live.py::test_stop_with_a_last_line_lets_reachy_finish_its_sentence_then_says_the_line` (762): `stop(say=)` for medicine and rest, `synthesised == chunks(LAST_LINE)`, `end` sent at once.
- `test_live.py::test_stop_ends_the_chat_at_once_and_drops_what_is_queued` (413), `::test_a_dropped_connection_plays_what_is_queued_and_falls_back_to_a_new_chat` (354), `::test_messages_it_does_not_know_are_ignored` (522, an empty `say` makes no sound).
- The fake: `medcare_reachy/bridge/tests/fakes.py:265-294` [checked: corrected: path] `FakeSpeaker.synth` gives 10 ms of level 0.25 per character at 16 kHz, so the assertions expect `[8192] * said(text)`.

### Live chat in the session (test_session_live.py)
- `::test_a_live_check_in_runs_the_live_chat_and_ends_with_it` (99): the greeting goes through `speaker.said == [CHECKIN_GREETING]`, a goodbye leads to WIND_DOWN, outcomes `{"live": "ended", "checkin": "goodbye"}`. **It does not assert the wind_down clip either way.**
- `::test_a_dose_tasks_check_in_follows_the_same_rule` (122): a dose task goes to POST_SLOT_OBSERVE.
- `::test_a_live_chat_blocked_for_a_risk_ends_with_the_risk_reason` (151).
- `::test_a_live_companion_rests_when_nobody_is_heard_or_seen` (271): `stops == [("patient_left", REST_LINE)]`, no wind_down.
- `::test_a_due_medicine_ends_the_live_chat_with_reachys_line` (288): `stops == [("medicine", MEDICINE_LINE)]`, no wind_down.
- `::test_a_hand_to_mouth_moment_during_reachys_closing_line_is_no_news_for_family` (299).
- `::test_a_companion_ending_never_cuts_the_servers_help_line` (356): a real LiveChat. The opening and the help line are heard whole, from `synthesised`.
- The `_may_go_live` gates:
  - `::test_no_live_chat_without_reachys_own_voice_for_the_help_line` (441, Matcha)
  - `::test_no_live_chat_without_microphone_consent` (451)
  - `::test_no_live_chat_without_the_listeners_voice_detector` (403)
  - `::test_without_live_in_the_task_nothing_changes` (138)
  - `::test_the_task_read_again_before_the_doses_decides` (460)
  - `::test_a_robot_app_that_cant_hold_a_live_chat_uses_todays_chat` (432)
  - `::test_the_robot_app_holds_a_live_chat_only_with_live_audio_and_a_voice_detector` (509)
  - The `self.monitor is not None` condition has no test (searched with grep, found none).
- These assert `speaker.said == [CHECKIN_GREETING["zh-TW"]]` and will change if the greeting moves to a kept clip: lines 115, 156, 176, 284.

### Check-in and the text chat (test_session_checkin.py)
- `::test_conversation_only_task_greets_finds_the_patient_and_chats` (281): `speaker.said[0] == CHECKIN_GREETING`, no `wake_greeting`.
- `::test_reachy_rests_after_ten_minutes_with_no_voice_and_nobody_seen` (95): REST_LINE plus `voice.spoken`, no wind_down.
- `::test_a_due_medicine_ends_the_chat_with_reachys_line` (156), `::test_words_the_patient_just_said_go_to_the_server_before_the_medicine_line` (168), `::test_a_medicine_due_while_reachy_answers_waits_for_the_answer` (183).
- `::test_reachy_is_muted_while_it_speaks` (78, hold/release), `::test_the_listener_is_told_what_reachy_says` (372, `note_spoken`).
- 「嗯」: `::test_reachy_says_mm_and_starts_thinking_the_moment_the_patient_pauses` (458), `::test_a_missing_mm_clip_or_a_failing_gesture_never_breaks_the_conversation` (569).
- `::test_the_help_line_after_a_risk_is_said_still` (542, text chat).

### Dose flow and wind-down (test_session.py)
- `::test_happy_path_two_doses_recorded_then_post_slot_and_wind_down` (22; line 54 checks `played[0] == "wake_greeting"` and `played[-1] == "wind_down"`).
- `::test_missed_retry_announces_with_reminder_clip` (60), `::test_all_doses_resolved_when_found_plays_already_taken_and_winds_down` (89), `::test_search_scans_head_plays_searching_clip_and_times_out_as_not_found` (75), `::test_watch_timeout_plays_help_then_leaves_dose_pending` (200), `::test_wrong_person_while_watching_plays_waiting_clip` (270).
- `test_session_refusal.py::test_refused_dose_start_is_explained_instead_of_prompted` (29).
- **No test pins the wind_down clip after a live goodbye**, in either direction. Item 4 of the plan needs a new one, for both the check-in task and the dose task.

### Medicine clips (test_medication_prompts.py)
- `::test_the_prompt_names_each_medicine_by_its_place_in_the_slot_and_keeps_the_clip` (20).
- `::test_a_kept_clip_is_checked_by_its_key_and_a_missing_or_slow_one_leaves_the_generic_prompt` (36).
- `::test_the_clip_player_prefers_the_positions_clip_then_the_medicines_then_the_generic` (94): the sidecar JSON, and the fixture WAV is 16 kHz.
- `::test_the_app_client_fetches_a_clip_with_its_key_and_treats_everything_else_as_none` (113): If-None-Match, 304 gives "unchanged", 404 gives None.
- The fakes: `medcare_reachy/bridge/tests/fakes.py:146-176` [checked: corrected: path] (`FakeClips.med_clip_key` and `save_med_clip`), `fakes.py:356, 390-397` (`FakeApp.med_clips`, `med_clip`).

### Clip player, WAV and media
- `test_clips_media_config.py::test_player_plays_existing_wav_and_counts_missing` (60, includes the KeyError for unknown ids), `::test_player_audit_reports_all_missing_manifest_clips` (72), `::test_med_prompt_prefers_per_medication_clip` (78), `::test_read_wav_downmixes_and_resample_changes_rate` (107), `::test_manifest_has_every_clip_in_both_languages_pending_review` (36).
- `test_live_media.py::test_the_end_of_a_live_chat_never_stops_the_shared_pipeline_and_the_next_clip_waits` (83, the LIVE_TAIL before the wind-down), `::test_the_live_chat_starts_the_speaker_once_and_pushes_its_blocks_as_they_are` (48), `::test_blocks_are_resampled_for_a_speaker_at_another_rate` (59).
- `test_speech.py::test_synth_gives_every_chunk_as_samples_and_plays_nothing` (195), `::test_synth_without_a_voice_text_or_sound_gives_nothing` (208), `::test_say_plays_every_chunk_in_order_while_synthesising_ahead` (107), `::test_say_without_models_or_text_says_nothing` (154).
- Thinking phrases and 「嗯」: `test_thinking_fillers.py::test_the_manifest_s_thinking_phrases` (46), `::test_the_ack_is_the_lone_mm_again` (58), `::test_english_has_no_clips_yet_so_nothing_is_said` (115); `test_filler_echo.py::test_every_thinking_phrase_in_the_manifest_is_recognised` (66), `::test_the_phrase_follows_the_hand_over_of_an_acknowledged_turn` (115).
- Deploy: `tests/test_deploy_tool.py::test_every_clip_has_simplified_words_written_for_its_current_text` (23), `::test_render_script_on_the_robot_compiles` (61), `::test_a_deploy_fails_unless_every_planned_clip_was_written` (111); `tests/test_deploy_fillers.py::test_any_clip_left_without_a_wav_fails_the_deploy` (55), `::test_the_render_script_keeps_only_acknowledgements_whose_echo_the_app_recognises` (114).
- Heartbeat: `test_runner.py::test_a_due_medicine_task_reaches_the_running_slot` (560), `::test_heartbeat_payload_and_stop_all` (505). The response handling where line keys could be read is runner.py:664-672 (a heartbeat every 10 s, runner.py:36).

---

## Findings that change the plan

1. **The swap point is one function.** `LiveChat._start_say` and `_say` (live.py:656-679) carry the opening, the
   reminder lead-in, repeat, help, rest and medicine lines. A kept WAV becomes `read_wav` -> `resample(..., 16000)` ->
   `to_pcm` -> `player.add_say(pcm, still)`. The read should happen in `run_blocking`, inside the `_saying` window, so
   that GPT-Live audio is still dropped and the microphone stays silent. All the SAY rules stay as they are. The
   greeting (session.py:295-297) and the wind-down (session.py:923-924) are outside the chat and would use
   `robot.play_clip` on the kept WAV.
2. **The robot's own three lines are Simplified only.** `CHECKIN_GREETING`, `REST_LINE` and `MEDICINE_LINE`
   (session.py:74-76) have no Traditional originals anywhere. The server needs Traditional text for these to key and
   render marin copies (plan server item 1). The opening, repeat and help lines are owned by the server.
   [checked: corrected] No Traditional original exists in code. The docs do carry Traditional forms of two of them:
   REST_LINE 「我先休息一下，有需要再叫我喔。」 and MEDICINE_LINE 「該吃藥囉，我們等一下再聊。」 (robot README.md:175-177,
   server CLAUDE.md:128-129). CHECKIN_GREETING has none. Also, in English today the Chinese zh-baker Matcha voice (the
   only model, speech.py:21-23) reads the English constants.
3. **The server's marin reminder is not a SAY.** It arrives as GPT-Live audio (`add_live`) and is dropped while a
   SAY is active (live.py:639-643). Once the lead-in is a marin clip, they will match, and nothing changes on the
   robot for the reminder itself.
4. **A bad WAV ends the slot.** A malformed kept WAV raises from `play_clip` and shuts the slot down
   (session.py:278-280). The line cache must check WAVs before keeping them and catch read errors so it can fall back
   to Matcha.
5. **`ClipPlayer` refuses non-`med_` ids.** It raises KeyError for any id that is neither in the manifest nor starts
   with `med_` (clips.py:45-46), so `line_*` needs its own path.
6. **The 304 is missing on the server.** The robot already sends If-None-Match for medicine clips (app_client.py:184),
   but the server route never returns 304 (server api_device.py:247-260). Today every prompt downloads and rewrites
   its clip.
7. **The wind-down gate is broader than "goodbye".** The clip plays after every live ending except `patient_left` and
   `medicine`, including `risk` right after the help line. For a dose task it comes 120 s later, after
   POST_SLOT_OBSERVE. The only gate is session.py:923, and no test pins the live-goodbye case.
   [checked: corrected] With this robot the server's `silence` and `limit` endings do not happen (section 3), but
   `access_withdrawn`, `access_unverified` and `stopped` do, and the clip plays after them too. A live `goodbye` is not
   always preceded by a GPT-Live bye (section 3, step 1).
8. **The dose flow already jumps voices, outside v1.** A marin medicine prompt is followed at once by the Matcha
   `say_when_done` clip (session.py:380-383), [checked: corrected] when `_may_listen()` holds (session.py:382,
   1028-1029).
9. [checked: corrected: missing from the map] **A reminder can be lost inside a live chat.** A reminder that fires
   while the robot is still synthesising a line's first chunk (the opening above all) passes the server's busy check
   and is dropped on the robot, while the server marks it `fired` (section 1a). Kept marin clips narrow the window.
   A reminder that fires mid-chat is already marin and has no Matcha lead-in (section 1b).
