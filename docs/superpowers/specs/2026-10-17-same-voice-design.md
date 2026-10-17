# Same voice: Reachy's own lines in GPT-Live's voice

Date: 17 Oct 2026. Repos: server `MedAiCarePlus` (branch to be made from `agent-tools-fixes` once it is merged, or
on top of it) and robot app `medcare-reachy-bridge` (branch from `feature/merged-live-emotion` `437b136`, 0.15.0 →
0.16.0). Read-only maps of today's code, with file:line for everything below (line numbers as of 17 Oct 2026):
`2026-10-17-same-voice-map-robot.md` and `2026-10-17-same-voice-map-server.md` beside this file.

## Intent and why

In a live chat, GPT-Live answers in OpenAI's "marin" voice, while Reachy's own lines (the check-in greeting, the
opening with the patient's name, the reminder lead-in, the repeat line, the help line, the rest and medicine lines,
the wind-down) come from the robot's local Matcha voice. The user hears the jump at every one of them (17 Oct 2026)
and asked for the same voice. The server already renders medicine prompts and reminders with OpenAI's speech API
(`gpt-4o-mini-tts`, which has "marin": OpenAI text-to-speech guide) in the live voice. This design extends that to
Reachy's own lines: the server renders each line once in the live voice, the robot keeps a copy and plays it, and
Matcha stays as the offline fallback.

## The user decided

- Reachy's own lines around a live chat sound like GPT-Live (the same voice).
- Matcha remains only as the fallback when no marin copy is at hand.
- No wind-down clip after a live chat that ended with the patient's goodbye.

## Claude's calls (stated to the user, not decided by them)

- **v1 scope is the lines around a live chat:** `checkin_greeting`, `opening`, `reminder_lead`, `repeat`,
  `helpline`, `rest`, `medicine`, `wind_down`. The dose-flow manifest clips, the text chat's replies and openings,
  「嗯」 and the thinking phrases stay Matcha (their echo handling is tuned to Matcha; the text chat is the fallback
  path anyway). So after a `repeat` line (which the server always follows with a fallback to the text chat) the
  next replies are still Matcha: the jump moves there in v1 instead of going away.
- **Marin only while a live voice target exists** (`LIVE_VOICE_TEST=1` and a passing Check), the same gate medicine
  prompts and reminders have today. Without it nothing changes, and a robot that kept marin copies drops them.
- **The help line keeps its rules:** said still, at once, whole, and always speakable: `_may_go_live` keeps
  requiring Matcha. A marin help line is a bonus, never a condition.
- **The wind-down after a `risk` ending is unchanged** (it still plays, from the marin copy when kept). Only a live
  `goodbye` skips it.
- **The robot never guesses a line's text.** For the lines whose text the server sends (`opening`, `reminder_lead`,
  `repeat`, `helpline`), the server also sends the clip id and key, and the robot plays its kept copy only when the
  key matches; otherwise Matcha now, and it fetches the new copy for next time. The robot-owned fixed lines
  (`checkin_greeting`, `rest`, `medicine`, `wind_down`) play the kept copy for that id; they are refreshed by key
  (If-None-Match) at start and at each task.
- **Robot app 0.15.0 keeps working** with the new server: it ignores the new fields. The server sends them to every
  companion robot (no version branch), because the fields are harmless.
- **The kept greeting and wind-down play only around a live chat.** The greeting at ANNOUNCE uses the kept marin
  copy only when the task says `live` (the robot has the flag at ANNOUNCE); a text-chat check-in keeps its Matcha
  greeting. The wind-down uses the kept copy only when a live chat ran in this task (`outcomes["live"]` is set);
  a dose-only task keeps the manifest WAV. So no marin line lands inside a flow that v1 leaves on Matcha, and a
  task leased just after the live voice was switched off (its `live` flag is then false) cannot play an old copy.
- **Per-patient lines need check-in consent.** `opening` and `reminder_lead` carry the call name, which can come
  from a memory note. They are rendered (at enqueue, on a name change, or on the route) and served only while the
  patient's check-in consent is current (`checkin_allowed`, the same gate as a live chat); otherwise the route
  answers 404 and the robot drops its copy. The fixed lines hold no patient data and need only the device's usual
  consent (as the medicine clip route).
- **After a voice change, a fixed line may play the old voice once.** The fixed lines (`rest`, `medicine`,
  `checkin_greeting`, `wind_down`) are refreshed in the background by key, so the first task after the live voice
  changes can say one of them in the previous voice. Accepted: it happens only on a test installation after a
  manual voice change, and the alternative (waiting for the refresh before the greeting) would delay every task.

## Goals

1. In a live check-in on a robot with a warm cache, every Reachy line in the v1 list is heard in the live voice:
   greeting → opening (or reminder lead-in) → GPT-Live → repeat / help / rest / medicine line → wind-down (unless
   goodbye).
2. The first chat after a change (a new call name, a new voice) degrades to Matcha for that line only, never to
   silence, and the next chat has the new copy.
3. Every SAY rule of the live chat is unchanged: no GPT-Live audio mixed into Reachy's line, the microphone muted
   for the line plus its tail, still posture for the help line, `playing`/`idle` reported to the server, the drain
   at the end.
4. A bad or missing copy never ends a task or a chat: it falls back to Matcha (or the manifest WAV) and logs once.
5. No render inside a robot's wait when it can be avoided: fixed lines are rendered at startup, per-patient lines
   when a check-in task is queued and when the call name changes.

## Non-goals (v1)

- The dose-flow clips (`wake_greeting`, `searching`, `thanks`, `say_when_done`, ...), the text chat's lines, 「嗯」,
  the thinking phrases, the dose refusal explanation.
- Changing what any line says, or adding new lines.
- Production use: the live voice is test-only (Phase 2b consent is not done); `APP_ENV=prod` still refuses
  `LIVE_VOICE_TEST`.
- A new deploy of Matcha clips; the deploy tool and the manifest are untouched.
- Deleting old clip files on the server (medicine clips are never deleted today either).

## Anti-goals (a failure even if it works)

- A line played with the wrong patient's name, or the old name after a change.
- A help line that is late, cut, interrupted by GPT-Live audio, or lost because the marin copy failed to load.
- A line that plays in the wrong voice when the live voice is switched off (marin copies must be dropped on 404).
- Reachy saying nothing where it said something before (for example an English wind-down was silent before; that
  may stay silent, but a Chinese line that had Matcha must still have Matcha as the floor).
- Keys, tokens or the patient's name in logs.

## Constraints

- Server: FastAPI + asyncpg, tests `.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider` must stay green;
  `CLAUDE.md`'s live voice section is checked by `tests/test_live_voice_docs.py` (every backticked name must exist in
  the server code; `NAME = value` spans must match; documented `/api/device/...` routes must exist; robot-only names
  from `medcare-reachy-bridge` must not be backticked there).
- Robot: Python 3.12 on the Reachy Mini, tests `pytest` in the robot repo (737 passed, 6 skipped at 437b136);
  `Player.add_say` takes PCM16 mono 16 kHz; `robot.play_clip` takes 16-bit PCM WAV at any rate or channel count.
- The speech API: `POST /v1/audio/speech`, `gpt-4o-mini-tts`, `response_format: wav` (24 kHz mono in practice),
  `instructions` supported (OpenAI text-to-speech guide). The existing `med_clips.render` is the one place that
  calls it.
- The existing keys and files must not change meaning: `med_clips.clip_key(text, voice)` and the medicine clip file
  names stay as they are (the robot has them cached).
- Simple English in docs and messages; plain Traditional Chinese for the zh-TW texts.
- Never push or merge; commits on a feature branch only.

## The design

### Server

**`app/services/voice_lines.py`** (new; reuses `med_clips.render` and the live target gate).

- Line texts, Traditional and English, in one table `LINE_TEXTS[line_id][language]`:
  - `checkin_greeting`: 「您好！我來找您聊聊天。」 / "Hello! I came to have a chat with you."
  - `repeat`: `live_session.REPEAT_LINES` (the same constant, imported, not copied)
  - `helpline`: `conversation.HELPLINE` (same constant). The text given to the speech API writes the phone numbers
    so they are read digit by digit (119 → 一一九, 1925 → 一九二五 in zh-TW; "one one nine" style in English is the
    builder's call after listening). The robot still receives the original `speech_text`.
  - `rest`: 「我先休息一下，有需要再叫我喔。」 / "I'll rest for a bit. Call me if you need anything."
  - `medicine`: 「該吃藥囉，我們等一下再聊。」 / "It's time for your medicine. Let's talk again in a little while."
  - `wind_down`: 「這次就到這裡，請多保重！」 / "That's all for now. Take care!"
  - Per patient: `opening` = `memory.opening_line(language, name)`; `reminder_lead` = `REMINDER_OPENING` /
    `REMINDER_OPENING_NONAME` (same constants).
  The English rest and medicine lines are new words (the robot's English constants are the builder's reference:
  `bridge/session.py:75-76`); the robot's own Matcha texts are not changed.
- `instructions_for(line_id)`: companion lines get a warm, calm, slow instruction (same spirit as `med_clips`);
  `helpline` gets a calm, clear one that reads numbers digit by digit. Exact wording is the builder's; it is part of
  the key.
- `line_key(text, voice, instructions)`: `sha256(f"{med_clips.MODEL}|{voice}|{instructions}|{text}")[:20]`. (A
  different formula from `clip_key` on purpose: medicine keys must not change.)
- `to_robot_wav(wav_bytes) -> bytes`: decode (any PCM16 rate/channels, via `normalize_wav` first), mix to mono,
  resample to 16 kHz (`scipy.signal.resample_poly`, as `reminders.to_pcm16k` does), trim leading silence to about
  10 ms below about −50 dBFS (as the deploy tool trims Matcha), write a PCM16 mono 16 kHz WAV.
- `render_line(text, voice, api_key, instructions)`: `med_clips.render` gains an `instructions` argument with the
  old constant as default (existing fakes with three arguments keep working), then `to_robot_wav`.
- Storage: fixed lines at `<MED_CLIP_DIR>/lines/<language>/<line_id>_<key>.wav`; per-patient lines at
  `<MED_CLIP_DIR>/<u_id>/line_<line_id>_<key>.wav`. `.part` then rename, one lock per key, like `med_clips.clip`.
- `line(u_id, line_id, language) -> (bytes, key) | None`: `None` without a live target, for an unknown id, or for
  a per-patient id without current check-in consent (`checkin_allowed`); the kept file when present; otherwise
  render now (bounded by `med_clips.TIMEOUT` and one retry) and keep it. A failure
  logs one WARNING with the line id, language and the HTTP status or error type (never the key or the text of a
  per-patient line), and returns `None`.
- `expected_clip(u_id, line_id, language) -> {"id", "key"} | None`: the key without rendering (for `ready`/`say`).
- Render ahead:
  - fixed lines, both languages, in `startup_report.finish` after the live check (whatever the stored status), in
    a worker thread, one `[STARTUP]` stage `voice lines` (OK / DEGRADED with the ids that failed); a scheduler job
    every 30 min re-tries only what is missing while a live target exists;
  - `opening` and `reminder_lead` for the patient when a check-in task is queued (`enqueue_reachy_task`, in the
    background like `reminders._render_ahead`) and when the call name changes (`PATCH /api/reachy/settings`, a
    memory `name` entry written or deleted, memory consent changed).
  - `ready`/`say` never wait for a render.

**Routes** (`app/routers/api_device.py`, device port, device token, same consent as the medicine clip).

- `GET /api/device/clips/line/{line_id}?language=`: 200 `audio/wav` with `ETag: "<key>"` and `Cache-Control:
  private`; **304** when `If-None-Match` equals the key (checked before any render); 404 when there is no live
  target, the id is unknown, or the render failed; 422 for a bad language value is not needed (`language_of` folds).
- `GET /api/device/clips/med/{med_id}`: gains the same 304 (the docstring already promises it).

**Relay messages** (`app/services/live_session.py`), companion robots only:

- `ready` gains `clip: {"id": "opening" | "reminder_lead", "key": ...}` when `expected_clip` gives one.
- `say` gains `clip: {"id": "repeat" | "helpline", "key": ...}` likewise.
- Nothing else changes: `speech_text` stays (fallback and the stored turn), `end` reasons stay, the reminder's
  marin PCM through `speak()` stays.

**Docs:** `CLAUDE.md`'s live voice section documents the `clip` field, the line route and the lines table;
`HANDOFF.md` notes the robot app 0.16.0 requirement for hearing them.

### Robot (medcare-reachy-bridge 0.16.0)

**`medcare_reachy/bridge/lines.py`** (new): `LineCache(dir, app_client, language)`.

- Files: `<LINES_DIR>/<language>/<line_id>.wav` plus `<line_id>.json` `{"key": ...}`. `LINES_DIR` defaults to
  `~/.medcare_reachy/lines` (the app's own folder, so it survives a reinstall; not the deploy's `clips` folder).
- `fetch(line_id)`: `GET /api/device/clips/line/{id}?language=` with `If-None-Match` from the sidecar; 304 keeps;
  200 validates the body with `media.read_wav` **before** keeping it (`.part` then replace, then the sidecar); 404
  deletes the kept copy and sidecar; anything else (error, timeout, bad WAV) is logged once and keeps what is there.
  It never raises and never starts an outage (like `app_client.med_clip`).
- `refresh(ids)`: fetches in the background, one at a time, with the same 20 s budget per fetch as medicine clips.
  Called at service start, at each task lease (before ANNOUNCE, without blocking it), and for one id after a key
  mismatch in a live chat.
- `kept(line_id, key=None) -> Path | None`: the WAV path when it exists and (if `key` is given) the sidecar key
  matches.
- The `AppClient` gains `line_clip(line_id, language, key)` beside `med_clip`.

**Live chat** (`bridge/live.py`):

- `_start_say(text, reason, clip=None)`: when `clip` names an id and `lines.kept(id, key)` exists, start `_say_clip`
  instead of `_say`: in `run_blocking`, `read_wav` → `resample(..., RATE)` → `to_pcm`, queued with `player.add_say`
  in pieces of about one second (so a server `flush` behaves as it does for Matcha chunks), inside the same
  `_saying` window (GPT-Live audio dropped, microphone muted, tail). A read failure logs once and falls back to the
  Matcha `_say` for that line. When the kept copy is missing or its key differs: Matcha now, and `lines.refresh([id])`.
  The clip branch comes **before** the `speaker.available` check.
- `ready` and `say` handlers pass `message.get("clip")`.
- `stop(reason, say=text, clip="rest" | "medicine")`: the session passes the id; the kept copy (no key check) or
  Matcha.

**Session** (`bridge/session.py`):

- ANNOUNCE for a check-in task whose payload says `live`: the kept `checkin_greeting` through `robot.play_clip`
  (with the same listener hold/release and `voice.note_spoken` as `_say`), else Matcha as today. A task without
  `live` keeps the Matcha greeting.
- `_wind_down`: skip the clip when `outcomes.get("live") == "ended" and outcomes.get("checkin") == "goodbye"`;
  when a live chat ran in this task (`outcomes.get("live")` is set) use the kept `wind_down` through
  `robot.play_clip`; otherwise (and whenever the copy is missing) the manifest WAV as today.
- Rest and medicine: `chat.stop(..., clip="rest" / "medicine")`.
- `_may_go_live` unchanged.
- A failure in `play_clip` on a kept line is caught, logged once, and falls back (it must never reach `tick()`'s
  catch-all).

**Version and docs:** `__version__ = "0.16.0"` and `pyproject.toml`; README's live chat section names the kept
lines and the fallback; the heartbeat is unchanged.

### Tests to update or add

Robot: `test_live.py` (synthesised chunks vs clip pieces), `test_session_live.py` (greeting through the kept clip,
the wind-down after a goodbye, the 2-minute dose-task case), `test_session_checkin.py`, `test_medication_prompts.py`
(the client's `line_clip`), new `test_lines.py` (fetch, 304, 404 deletes, bad WAV refused, key mismatch).
Server: `test_live_safety.py` and `test_live_companion.py` (the `SAY` and `ready` dicts gain `clip`),
`test_live_speak.py`, `test_med_clips.py` (`render` with instructions; the 304 on `/clips/med`), new
`test_voice_lines.py` (keys, `to_robot_wav` rate/mono/trim, storage, render-ahead, the line route and its 304/404,
the `[STARTUP]` stage), `test_reachy_tasks.py` (render-ahead on enqueue), `test_live_voice_docs.py` (docs).

### Before release (by ear, with the user)

Listen to the marin help line (the numbers), the opening with the name, and one full check-in on the robot; compare
the voices. Then decide whether the dose-flow clips should follow (v2).

## Left to the builder

- The exact instruction strings for the speech API, and the digit spelling of the help line numbers.
- The trim threshold and window (copy the deploy tool's values).
- The `LINES_DIR` setting name and how `service.py` wires `LineCache` into the session and the live chat.
- Whether `refresh` at task lease fetches all ids or only the ones for the task's kind (both are fine).
- Log wording, test names, and the order of commits.
