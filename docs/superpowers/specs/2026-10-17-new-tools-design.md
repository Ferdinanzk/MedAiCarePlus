# New GPT-Live agent tools: go_to_sleep, volume_control, head_tracking, robot_status, forget, move_head, sweep_look, stop_emotion

Date: 17 Oct 2026. Repos: server `MedAiCarePlus` (built on branch `same-voice`, worktree
`C:\Reachy_Mini\worktrees\MedAiCarePlus-same-voice`, read today at `c49f4ff`) and robot app `medcare-reachy-bridge`
(built on its `same-voice` branch, read today at `6d5bed2`; 0.16.0 → **0.17.0**).

**Brief:** `2026-10-17-new-tools-brief.md` beside this file (the rulings this design started from).

**What "today's code" means here.** Every `file:line` below was read on 17 Oct 2026 at those two commits. Both
branches are still being built. Server: Tasks 1-8 of `docs/superpowers/plans/2026-10-17-same-voice-server.md` have
landed (`ready` and the help line's `say` carry the `clip` field: `LiveRelay._with_clip`, `live_session.py:2293-2303`,
used at `:780-783` and `:2125-2127`); Tasks 9-12 (render ahead, docs) have not. Robot: Tasks 1-6 of
`...-same-voice-robot.md` have landed (`LiveChat.stop(reason, say, clip)`, `live.py:365`; `SlotSession._play_line`,
`session.py:1042`; the kept greeting at `session.py:304`), Tasks 7-9 have not (the wind-down still skips its clip only
after `COMPANION_ENDINGS`, `session.py:929-937`; the version is still 0.15.0, `medcare_reachy/bridge/__init__.py:3`).
The new tools are built AFTER both plans finish, so the planner reads this design against the finished same-voice
code: where a line has moved, the anchor named with it (a function, a constant) is what to look for.

Sources, in the brief's order: `2026-10-17-new-tools-research.md` beside this file (the raw research results) (workflow `wf_fb441483-88d`: Pollen's
conversation app v1.1.1, the SDK 1.11.0 calls, what the two repos have; `wf_df3c0bb1-f2a`: the stop_dance and
record_medication_taken investigations), the same-voice spec and plans, then today's code. The SDK facts were read
again in the installed copy `C:\Reachy_Mini\repo\reachy-mini-tcu\.venv\Lib\site-packages\reachy_mini` (1.11.0, the
version the robot's `openapi.json` matched in the research).

## Intent and why

Pollen's conversation app gives its GPT-Live eight small tools that patients ask for in plain words: go to sleep,
louder / quieter, look at me / stop looking at me, how are you doing, forget that, look left or right, look around,
stop that move. MedAiCarePlus's live chat has none of them, so today 「你去休息吧」 is only talk, 「大聲一點」 changes
nothing, and 「忘記我剛剛說的」 is answered by a model that cannot forget. The user approved building them (Telegram,
17 Oct 13:02); `idle_do_nothing` is skipped (Pollen retired it: the idle choice is local there since June 2026, the
research's convapp notes).

Each tool goes in the chain the existing tools use. GPT-Live delegates to the Responses backend
(`gpt_live_config.BACKEND_INSTRUCTION`, `TOOLS_SCHEMA`, `gpt_live_config.py:84-233` and `:301-468`) →
`AgentLiveClient._on_delegated` notes the call and `_run_call` runs one call at a time under `_tool_lock`
(`agent_bridge.py:219-240`, `:242-270`) → `ToolRegistry.execute` → `_call`, which finds the handler by the tool's
name, drops null arguments, binds the signature and waits at most its `TIMEOUTS` entry (`tool_registry.py:1057-1072`,
`:1087-1112`; handlers at `:933`) → for a robot action `RelayRobotLink._ask({type, request_id})` over the live
WebSocket (`agent_bridge.py:67-77`) → the robot's `LiveChat._control` branch and its `_<type>_request` task
(`live.py:490-541`, `:544-547`) → `ReachyMiniHardware` on its camera or motion worker (`reachy_hardware.py:75-76`) →
`<type>_result` back → `LiveRelay._on_robot_message` routes it to the link (`live_session.py:1139-1141`) → the
registry's envelope `{ok, tool, result | error}` → GPT-Live. The relay's backstop reads the patient's words itself
(`services/tool_intents.py`; `LiveRelay._want_tool`, `_want_stop`, `_intent_due`, `_run_intent`,
`live_session.py:1608-1791`) and runs a tool when GPT-Live has not asked for it `INTENT_REPLY_GRACE_SECONDS` (2.5)
after its reply began, or `INTENT_WAIT_SECONDS` (6) after the words (`:101-102`).

## What the user decided

- Build these eight tools, after the same-voice work, as GPT-Live agent tools of the live chat. `idle_do_nothing` is
  not built.

## Claude's calls (the brief's rulings, with what this design changed and why)

Every ruling is kept in its intent. Where the code made the literal shape worse, the change is marked **changed**.

- **go_to_sleep** ends the chat the way a goodbye does: GPT-Live says one short goodbye, the chat ends with reason
  `goodbye`, the session's wind-down rules apply (no wind-down clip after a live goodbye, per the same-voice spec),
  `robot.sleep` as today. No new robot state. The tool is server-only. It needs a handle on the relay, which the
  registry has not got: `make_registry` passes only `relay._to_robot` (`agent_bridge.py:155-159`); this design adds
  "relay hooks" (section 2). The robot's own ending path (`LiveChat.stop("patient_left", say=REST_LINE)`,
  `session.py:729`) is not used: it ends the chat as `patient_left` with the rest line, not as a goodbye.
  **Changed (one detail):** today `_goodbye_said` treats a sound already playing as the goodbye answer
  (`live_session.py:1949-1955` with `_sound_started` / `_sound_ended`, `:1438-1449`); the hook arms the goodbye so
  that the answer counted is the one that starts after the tool's result went to GPT-Live, and the chat ends anyway
  `INTENT_WAIT_SECONDS` (6 s) after the result when GPT-Live says nothing.
- **volume_control** sets the speaker with the SDK's silent command: `SetVolumeCmd(volume=N)` sent with
  `mini.client.send_command` (`reachy_mini/io/protocol.py:386-390`, `io/ws_client.py:145-159`; the daemon sets the
  level and plays no sound, `daemon/backend/abstract.py:1940-1948`, unlike `POST /api/volume/set`, which plays
  `impatient1.wav` every time, `daemon/app/routers/volume.py:89-108`). The level is clamped to **20-100** on the
  server and again on the robot, so the help line stays audible; 「大聲一點」 / "louder" is +20, 「小聲一點」 /
  "quieter" −20, 「音量50」 sets 50. The robot reads the level back (`GET /api/volume/current`, `volume.py:80-86`) and
  answers it; `robot_status` reports it. Not persisted across restarts in v1: the robot keeps the last level in
  memory only, and the daemon's level at boot is whatever the card had. A backstop intent for the relative forms.
- **head_tracking** is a patient-only follower during the live chat, reusing `follow.FaceFollower`
  (`follow.py:25-77`) on the verified face box the server already sends with every camera reply (`target_box`, read
  by the session at `session.py:1119-1125`; the companion camera runs through the chat at `COMPANION_CAPTURE_FPS`,
  `session.py:61`, `:568`). ON by default when a live chat starts; GPT-Live turns it off and on; it pauses by itself
  during a dance, an emotion move, a `move_head` or `sweep_look`, and the SAY / STILL lines, never during the help
  line, and resumes after. **Changed (two details):** (1) it also pauses while GPT-Live's own voice plays, because
  Reachy's "speak" gesture has the servos then (`live.py:856` → `ReachyRobot.gesture`, `media.py:495-503`) and a
  tracking move halts that stream (`look_at` calls `_gestures.halt()`, `media.py:604`); (2) a tracking move ends by
  declaring the robot placed (`_gestures.placed()`, `gestures.py:236-239`, which today only `hold_head` calls,
  `media.py:582-585`), because after a `halt()` no gesture starts again until `placed()` (`gestures.py:241-252`,
  `:208-209`): without this one line the speaking antennas and the concern / joy posture would be off for the rest
  of the chat after the first tracking move (the research's point 2). The gesture then starts from the measured pose
  and eases back to neutral when the head is within `RECENTER_LIMIT` (15°, `media.py:37`; the rule at
  `media.py:534-561`), and when the head is further Reachy speaks still with its head on the patient. After a
  `move_head` tracking stays paused for a few seconds, else it would undo the move at once.
- **robot_status** is read-only and answers what the robot reports: app version, volume, head tracking on or off,
  whether a dance or emotion move runs, camera ok (frames flowing), microphone muted or not, the time since the chat
  started, the robot's own uptime. No battery (mains-powered). Nothing that names the patient. GPT-Live reads it
  out in plain words. No backstop (a rare ask GPT-Live delegates well enough; nothing moves if it does not).
- **forget** deletes the matching memory note when the patient's ask is clear, reads the memory back and answers
  what was forgotten and what is left. **Changed (what "clear" means, made a server rule):** the tool deletes in one
  call only when the patient's words match exactly one current note by that note's own words (its subject or its
  text, folded as `get_patient_memory` folds them, `tool_registry.py:879-890`); `category` narrows, never matches
  alone; several matches, or none, delete nothing and answer the candidates, so GPT-Live asks back. The `name` entry
  and notes the patient typed in the app (`source = 'patient'`) are never deleted by voice (`in_app_only`): the
  database makes the name patient-entered only (`sql/init.sql:365`), a typed note is a deliberate entry, and a
  transcript can mishear; the app's own delete is one tap away. Memory consent must be current, as `remember` and
  `get_patient_memory` require (`tool_registry.py:858-866`, `:879-881`). The backstop never runs it: a deletion
  comes only from the model's call. The **same-chat relearn gap** is closed in `memory.store_facts` (section
  "forget" below): a tombstone counts from the chat's `started_at`, not its `ended_at`, and it also carries the
  deleted note's text; the after-chat extractor is told what was forgotten; the running chat's memory block is
  rebuilt and GPT-Live is told to treat the note as unknown.
- **move_head** looks left / right / up / down / centre with the facade's bounded `goto` (`media.py:570-580` →
  the SDK's `goto_target`, `reachy_mini.py:672-722`), relative to the current pose, one small step each (20° yaw,
  15° pitch, within ±40° yaw and ±25° pitch: the robot's own scan already goes to ±40° yaw, `SCAN_YAWS_DEG`,
  `media.py:24`; the SDK's limits are ±40° pitch and roll, the research's core-concept page), never while the help
  line is said; tracking pauses for it and resumes. **Changed (one addition):** with tracking off the head comes
  back to centre by itself 8 s after the step (`hold_head`), so the speaking gestures are not lost for the rest of
  the chat (they need the neutral pose within 15°, above); with tracking on the follower takes over then.
- **sweep_look** is the existing `look_around()` (`media.py:587-588`: eight steps of 1.5 s over `SCAN_YAWS_DEG`,
  about 12 s), run on the motion worker in the same stoppable slot as a dance, so 停 / "stop" and the chat's end stop
  it; it answers when done; tracking pauses for it.
- **stop_emotion** stops whatever expressive move runs (a dance or an emotion move): the robot's `stop_dance()`
  already cancels either (`reachy_hardware.py:285-298`), plus `express(None)` to lift the posture
  (`media.py:505-510`). It shares the stop hold of `stop_dance` (`STOP_HOLD_SECONDS`, `tool_registry.py:65`; the
  refusal at `:1217-1219`), so a dance asked for right after it stays refused until the patient asks to dance again
  (`dance_asked`, `:1020-1025`). The backstop's stop phrases run `stop_emotion` instead of `stop_dance` when an
  emotion move, not a dance, is running. **Changed (one addition):** during the hold `play_emotion` is refused with
  `stopped_by_patient` too, and the relay sends no automatic posture or move (`LiveRelay._express`,
  `live_session.py:1540-1559`): a patient who said 「不要這樣」 must not see the robot droop again on their next sad
  sentence.
- **Every tool** gets a `TOOLS_SCHEMA` entry, a `ToolRegistry` method, a `RelayRobotLink` request type with a result
  type the relay routes, the robot's `_control` branch and hardware method, a `BACKEND_INSTRUCTION` sentence, tests
  on both sides, CLAUDE.md and README words. Robot app **0.17.0**. Robot app 0.16.0 keeps working with the new
  server. **Changed (how):** a 0.16.0 robot cannot answer a type it does not know (it only logs it,
  `live.py:539-540`), so the "robot cannot do that yet" error must come from the server: a version gate on the
  robot's `bridge_version` refuses the six robot tools at once with `robot_too_old`; from 0.17.0 on the robot
  answers an unknown request with `unknown_request`, which the link turns into `robot_cannot_do_that`. Either way
  the link's timeout (`_ask`, `agent_bridge.py:67-77`) still bounds a request nobody answers.
- **Tool names** stay Pollen's, `forget` included. The research proposed `forget_memory` because `ToolRegistry` has
  `forget_intents` (`tool_registry.py:1050-1055`) and "forget a reminder" means `cancel_reminder`
  (`gpt_live_config.py:224-225`); `forget` collides with nothing (handlers are looked up by the exact tool name,
  `:933`), and its description and the MEMORY section say "a memory note only, never a reminder".
- **Consent:** head tracking and robot_status add no new data use (the camera frames and the heartbeat already flow
  in a chat). `forget` needs memory consent current. No notice text changes.
- **Out of scope:** GPT-Live seeing the camera; battery; persisting the volume; any change to the dose flow; the
  daemon's own face tracker (`start_head_tracking`, `reachy_mini.py:275-286`: it follows any face, and at weight 1
  the daemon ignores the app's head targets, so gestures, emotes, dances and `goto_sleep` would be overridden, the
  research's point 2); the memory notice's "not learned again for 7 days" wording (research gap B).

## Goals

1. Each of the eight asks works by voice in a live chat, through GPT-Live's call or the backstop, and GPT-Live says
   only what the tool answered.
2. No tool can make the help line inaudible, move the head during it, or delete a note the patient did not clearly
   name.
3. A robot app older than 0.17.0 with the new server, and a 0.17.0 robot with an older server, both keep today's
   live chat: an unknown tool is a quick refusal, never a hang or a crash.
4. The head follows the patient while Reachy listens, and Reachy's speaking antennas and postures keep working.
5. Every behaviour named here has a test on the side that owns it.

## Non-goals (v1)

- Persisting the volume; a microphone volume tool; `idle_do_nothing`.
- The daemon's face tracker; tracking in the text chat or during a dose watch (the dose watch keeps today's
  follower, `session.py:1111-1125`).
- Changing the memory notice text.
- Deleting every note by voice (the app's `DELETE /api/memory?confirm=all`, `api_memory.py:100-104`, stays the way).
- Reporting the robot's Wi-Fi, IP, IMU or temperature in `robot_status`.
- A backstop for `forget` (never), `robot_status`, `move_head` or `sweep_look`.

## Anti-goals (a failure even if it works)

- A help line made inaudible: the volume floor is 20, enforced on the robot and not only in the prompt.
- A head move (a tool or tracking) during the help line, or a tracking move that fights a dance, an emote, a goto or
  Reachy's speaking gesture.
- A memory note deleted on a vague ask, by category alone, by the backstop, or a typed note or the name deleted by
  voice.
- A forgotten note that comes back from this chat's own transcript, or that GPT-Live keeps bringing up.
- A chat that hangs on a tool the robot does not know, or a tool answer that claims success the robot did not
  confirm.
- The patient's name, a key or a token in a log line or in `robot_status`.

## Constraints

- Server: FastAPI + asyncpg; `C:\Reachy_Mini\repo\MedAiCarePlus\.venv\Scripts\python.exe -m pytest tests -q -p
  no:cacheprovider` from the worktree root must stay green. Two tests pin the tool count at 25
  (`tests/test_tool_registry.py:218`, `tests/test_agent_bridge.py:67`): it becomes 33. The schema test requires every
  property listed as required with nulls allowed (`test_tool_registry.py:232-234`), `TIMEOUTS` keys equal to
  `TOOL_NAMES` (`:235`) and a handler per tool (`:236`).
- `CLAUDE.md`'s live voice section (`CLAUDE.md:188-233`) is checked by `tests/test_live_voice_docs.py` (its
  `SECTIONS`, `:29-33`: every backticked name must exist in the server code, `NAME = value` spans must match): new
  names written there must exist in `app/` first, and robot-only names must not be backticked.
- Robot: Python 3.12 on the Reachy Mini; `pytest tests medcare_reachy/bridge/tests -q -p no:cacheprovider`.
  `tests/test_app.py:393` pins the version literal in `__init__.py` and `pyproject.toml`.
- The hardware adapter never calls the SDK itself: every call goes through the `ReachyRobot` facade
  (`reachy_hardware.py:3-5`), and the `media.Robot` Protocol (`media.py:65-88`) lists what the fakes must stub.
- The live WebSocket's JSON messages all carry a `type`; a robot request carries a `request_id` and is answered with
  `<type>_result {request_id, ok, result | error}` (`agent_bridge.py:139-146`, `live.py:565-644`).
- Link timeouts stay below the registry's `TIMEOUTS` entry for the same tool, so a slow robot shows as
  `robot_did_not_answer`, not `tool_timeout` (`agent_bridge.py:48-51`, `tool_registry.py:67-80`).
- The help line is never GPT-Live's and is said still (`live.py:692`: `still = reason == "risk"`); nothing here may
  touch the SAY rules.
- Never the SDK's `cancel_move()` (`reachy_mini.py:1105`): it stops the app's own audio pipeline (the research's
  point 1, `bridge/moves.py:6-8`).
- Plain simple English in docs and messages; plain Traditional Chinese in the zh-TW texts; the robot's own Matcha
  texts (Simplified) are not changed.
- No pushes or merges; commits on a feature branch of each repo.

## The design, shared pieces

### 1. The schema, the instruction text and the live note (server, `gpt_live_config.py`)

Eight `_tool(...)` entries appended to `TOOLS_SCHEMA` (`gpt_live_config.py:301-468`), each in the strict shape
(`_tool`, `:236-239`: every property required, optional ones typed `["x", "null"]`):

| tool | properties |
|---|---|
| `go_to_sleep` | none |
| `volume_control` | `level` integer 20-100 or null; `change` enum `up`, `down`, null (exactly one of the two) |
| `head_tracking` | `enabled` boolean |
| `robot_status` | none |
| `forget` | `query` string 1-300 (the note in the patient's words, its key words); `category` enum `preference`, `routine`, `event`, `person`, null (narrows only; the same words as `remember`, mapped by `MEMORY_KIND`, `tool_registry.py:95`) |
| `move_head` | `direction` enum `left`, `right`, `up`, `down`, `centre` |
| `sweep_look` | none |
| `stop_emotion` | none |

Descriptions are plain and name what the tool answers; `forget`'s says "a memory note only, never a reminder".

New `BACKEND_INSTRUCTION` sections (`:84-233`), one short paragraph each, in the voice of the DANCE section
(`:183-193`): SLEEP, VOLUME, HEAD TRACKING, ROBOT STATUS, HEAD MOVES; the MEMORY section (`:169-172`) gains `forget`;
the DANCE section gains `stop_emotion`. Every section repeats the two rules that already govern the others: never
claim what the tool did not answer; `ok=false` means it did not happen, say so plainly (general rule 2, `:92-93`).
The general rules gain one sentence: `robot_too_old` or `robot_cannot_do_that` means Reachy's robot app cannot do
that yet: say so and move on. The exact sentences are in each tool's part below.

`LIVE_TOOL_NOTE` (`:36-82`) gains, in both languages, one clause per tool naming the patient phrases that must be
delegated at once (the examples in each tool's part), in the style of the dance and time clauses (`:41-45`,
`:60-65`). `LIVE_RULES_TOOLS` (`live_session.py:254-271`) needs no change: "anything to see, to do, to look up ...
goes to the backend" covers them.

### 2. The registry (server, `tool_registry.py`)

- `TIMEOUTS` (`:67-80`) gains eight keys (the table in "Timeouts" below).
- `RobotLink` Protocol (`:126-134`) gains `volume`, `head_tracking`, `robot_status`, `move_head`, `sweep_look`,
  `stop_emotion`.
- `ToolRegistry` gains one method per tool, next to `dance` / `stop_dance` / `play_emotion` (`:1203-1285`).
- **Robot version gate.** `ToolRegistry.__init__` (`:930-949`) takes `robot_version: tuple | None`. The six robot
  tools raise `ToolError("robot_too_old")` at once when the version is below `ROBOT_TOOLS_VERSION = (0, 17, 0)`, or
  unknown. The relay keeps the robot's `bridge_version` from `start` (today read once at `live_session.py:728` for
  `companion_robot` and dropped) in `self.bridge_version`; `make_registry` (`agent_bridge.py:155-159`) parses it
  with the leading-digits rule of `companion_robot` (`live_session.py:369-381`; make that parser a shared
  `version_tuple(text)`) and passes the tuple. `go_to_sleep` and `forget` need no robot.
- **Relay hooks.** `ToolRegistry.__init__` also takes `hooks: RelayHooks | None`, a small `Protocol` in
  `tool_registry.py` with five methods the relay implements and `make_registry` passes (`hooks=relay`):
  - `request_goodbye() -> bool` (go_to_sleep): arms the goodbye ending; False when the chat already has an outcome or
    a risk (`self.outcome`, `self._risk`).
  - `help_line_active() -> bool` (move_head, sweep_look): the relay's `_voice_held()` (`live_session.py:1959-1962`)
    or `_risk` set; a head move is refused then with `ToolError("help_line")`.
  - `note_forgotten(kind, subject, text) -> None` (forget): the relay rebuilds the memory block and tells GPT-Live.
  - `posture_cleared() -> None` (stop_emotion): the relay forgets its own affect state.
  - `chat_seconds() -> float` (robot_status): the relay's clock since `_started_at` (`live_session.py:891`).
  With `hooks` None (unit tests that build a bare registry, `tests/test_tool_registry.py:236`), `go_to_sleep`
  answers `ToolError("no_relay")`, `help_line_active` counts as False and the rest are no-ops.
- **Expressive-move clock.** `ToolRegistry` keeps `_emotion_at` beside `_dance_at` (`:934`): `play_emotion` sets it
  (`:1278-1285`), and the relay calls a new `tools.note_move()` whenever it sends the robot an `express` with a
  `move` (`_express`, `live_session.py:1540-1559`; `_send_pending_move`, `:1561-1569`). `expressing()` is true for
  `EMOTION_ACTIVE_SECONDS = 20.0` after it (the curated moves last 2-6 s, `gpt_live_config.py:280-298`; the window
  also covers a move queued behind emotes). `stop_emotion` and `stop_dance` both reset `_dance_at` and `_emotion_at`
  and set `_stopped_at` (`:1270`); `play_emotion` and the relay's automatic expression respect the hold
  (`held()`: `clock() - _stopped_at < STOP_HOLD_SECONDS`).

### 3. The link and the relay (server, `agent_bridge.py`, `live_session.py`)

- `RelayRobotLink` (`agent_bridge.py:59-152`) gains one method per robot tool, each `_ask({type, ...}, timeout)`
  and, on `ok` false, `ToolError(f"<tool>_failed: {error[:160]}")`, as `stop_dance` and `express` do (`:118-137`).
- `_ask` (`:67-77`) gains one check: an answer whose `type` is `unknown_request` raises
  `ToolError("robot_cannot_do_that")` (the 0.17.0 robot's answer to a type it does not know, section 5).
- `LiveRelay._on_robot_message` (`live_session.py:1126-1141`) routes the new answer types to
  `self.tools.robot.answer`: `volume_result`, `head_tracking_result`, `robot_status_result`, `move_head_result`,
  `sweep_look_result`, `stop_emotion_result`, `unknown_request` (added to the tuple at `:1139`).
- The relay keeps `self.bridge_version` (section 2) and implements the five hooks.
- `make_registry` (`agent_bridge.py:155-159`) passes `hooks=relay` and
  `robot_version=version_tuple(relay.bridge_version)`.

### 4. The backstop (server, `tool_intents.py`, `live_session.py`)

Three new intents in `INTENTS` (`tool_intents.py:33`), read in this order: `stop_dance` (now also `stop_emotion`,
decided in `_want_stop`), `took_medicine`, **`sleep`**, **`volume`**, **`tracking`**, `dance`, `look`, `time`,
`search`. `TOOLS` (`:34-39`) gains `sleep: (go_to_sleep,)`, `volume: (volume_control,)`,
`tracking: (head_tracking,)` and `stop_dance: (stop_dance, stop_emotion)`, so GPT-Live's own call of either stop
counts as told (`_told`, `live_session.py:1595-1606`). `PATTERNS` (`tool_intents.py:59-108`) gains the three
tables (each tool's part below); `tracking`'s forms are blanked out of the words before the `look` patterns run (as
`NOT_STOP` is blanked before the stop patterns, `_blank`, `:302-304`), so "don't look at me" never reaches
`describe_view` through `look at (me|this|that)` (`:90`). Plain "look at me" stays a camera look, as the intent
table pins (`tests/test_live_tool_backstop.py:262-285`).

`INTENT_TOOL` (`live_session.py:105-110`) gains the three entries, each building its arguments from the words:
`sleep → ("go_to_sleep", {})`; `volume → ("volume_control", {"level": None, "change": up | down from the words})`;
`tracking → ("head_tracking", {"enabled": not an off-form})`. `INTENT_LINES` (`:112-141`) gains `sleep`, `volume`,
`volume_at_min`, `volume_at_max`, `tracking_on`, `tracking_off`, `tracking_unavailable`, `stop_emotion` (the words
of `stop_dance`) and `robot_too_old` (「這個動作要等機器人更新才會。」 / "Reachy needs an update before it can do
that."). `_intent_line` (`:1870-1903`) gains one branch per new kind **before** its last branch, which treats any
other kind as a camera look (`:1900-1903`). `_want_stop` (`:1642-1668`) passes
`dancing=self.tools.dancing() or self.tools.expressing()` to `tool_intents.stops` (`:307-336`), so 停 / "stop"
during an emotion move stops it at once too, and runs `stop_emotion` instead of `stop_dance` when
`self.tools.expressing() and not self.tools.dancing()`; the `_stop` slot keeps the tool's name for `_run_intent`
and `_told`. No backstop for `forget` (a deletion must come from the model's call: the research's rule 4),
`robot_status`, `move_head` or `sweep_look`.

### 5. The robot (`live.py`, `reachy_hardware.py`, `media.py`, `session.py`, `runner.py`)

- `LiveChat._control` (`live.py:490-541`) gains one branch per request type, each `_start_request(...)` as the
  dance branch does (`:526-529`), and its last branch (`:539-540`, "ignored a message it does not know")
  **answers** a message that carries a `request_id` with `{"type": "unknown_request", "request_id", "error":
  "unknown_request:<type>"}` before logging. A message without a `request_id` is still only logged: the relay's own
  `express` carries none (`live_session.py:1523`, `:1559`, `:1569`; `live.py:630-631`). So a server newer than
  the robot gets a refusal at once for any future tool.
- Each `_<tool>_request` answers `{"type": "<tool>_result", "request_id", "ok", "result" | "error"}`, refusing with
  `no_motion` without the hardware adapter and `gestures_off` with the settings page's check-in gestures switch
  off, exactly as `_emote_request` (`:565-579`) and `_dance_request` (`:581-601`) do, for the head-moving tools
  (`move_head`, `sweep_look`); `stop_emotion` has no gestures check (a stop always works, as `_stop_dance_request`,
  `:633-644`); `head_tracking` refuses with `follow_face_off` when the settings page's "Follow the patient's face"
  switch is off (`service.face_follower` builds no follower then, `service.py:39-46`).
- A `say` with reason `risk` (the help line, `live_session.py:2124-2127`) first cancels a recorded move or sweep
  (`hardware.stop_dance()`), so nothing already running moves the head under it; tracking is off while any own line
  is queued (section "head_tracking").
- `ReachyMiniHardware` (`reachy_hardware.py:64-333`) gains `set_volume`, `get_volume`, `track`, `move_head`,
  `recentre`, `sweep_look`, `running_move` (per tool below). Every head move runs on the one motion worker (`:76`),
  so it never interleaves with an emote, a dance or an emotion move, and `MAX_PENDING_EMOTES` (`:42`, `:168-171`)
  bounds the queue.
- `ReachyRobot` (`media.py:161-689`) gains `set_volume(level)`, `get_volume()`, `head_angles()`, `place()`; its
  `look_at` ends with `self._gestures.placed()`; the `media.Robot` Protocol (`:65-88`) lists the new methods so
  `VideoFileRobot` (`:692`) and the tests' `FakeRobot` stub them.
- `SlotSession` hands the chat what it needs beside `chat.gestures`, `chat.mood` and `chat.lines`
  (`session.py:645-647`): `chat.follow = self.follow` (the session's `FaceFollower`, `:162`) and `chat.status =
  self._live_status` (a callable: the camera snapshot, the microphone consent, the app's uptime); its `_run_live`
  loop (`:675-754`) feeds the chat the latest face box each poll (`chat.track(self._latest(), now)` right after
  `_keep_watching`, `:718`).
- `Runner.__init__` (`runner.py:506-525`) records `self.started_at = self.clock()`, handed to each `SlotSession` in
  `run_slot` (`:613-634`) for the uptime.
- Version 0.17.0 in `medcare_reachy/bridge/__init__.py:3`, `pyproject.toml:8` and `tests/test_app.py:393`; the
  README's live section (`README.md:136`) lists the new request types and the tracking rules, and "Status"
  (`:191`) gets a 0.17.0 entry. The `live.py` module docstring (`:8-24`) lists the new types.

## The design, per tool

### go_to_sleep

**When.** The patient asks Reachy to sleep, rest or go away: 「去睡覺」, 「你去睡覺吧」, 「你休息吧」, 「你可以去休息了」,
「日希，去睡吧」, "go to sleep", "you can rest now", "go rest", "go away now". Never the patient's own sleep (「我要去睡覺
了」, 「我想睡覺」, "I'm going to sleep", "I want to sleep"): the backstop's `NOT_SLEEP` guard blanks first-person forms
(我 within three characters before 睡 / 休息; "I'm / I am / I want to / I'm going to ... sleep / bed"), clause by
clause as `stops` reads clauses (`tool_intents.py:325-335`), so 「我要去睡了，你也去睡吧」 still asks Reachy to sleep.

**Server.** `ToolRegistry.go_to_sleep()`: `hooks.request_goodbye()`; True → `{"status": "ending", "after_goodbye":
true, "grace_seconds": GOODBYE_GRACE_SECONDS}`; False → `ToolError("chat_already_ending")`. `request_goodbye` sets
`_goodbye = True`, `_goodbye_replied = False`, `_goodbye_end_at = now + INTENT_WAIT_SECONDS` (the fallback when
GPT-Live says nothing), and marks that the next reply's sound is the answer: `_sound_started` (`live_session.py:1438-
1442`) clears the deadline when that sound starts, `_sound_ended` (`:1444-1449`) sets it to `now +
GOODBYE_GRACE_SECONDS` (3, `:181`) when it ends, and `_tick` finishes the chat `("end", "goodbye")` (`:1047-1048`)
→ `_wrap_up` → `_end_chat` (`:2224-2247`) → `end {reason: goodbye}` to the robot (`:2246`). A sound already playing
when the tool runs is not the answer (the "changed" note above). Nothing else changes: a claim of a dose still in
its grace runs at the wrap-up (`_claim_at_end`, `:2198`); a risk still outranks it (`:2199-2200`).

**BACKEND_INSTRUCTION, SLEEP:** "go_to_sleep() when the patient tells Reachy to sleep, rest or go away (never for
their own sleep, a sleepy mood or an unclear wish). It answers status ending: the Live model then says one short
goodbye and nothing more; the chat ends by itself after it. Never promise to stay or to come back at a time."

**Robot.** Nothing new: the robot gets `end {reason: goodbye}` as for a spoken goodbye (`live.py:536-538`), the
session records `outcomes["checkin"] = "goodbye"` (`session.py:670-671`), a check-in task goes to WIND_DOWN and a dose
task's chat to POST_SLOT_OBSERVE first (`_after_checkin`, `:908-914`); the same-voice rule (robot plan Task 7) skips
the wind-down clip after a live goodbye; then `robot.sleep` (`:936`).

**Backstop.** Intent `sleep`; `INTENT_LINES["sleep"]`: 「好，我去休息了，再見！」 / "Okay, I'll rest now. Goodbye!".
The commentary is GPT-Live's goodbye answer, so the chat ends 3 s after its sound.

### volume_control

**When.** Relative: 「大聲一點」, 「大聲點」, 「再大聲一點」, 「太小聲了」, 「聲音大一點」, "louder", "turn it up", "speak
up", "too quiet"; 「小聲一點」, 「小聲點」, 「太大聲了」, 「太吵了」, 「不要那麼大聲」, "quieter", "turn it down", "too
loud", "not so loud". Absolute: 「音量50」, 「音量調到五十」, 「音量一半」 (50), 「最大聲」 (100), 「最小聲」 (20), "volume
50", "set the volume to 50". The backstop reads only the relative forms (the ruling); absolute levels go through
GPT-Live's call. Not 「電視太大聲」 (a `NOT_VOLUME` guard: the words name 電視 / TV / 收音機 / radio).

**Server.** `ToolRegistry.volume_control(level=None, change=None)`: exactly one of the two, else
`ToolError("level_or_change_required")`; `level` outside 20-100 is clamped (the answer says so, `clamped: true`);
`change` is `up` or `down`. It sends `{"type": "volume", "level": int | null, "change": "up" | "down" | null}` and
returns the robot's result. `VOLUME_MIN = 20`, `VOLUME_MAX = 100`, `VOLUME_STEP = 20` live in `gpt_live_config.py`
beside `MAX_DANCE_REPEAT` (`:272`) and are repeated on the robot (`reachy_hardware.py:40-43` already duplicates
`EMOTE_PRESETS` and `MAX_DANCE_REPEAT` by hand), because the robot enforces the floor itself.

**Robot.** `_volume_request` → `hardware.set_volume(level, change)` (a short blocking call through `_run_blocking`,
not the motion worker): `previous = robot.get_volume()` (`GET http://127.0.0.1:8000/api/volume/current`, the `httpx`
pattern and 2 s timeout of `media_status`, `media.py:379-388`, `DAEMON_PORT` at `:38`; None on any error); `wanted =
level`, or `previous ± 20` (with no readable `previous`, a relative ask uses the last level this app set, else 60),
clamped to 20-100; `robot.set_volume(wanted)` sends `self._mini.client.send_command(SetVolumeCmd(volume=wanted))`
(fire-and-forget: `ws_client.py:159` sends and returns, so the level is read back); then `robot.get_volume()` again.
Result `{"volume": int, "previous": int | null, "read_back": bool, "at_limit": "min" | "max" | null, "requested":
int}`; `read_back` false when the `GET` failed, and `volume` is then the level sent. A relative ask already at the
limit sends nothing and answers `at_limit` with the unchanged level. The robot keeps `_volume` (the last level it set
or read) for `robot_status`; nothing is written to disk.

**BACKEND_INSTRUCTION, VOLUME:** "volume_control(change="up" | "down") for louder / quieter (one step), or
volume_control(level=N) for a number the patient said (20-100; the robot never goes below 20, so it can always be
heard). Say the level it answers; at_limit min means it is already as quiet as it will go: say that."

**Backstop.** Intent `volume`; lines: 「好，音量調到{volume}了。」 / "Okay, the volume is {volume} now.";
`volume_at_min`: 「音量已經是最小了，再小你會聽不到我。」 / "That's as quiet as I go, or you wouldn't hear me.";
`volume_at_max`: 「音量已經最大了。」 / "That's as loud as I go."

**To check on the robot (left to the builder):** the research did not verify that the daemon's mixer level (pulsectl
or amixer, `volume_control_linux.py` per the research) also scales the app's own GStreamer playback, which carries
the live chat's audio. One listening test settles it; if it does not, the fallback is a software gain on the chat's
`push_audio` blocks (`live.py:827`), as the TCU companion does (`companion/audio/base.py:64` per the research), with
the same 20-100 floor.

### head_tracking

**When.** On: 「看著我」, 「一直看著我」, 「眼睛看我」, 「跟著我」, 「看我這邊」, 「轉過來看我」, "keep looking at me", "follow
me", "follow my face", "keep your eyes on me", "turn to me", "face me". Off: 「不要一直看我」, 「不要看我」, 「別一直看我」,
「不要盯著我」, 「別盯著我看」, 「不要跟著我」, 「不用看我」, "stop following me", "stop looking at me", "don't look at me",
"don't stare at me", "stop staring". An off-form wins over an on-form inside it (「不要看著我」 holds 看著我); plain
"look at me" stays the camera look (section 4).

**Server.** `ToolRegistry.head_tracking(enabled)`: `{"type": "head_tracking", "enabled": bool}` → the robot's result
`{"enabled": bool, "available": bool, "reason": null | "follow_face_off" | "no_motion"}`. The robot's answer is the
truth; the server keeps no copy.

**Robot, the follower in the chat.** `LiveChat` gets `follow` (the session's `FaceFollower`, `follow.py:25-77`; None
when the settings switch is off), `tracking = True` at construction (ON by default), `_tracking_resume_at = -inf`,
and `track(latest, now)`, called by the session's `_run_live` loop each poll (`LIVE_POLL_SECONDS = 0.2`,
`session.py:65`; the loop at `:701-740`) with `self._latest()` (`:1009-1018`: the latest camera reply of the attached
companion session; `target_box` and `identity_status` as `_follow_face` reads them, `:1124`). `track` does nothing
unless **all** hold: `self.tracking and self.follow is not None and self.hardware is not None`; no sound is playing
or queued (`not self._playing and not self._say_active() and not self._say_tail`: `_playing` is set at the first block
with sound and cleared `IDLE_QUIET_BLOCKS` after the last, `live.py:853-865`; `_say_active`, `:759-760`; `_say_tail`,
`:850-852`); no motion job is pending and no recorded move runs (`hardware.pending_motion_jobs == 0`,
`reachy_hardware.py:156-159`, and `hardware.running_move() is None`); the chat is not closing (`self.closing`,
`:343`); and `now >= self._tracking_resume_at` (set by `move_head`). When allowed again after a pause it first calls
`follow.start()` (`follow.py:38-41`: it forgets its last target, so the next box re-aims the head even if the face did
not move). Then `hardware.track(follow, box, now)`: `follow.update(box, now)` submitted to the motion worker (so a
tracking move never runs beside an emote's gotos), at most one in flight. The follower's own rules stay: one move
per `INTERVAL` (0.4 s), a `DEADBAND` of 0.06, `MOVE_SECONDS` 0.35, recentre after `RELEASE_AFTER` 3 s without a face
(`follow.py:19-22`, `:48-70`). `follow.stop()` when the chat ends (`run()`'s finally, `live.py:417-440`).

`ReachyRobot.look_at` (`media.py:590-605`) gains one line after `look_at_image`: `self._gestures.placed()`
(`gestures.py:236-239`). So when GPT-Live starts talking, `_set_gesture("speak")` (`live.py:856`) works: the gesture
stream starts from the measured pose by today's rule (`_start_pose`, `media.py:534-561`: within `NEUTRAL_TOLERANCE`
from neutral; within `RECENTER_LIMIT` with the motors on, easing back to neutral over `RECENTER_SECONDS`; further, or
motors off, no stream); when the sound ends (`gesture(None)`, `:864`), `track` resumes and re-aims. `look_at` is
still skipped while `_dancing` (`media.py:595`). The dose watch is unaffected: in WATCHING nothing calls `gesture()`
(the session's `_say` gestures only in CHECKIN, `session.py:1081-1097`). A reply whose sound starts during a
tracking move (0.35 s) has its "speak" gesture ignored (`halt` → `set` ignored until `placed`, `gestures.py:208-209`)
and is said still: accepted.

The dose watch's follower and `_sync_follow` (`session.py:1111-1117`) are untouched: in CHECKIN the session never
starts it (`_sync_follow` starts it only for WATCHING), so the chat's `track` is the only caller during a chat.

**BACKEND_INSTRUCTION, HEAD TRACKING:** "Reachy's head follows the patient's face by itself while it listens (head
tracking is on when a chat starts). head_tracking(enabled=false) when they ask it to stop looking at or following
them; head_tracking(enabled=true) when they ask it to look at or follow them again. Its answer says whether it is
on; available=false with reason follow_face_off means the robot's face-following setting is off: say Reachy cannot
do that on this robot."

**Backstop.** Intent `tracking`; lines `tracking_on`: 「好，我會看著你。」 / "Okay, I'll keep looking at you.";
`tracking_off`: 「好，我不盯著你了。」 / "Okay, I'll stop watching you."; `tracking_unavailable` (`available` false):
「這台機器人沒有開臉部跟隨，我沒辦法。」 / "Face following is off on this robot, so I can't."

### robot_status

**When.** 「你還好嗎」, 「你的狀態怎麼樣」, 「你現在音量多少」, 「你有在看我嗎」, "how are you doing", "what's your status",
"what's your volume". GPT-Live's call only.

**Server.** `ToolRegistry.robot_status()`: `{"type": "robot_status"}` → the robot's result, plus the server's own
facts: `chat_seconds` (`hooks.chat_seconds()`), `dancing` / `expressing` from the registry's clocks (the robot's
`running_move` wins when present). The answer never carries a name, an id, an address or a key.

**Robot.** `_robot_status_request` composes, off the loop: `app_version` (`__version__`), `volume` (`robot.get_volume()`,
else the last level this app set, else null), `head_tracking` (`self.tracking and self.follow is not None`),
`running_move` (`hardware.running_move()`: `{"library": "dance" | "emotion" | "sweep", "move": name}` or null, from
the `_dance` slot, `reachy_hardware.py:79`, `:289-293`), `camera` (the session's status callable gives
`self.stream.camera.snapshot()`: `state` and `fps`, `camera_watch.py:123-132`, as the heartbeat sends it,
`runner.py:656`), `microphone_muted` (`self._say_active() or bool(self._say_tail)`: the chat sends silence then,
`live.py:778-779`), `microphone_consent` (the session's `task["microphone"]`, `session.py:707`), `chat_seconds` (the
chat's clock since `run()` began; keep `self.started_at` in `run`), `uptime_seconds` (the Runner's start time through
the status callable), `gestures` (the settings switch, `self.gestures`), `speaker_ok` (`self.speaker is not None and
self.speaker.available`). Nothing here needs the motion worker; the one network call is the 2 s volume `GET`.

**BACKEND_INSTRUCTION, ROBOT STATUS:** "robot_status() when the patient asks how Reachy is, what its settings are or
whether it is working: it answers the app version, the volume, whether head tracking is on, whether a dance or
emotion move is running, whether the camera is giving frames, whether the microphone is muted right now, how long
this chat has run and how long the robot has been on. Read it out in two short plain sentences; there is no battery
to report (Reachy is plugged in)."

### forget

**When.** 「忘記我剛剛說的」, 「不要記得我喜歡吃甜食」, 「把我孫女的事忘掉」, 「不要記這個」, "forget that", "forget what I
said about my garden", "don't remember that". GPT-Live's call only: **no backstop intent**.

**Server, the tool.** `ServerCareBackend.forget(query, kind=None)` beside `remember` and `get_patient_memory`
(`tool_registry.py:861-890`), wrapped by `ToolRegistry.forget(query, category=None)` (clips `query` to 300, maps
`category` through `MEMORY_KIND`, `:95`; `care_note` and `other` → `ToolError("invalid_memory_category")`):

1. Consent: `if not await self._memory_on(): return {"deleted": 0, "reason": "memory_consent_off"}` (`_memory_on`,
   `:858-859`). Reading notes back to the model needs memory consent, as `get_patient_memory`.
2. Find: `facts = current_facts(conn, u_id)` (`memory.py:260-261`), narrowed by `kind` when given; a match is the
   folded query (`memory._fold`, `:57-59`) inside the folded subject or text, or a query word of 2+ CJK characters
   or 3+ Latin letters inside the folded text (tighter than `get_patient_memory`'s 2-character rule, `:884`: a
   deletion must not match on "my" or "on"). `kind == 'name'` and `source == 'patient'` notes are excluded from
   deletion: when the only match is such a note, answer `{"deleted": 0, "reason": "in_app_only", "note": {kind,
   subject, text}}`.
3. No match: `{"deleted": 0, "reason": "not_found"}`. Several matches: `{"deleted": 0, "reason": "several",
   "candidates": [{kind, subject, text, event_date}, ... up to 5]}`. A query that is only a category word (記得的人 /
   "everyone") matches nothing by words: `not_found` with `hint: "name_the_note"`.
4. Exactly one match: delete it exactly as `api_memory._delete` does (`api_memory.py:83-88`): inside
   `conn.transaction()`, `ids = await memory.delete_facts(conn, u_id, kind, subject)` (`memory.py:461-479`: the
   lock, every row of the pair, the tombstone, the `deletion_ledger` rows), then after the commit
   `deletion_ledger.append_host_file("memory", u_id, id)` for each id (`deletion_ledger.py:21-32`, never raises).
   The transaction and the appends run together under `asyncio.shield`, so the tool's `wait_for`
   (`tool_registry.py:1097`) or `_db`'s 5 s (`:212`) can never cancel between the commit and the host ledger (the
   research's point 9: a restore replays the host file, and would bring the note back).
5. Read back: `remaining = current_facts(...)` after the commit; answer `{"deleted": n, "forgotten": {kind, subject,
   text, event_date}, "remaining_count": len(remaining), "remaining_kinds": {kind: count}}`. The remaining notes'
   texts are not sent back (keep the answer small).
6. `hooks.note_forgotten(kind, subject, text)` (below). Logging: the tool, the kind and the count, never the text.

**Server, the running chat.** `LiveRelay.note_forgotten(kind, subject, text)`:
- rebuild the memory block with `memory.build_block` (`memory.py:436-443`) under `_bounded`
  (`live_session.py:323-332`) and replace `self._live_args = (target, name, block)` (`:568`, set at `:766`), so the
  next stretch's instructions (`_start_stretch`, `:1259-1265`) no longer carry the note; a failed rebuild keeps the
  old block and logs (as the start does, `:716-723`);
- when a stretch runs (`self._phase == "running"`, `self.client` set, not `_live_closed`), send
  `client.instructions_append(FORGOTTEN_NOTE[language].format(text=text))`, the precedent being `SPOKEN_NOTE`
  (`:173-174`, sent at `:831-834`): 「長者剛剛要你忘記這件事：「{text}」。從現在起不要再提它，也當作不知道。」 / "They just
  asked you to forget this: \"{text}\". Do not mention it again and treat it as unknown."
- The stored turns are not changed (the notice says deleting a note never changes its chat; the companion note quotes
  the last 20 turns as data, `:1262-1265`): the note above is what keeps GPT-Live from using them.

**Server, the relearn gap (closed).** Today `store_facts` skips a candidate whose `(kind, subject)` has a tombstone
newer than the chat's `ended_at` (`memory.py:281-289`); a note deleted during the chat is older than `ended_at`, so
this chat's after-chat extraction (`after_chat.py:275-281` → `store_facts`) stores it again, and while the chat
runs (`ended_at` NULL, `sql/init.sql:326`) the `remember` tool ignores every tombstone. Three changes:
- `store_facts` reads `started_at` too (`:281-283`) and compares `deleted_at >= started_at` (`init.sql:325`): a note
  deleted during a chat is "do not relearn" for that chat (its after-chat work and its `remember` calls) and for any
  other chat running then; a chat that starts after the deletion may learn it again, as today.
- `patient_memory_deleted` (`init.sql:375-381`) gains a nullable `text VARCHAR(160)` column (`ALTER TABLE ... ADD
  COLUMN IF NOT EXISTS`, as the file does at `:383-387`): `delete_facts` records the deleted pair's newest text
  (`RETURNING` gains `text, created_at`, `:469-471`), and `store_facts` also skips a candidate of the same kind whose
  folded text equals a tombstoned text in the window. This catches the extractor choosing another subject for the
  same note (the research's remaining risk). The retention purge (`conversation_retention_job.py:29`) and the ledger
  replay need no change.
- `after_chat.process` reads the chat's tombstones since `started_at` (one query) and passes them to
  `memory.after_chat_call` (`memory.py:373-413`) as `forgotten`; the user content gains one line, 「這次對話中已刪除、不要
  再記：subject: text」 / "Forgotten in this chat (never note again): subject: text", so the model is told as well.
  The server-side check is the guarantee for the same pair or text; a note the model paraphrases with a new subject
  and new words could still slip through: accepted, the patient can delete it in the app, and the notice's 7-day
  wording stays research gap B.
- `remember` after `forget` of the same pair in the same chat is therefore refused (`stored` 0 → `not_stored`,
  `tool_registry.py:875-877`); the MEMORY section says so.

**BACKEND_INSTRUCTION, MEMORY (added):** "forget(query, category) when the patient asks Reachy to forget or not
remember a memory note (never a reminder: that is cancel_reminder). query is the note in their words, its key words.
It deletes only when exactly one note matches those words: deleted=1 means it is gone (say what was forgotten in
its words); reason not_found means there is no such note (say so, never pretend to forget); several lists the
candidates (ask which one, then call again with its words); in_app_only means the patient typed that note in the
app (or it is their name) and it can only be removed there, under Conversations; memory_consent_off means memory is
off and notes can be deleted in the app. A note forgotten in this chat cannot be remembered again until the next
chat. Never say 'never again' or 'for 7 days': say Reachy forgot it, and that if they tell it again later it may
remember it again."

### move_head

**When.** 「看左邊」, 「往右看」, 「抬頭」, 「低頭」, 「看前面」, 「頭轉回來」, "look left", "look to the right", "look up",
"look down", "look straight ahead". GPT-Live's call only.

**Server.** `ToolRegistry.move_head(direction)`: `direction` in `left, right, up, down, centre` (also accept
`center`, `front`, `forward` → `centre`), else `ToolError("invalid_direction")`; refused with `ToolError("help_line")`
when `hooks.help_line_active()`; `{"type": "move_head", "direction"}` → the robot's result.

**Robot.** `_move_head_request` → `hardware.move_head(direction)` on the motion worker (one job, `MAX_PENDING_EMOTES`
applies, `reachy_hardware.py:168-171`); refused with `motion_busy:<move>` while a recorded move or sweep runs
(`running_move()`), `saying_help_line` while the chat says a `STILL` line (`self._say_active()` with the player
holding `STILL`, `live.py:94`, `:183-184`), `gestures_off` / `no_motion` as the emotes. The move: read the head's
angles (`robot.head_angles()`: `moves.head_angles(self._mini.get_current_head_pose())`, `bridge/moves.py:30-36`,
`reachy_mini.py:950-959`, as `_start_pose` reads them, `media.py:556`), add the step (`MOVE_HEAD_STEP = {"yaw": 20.0,
"pitch": 15.0}`; left = +yaw, right = −yaw, up = −pitch, down = +pitch: positive pitch looks down, `media.head_pose`,
`media.py:107`), clamp to `MOVE_HEAD_LIMIT = {"yaw": 40.0, "pitch": 25.0}`, keep roll 0, and
`robot.goto(head_pose(yaw, pitch), antennas_deg=(0, 0), duration=0.6)` (`media.py:570-580`: blocks for the move,
halts the gesture stream), then `robot.place()` (a one-liner: `self._gestures.placed()`), **not** `hold_head` (the
head must stay where it looked). `centre` is `robot.hold_head()` (`media.py:582-585`: neutral, placed). Result
`{"direction", "yaw_deg", "pitch_deg", "step_deg", "at_limit": bool}` (the angles after the move, rounded). The
chat sets `self._tracking_resume_at = now + MOVE_HEAD_HOLD_SECONDS` (8.0) so the follower does not undo the move at
once; when tracking is off (or unavailable) the chat's `track()` poll calls `hardware.recentre()` (`hold_head` on
the motion worker) at that time instead, so the head never stays off-centre for more than 8 s unless the patient is
there to follow; `centre` resumes tracking at once.

**BACKEND_INSTRUCTION, HEAD MOVES:** "move_head(direction) for look left / right / up / down / centre (straight
ahead): one small step each (about 20° sideways, 15° up or down), relative to where the head is; at_limit true means
it is as far as it goes that way. Reachy looks back at the patient, or to the centre, by itself a few seconds later.
sweep_look() for 'look around' / 'have a look around the room': about 12 seconds; its answer comes when the sweep is
done (status done, or cancelled when the patient said stop). Both are refused with help_line while Reachy says its
help line, with motion_busy while a dance or emotion move runs (ask it to stop first), and with gestures_off when
the robot's gestures setting is off: say so."

### sweep_look

**When.** 「看看四周」, 「看一下周圍」, 「環顧一下」, 「你看看房間」, "look around", "have a look around", "scan the room".
GPT-Live's call only.

**Server.** `ToolRegistry.sweep_look()`: refused with `help_line` as `move_head`; `{"type": "sweep_look"}` → the
robot's result when the sweep is done; the registry sets `_dance_at = now` (`tool_registry.py:934`) so a 停 / "stop"
during the sweep is a stop (`tool_intents.stops` with `dancing` true, `:307-336`) and `stop_dance` cancels it; it
does not set `_danced` (「再跳一次」 after only a sweep is no dance, `:1016-1018`).

**Robot.** `_sweep_look_request` → `hardware.sweep_look()`: refused like `move_head` (`motion_busy`,
`saying_help_line`, `gestures_off`, `no_motion`); otherwise a job on the motion worker stored in the `_dance` slot
(`(future, stop_event, "sweep_look")`, `reachy_hardware.py:79`, `:264-267`), so `stop_dance()` (`:285-298`) and
`shutdown()` (`:324-333`) end it. The job: for each `step` in `range(len(SCAN_YAWS_DEG))` (`media.py:24`), stop if
the event is set, else `robot.look_around(step)` (`:587-588`: a 1.5 s `set_head`); then `robot.hold_head()` always
(neutral, placed; the follower re-aims after). The request waits for the job (`await asyncio.wrap_future(future)`)
and answers `{"status": "done" | "cancelled", "steps": n_done, "seconds": round(elapsed, 1)}`. The chat pauses
tracking while the job is pending (`pending_motion_jobs`) and resumes after. A companion pause or the chat's end
cancels the server's waiting tool task (`AgentLiveClient.close`, `agent_bridge.py:291-298`); the robot's job runs to
its end or `shutdown()` stops it (`live.py:428`), and its late answer finds no pending request (`answer()` returns
False, `agent_bridge.py:139-146`): harmless.

### stop_emotion

**When.** The stop phrases of `tool_intents.stops` (`STOP_WORDS`, `:126-128`; `PATTERNS["stop_dance"]`, `:63-65`)
while an emotion move runs, plus 「不要這樣」, 「不要做那個動作」, 「不要動」, "stop that", "stop doing that", "stop
moving" at any time (`PATTERNS["stop_emotion"]`, always on, as 「不要跳舞了」 is for dances). GPT-Live's call or the
backstop.

**Server.** `ToolRegistry.stop_emotion()`: logs `[TOOL] stop_emotion called`; `self._dance_at = self._emotion_at =
−inf`, `self._stopped_at = now` (the hold, as `stop_dance`, `:1270`), `self._intent_runs.pop("dance", None)`;
`{"type": "stop_emotion"}` → the robot's result; then `hooks.posture_cleared()`. The relay's `posture_cleared` sets
`_affect = None`, `_affect_at = −inf`, `_move_pending = None` (`live_session.py:586-589`), so neither the posture nor
the chosen move is sent again until the patient's next turn gives a new reading (`_feel`, `:1528-1538`), and
`_express` sends nothing while `tools.held()` (the hold). `dance` and `play_emotion` are refused with
`stopped_by_patient` during the hold (`:1217-1219`, and the same check in `play_emotion`); `dance_asked`
(`:1020-1025`) ends the hold as today. `forget_intents` (`:1050-1055`) keeps a `stop_emotion` run as it keeps
`stop_dance`'s.

**Robot.** `_stop_emotion_request` → `result = await hardware.stop_dance()` (`reachy_hardware.py:285-298`: cancels a
dance, an emotion move or a sweep within a trajectory step; `idle` when none runs), then `self.robot.express(None)`
(`media.py:505-510`) and `self._expressed = False` (`live.py:361`, `:619`). Answer `{"status": "cancelled" | "idle",
"move": name | null, "posture_lifted": bool}`. No gestures check.

**BACKEND_INSTRUCTION, DANCE (added):** "stop_emotion() when asked to stop an emotion move or a posture (「不要這樣」,
"stop that"); it also stops a dance. Its answer says cancelled (something was stopped) or idle. A dance or an
emotion move asked for within seconds of either stop is refused with stopped_by_patient, as after stop_dance."

**Backstop.** `_want_stop` (`live_session.py:1642-1668`) chooses the tool: `stop_emotion` when
`tools.expressing() and not tools.dancing()`, else `stop_dance` (a dance running or waiting: `stop_dance` stops the
dance and the hold rules are the dance's). `INTENT_LINES["stop_emotion"]` = 「好，我停下來了。」 / "Okay, I've
stopped." (said only on `cancelled`, as for `stop_dance`, `:1880-1882`).

## The messages, in one place

Robot requests (server → robot, each with `request_id`) and answers (robot → server):

| request | answer type | result (ok) | errors (ok false) |
|---|---|---|---|
| `{"type": "volume", "level", "change"}` | `volume_result` | `{volume, previous, read_back, at_limit, requested}` | `no_robot`, `volume_unreadable` (set sent, nothing read) |
| `{"type": "head_tracking", "enabled"}` | `head_tracking_result` | `{enabled, available, reason}` | `follow_face_off`, `no_motion` |
| `{"type": "robot_status"}` | `robot_status_result` | the status fields above | none (a missing part is null) |
| `{"type": "move_head", "direction"}` | `move_head_result` | `{direction, yaw_deg, pitch_deg, step_deg, at_limit}` | `no_motion`, `gestures_off`, `motion_busy:<move>`, `saying_help_line`, `invalid_direction` |
| `{"type": "sweep_look"}` | `sweep_look_result` | `{status: done \| cancelled, steps, seconds}` | `no_motion`, `gestures_off`, `motion_busy:<move>`, `saying_help_line` |
| `{"type": "stop_emotion"}` | `stop_emotion_result` | `{status: cancelled \| idle, move, posture_lifted}` | `no_motion` |
| any type with a `request_id` the robot does not know (0.17.0 on) | `unknown_request` | — | `unknown_request:<type>` |

Server-only tools: `go_to_sleep` → `{status: ending, after_goodbye, grace_seconds}` or `chat_already_ending` /
`no_relay`; `forget` → `{deleted, forgotten, remaining_count, remaining_kinds}` or `{deleted: 0, reason: not_found |
several | in_app_only | memory_consent_off, candidates?, note?, hint?}`.

Registry refusals shared by the six robot tools: `robot_too_old` (the version gate), `robot_cannot_do_that`
(`unknown_request`), `robot_did_not_answer` (the link timeout), `<tool>_failed: <robot error>`.

## Timeouts

| tool | `TIMEOUTS` (registry) | link timeout | why |
|---|---|---|---|
| `go_to_sleep` | 3.0 | none (no robot) | a hook call |
| `volume_control` | 6.0 | `VOLUME_SECONDS = 5.0` | the command plus two 2 s read-back `GET`s |
| `head_tracking` | 4.0 | `TRACKING_SECONDS = 3.0` | a flag on the robot |
| `robot_status` | 6.0 | `STATUS_SECONDS = 5.0` | one 2 s `GET` for the volume |
| `forget` | 12.0 | none | two database calls of `DB_TIMEOUT_SECONDS` (5 s each, `tool_registry.py:206-214`) plus the host ledger |
| `move_head` | 6.0 | `MOVE_HEAD_SECONDS = 5.0` | one 0.6 s goto behind at most the queued emotes |
| `sweep_look` | 18.0 | `SWEEP_SECONDS = 16.0` | 8 × 1.5 s plus the 0.8 s return (`media.py:583`) plus queueing |
| `stop_emotion` | 6.0 | `EMOTE_SECONDS = 5.0` (`agent_bridge.py:49`) | as `stop_dance` (`:121`) |

`sweep_look`'s 16 s holds `_tool_lock` (`agent_bridge.py:245`) for GPT-Live's other calls meanwhile; the backstop's
stop does not take the lock (`run_intent`, `tool_registry.py:1027-1037`), so 停 still stops the sweep at once.

## Compatibility

- **Robot 0.16.0 with the new server.** The six robot tools are refused by the version gate before any message is
  sent (`robot_too_old`), and GPT-Live is told to say Reachy needs an update. Should the gate be wrong (a version
  string the parser cannot read counts as old), the link timeout still bounds a request the robot ignores
  (`live.py:539-540` logs and never answers today) at `robot_did_not_answer`. `go_to_sleep` and `forget` work with
  any robot.
- **Robot 0.17.0 with an older server.** It answers nothing new unasked; `head_tracking` defaults to on, so the
  follower runs in the chat on the new robot with any server that sends camera replies (every companion chat does).
- **The static `TOOLS_SCHEMA`** goes to every chat (`gpt_live_config.delegation`, `:477-482`); no per-robot
  filtering (the research's optional recommendation): the gate answers instead, and the backend's wording covers it.

## Tests to add

### Server (`tests/`)

- `test_tool_registry.py`: the schema test (`:216-236`) counts 33 and checks the new properties; the fake `Robot`
  (`:109-137`) gains `volume`, `head_tracking`, `robot_status`, `move_head`, `sweep_look`, `stop_emotion` recording
  calls in `emotes`; new tests: each tool's validation (`invalid_direction`, `level_or_change_required`, the 20-100
  clamp), the version gate (`robot_too_old` with `robot_version=(0, 16, 0)`, allowed at `(0, 17, 0)`, `None` is old),
  `go_to_sleep` with and without hooks, `stop_emotion` setting the hold (`dance` and `play_emotion` refused
  `stopped_by_patient` after it) and calling `posture_cleared`, `sweep_look` setting `_dance_at` but not `_danced`,
  `expressing()` after `note_move()`, and `forget` on the `consent_on` fixture (`:187-191`) and the `backend()` fake
  (`:151-153`) with `memory.current_facts`, `memory.delete_facts` and `deletion_ledger.append_host_file`
  monkeypatched (as `:631-656` do): consent off, `not_found`, `several`, `in_app_only` for a typed note and for
  `name`, `category` narrowing, the one-match delete (the appends happen after the commit, every call carries `u_id`
  7, the log never holds the text), and the hook `note_forgotten` called once.
- `test_live_agent_tools.py`: with `_sent` (`:70-81`), the link's six new request shapes and their `ok` / error
  answers, `unknown_request` → `robot_cannot_do_that`; with `relay()` (`tests/test_live_tools.py:53-54`), the relay
  routing each new `*_result` to the link (as `:114-131` does for `dance_result`).
- `test_live_tools.py`: `test_the_tool_note_names_...` (`:137-151`) gains the new sections (`SLEEP`, `VOLUME`, `HEAD
  TRACKING`, `ROBOT STATUS`, `HEAD MOVES`) and the delegation phrases in both languages.
- `test_agent_bridge.py`: the count at `:67` becomes 33.
- `test_live_tool_backstop.py` (the harness `tests/test_live_relay_chat.py`: `world` `:210`, `_begin` `:248`,
  `_step` `:277`, `_live` `:283`, `_said` `:289`, `_reachy` `:293`, `_sound` `:297`; the `Link` fake `:39-57` gains
  the new methods, the `Backend` fake `:60` gains `forget`): the `sleep` intent runs `go_to_sleep` after the grace
  and the chat ends `goodbye` 3 s after GPT-Live's line, and 6 s after the result when it says nothing; 「我要去睡覺了」
  runs nothing; 「大聲一點」 → `volume_control(change=up)` and the line names the level; 「不要一直看我」 →
  `head_tracking(false)`, 「看著我」 → `head_tracking(true)`, "don't look at me" never runs `describe_view`, plain "look
  at me" still does; 停 during an emotion move (the relay sent an `express` with a move) runs `stop_emotion` at once
  and a dance within 10 s is refused; `go_to_sleep` is never run by a keyword while a risk hold stands; the new rows
  of the intent table (`:262-285`).
- `test_live_expression.py` (the affect harness, `_expressions` `:18`, `_turn` `:22`): after `stop_emotion` the
  posture is not sent again on the next sad turn within the hold, and is sent again after it.
- `test_live_relay_chat.py` or `test_live_safety.py`: `go_to_sleep`'s ending follows the goodbye path (`end {reason:
  goodbye}`, the after-chat work started, as `test_a_goodbye_ends_the_chat_3_s_after_gpt_lives_answer`, `:622`).
- `test_after_chat.py`: `test_tombstoned_subject_is_not_relearned` (`:212-215`) and the fake at `:57-58` gain the
  `started_at` comparison (a tombstone between `started_at` and `ended_at` blocks; one before `started_at` does not),
  the text match (same kind, same folded text, another subject: skipped) and the "forgotten" line in the prompt's
  user content.
- `test_api_memory.py`: `test_delete_fact_with_slash_and_cjk_subject` (`:211-233`) asserts the tombstone upsert now
  carries the text; `test_every_route_filters_by_the_authenticated_user` (`:270`) unchanged.
- `test_live_voice_docs.py`: unchanged, but it must pass after the CLAUDE.md words are written.

### Robot (`medcare_reachy/bridge/tests/`)

- `fakes.py`: `FakeRobot` (`:30-154`) gains `set_volume` / `get_volume` (a settable `volume`, an error switch),
  `head_angles`, `place`; `FakeLiveChat` in `test_session_live.py` (`:41-87`) gains `track`, `follow` and `status`
  records.
- `test_reachy_hardware.py` (`hardware()` `:23-24`, `_chat_with_hardware` `:119-123`): `move_head` steps and clamps
  (+20°, −20°, the limit, `centre` → `hold_head`, `place` after a step), `sweep_look` runs the eight `look_around`
  steps then `hold_head` and answers done, a stop during it answers cancelled, `motion_busy` while a dance runs,
  `track` runs the follower's `update` on the motion worker and skips it while a job is pending, `running_move`.
- `test_live.py` (the `Chat` harness, `:59-92`; `until`, `:51`): the request / answer shape of each new type over
  the fake socket (as `test_the_server_dance_requests_are_answered_and_refused_like_emotes`,
  `test_reachy_hardware.py:332-362`); `unknown_request` for a type with a `request_id` and nothing for one without
  (`test_messages_it_does_not_know_are_ignored`, `:526`, changes); `volume` clamps at 20 and 100 and reports
  `at_limit`; `head_tracking` on / off and `follow_face_off`; `saying_help_line` for `move_head` during a `STILL`
  line, and a running sweep cancelled by a `say` with reason `risk`; `track` paused while GPT-Live's sound plays,
  while a say line plays, while a job is pending and during the drain, resumed with a fresh `follow.start()`;
  `MOVE_HEAD_HOLD_SECONDS` after a step, then `recentre` with tracking off; `stop_emotion` lifts the posture
  (`express:None` in `robot.calls`) and answers idle / cancelled; `robot_status` fields (no name, no token).
- `test_expression.py` (`:130-200`): a `stop_emotion` request during `play_emotion` cancels the move and lifts the
  posture.
- `test_session_live.py`: the session feeds the chat the latest box each poll and hands it `follow` and `status`;
  the dose watch's follower is not started during CHECKIN.
- `test_follow.py` (`:23-72`) and `test_gesture_recenter.py`: `look_at` leaves the robot placed (a gesture after it
  starts from the measured pose).
- `tests/test_app.py:393`: `"0.17.0"`.

## Docs

- `CLAUDE.md`, live voice section (`:188-233`): one bullet "**Eight more agent tools (17 Oct 2026, robot app
  0.17.0)**" after the "stop_dance stops at once" block (`:223-227`, before "Settings" at `:228`), naming each tool,
  its backstop phrases in short, the volume floor, the tracking rules, the forget rules and the relearn fix (the
  `started_at` tombstone rule and the tombstone text), the version gate and `unknown_request`. Only names that exist
  in `app/` in backticks (the docs test).
- `medcare-reachy-bridge/README.md`: the live section (`:136`) lists the new request types and the tracking
  behaviour; "Status" (`:191`) gets the 0.17.0 line.
- The robot's `live.py` module docstring (`:8-24`) lists the new types.

## Left to the builder

- The exact Chinese and English phrase tables for the three backstop intents and their guards, and for
  `PATTERNS["stop_emotion"]`; expect review rounds, as the stop phrases needed (`tool_intents.py:13-19`). Start with
  the lists in this document.
- The exact `BACKEND_INSTRUCTION` and `LIVE_TOOL_NOTE` sentences (the content is fixed above; the wording is yours),
  and the `INTENT_LINES` texts in both languages.
- The robot check that the daemon's volume applies to the chat's own playback (volume_control's note); if not, the
  software-gain fallback.
- `MOVE_HEAD_STEP`, `MOVE_HEAD_LIMIT`, `MOVE_HEAD_HOLD_SECONDS`, `EMOTION_ACTIVE_SECONDS`: the numbers above are
  starting points; adjust after one robot session, each is one constant.
- Whether the forget match rule's word lengths (2 CJK, 3 Latin) are right for the notes real chats produce; one
  look at the stored notes decides.
- How the Runner's start time and the camera snapshot reach the chat's status callable (a kwarg on `SlotSession`,
  or a callable on the Runner).
- Log wording, test names, the order of commits (server first: the schema, the registry gate and hooks, then the
  tools; robot: the facade methods, the hardware methods, then the chat branches, then the session wiring; the
  memory change last, with its tests).

## The builder check and Claude's rulings (17 Oct 2026)

A builder read this design cold (workflow `wf_db3505f1-075`) and found the code claims sound. It raised five
questions where a wrong guess changes what the patient sees. Claude ruled on each (not the user's decision; the
user approved the tool list on 17 Oct and left the details to Claude). These rulings win over the sections above
where they differ.

1. **Stop words while an emotion move may run.** The everyday answers 「好了」, 「好啦」, 「夠了」, 「夠啦」 as a whole
   clause count as a stop **only while a real dance runs** (`tools.dancing()`), never in the expressing window, so a
   patient answering GPT-Live with 「好了」 after an automatic move does not stop anything. In the expressing window
   only an explicit stop counts (停 / 停下來 / 不要動 / 別動 / "stop", the always-on forms). The expressing window
   should follow the robot's real running move where the robot reports one, with `EMOTION_ACTIVE_SECONDS` as the
   upper bound. **A stop that finds nothing running (answer `idle`) sets no hold**: the 10 s refusal of dances and
   emotions follows only a stop that stopped something.
2. **Head tracking against a concern / joy posture while Reachy listens.** The tracked head wins for where the head
   points; the posture keeps its other parts (antennas, body) while tracking holds the head. `set_affect` must not
   ease the head back to neutral while tracking holds it, and the follower re-aims after any posture change (its
   deadband is measured against the head's real pose, not its last target). The patient should never see "it
   glanced at me and looked away" while Reachy is being kind.
3. **head_tracking when the robot cannot follow** (`follow_face` off, no motion): the answer is `ok: true` with
   `{"enabled": false, "available": false, "reason": "follow_face_off" | "no_motion"}`, never an error, so GPT-Live
   can say in plain words that it cannot follow the patient right now. The messages table, the result shape, the
   instruction sentence and the `tracking_unavailable` line all use this one shape.
4. **A stop must never wait behind a long tool.** `stop_dance` and `stop_emotion` run outside
   `AgentLiveClient._tool_lock` (as the backstop's `run_intent` already does), so GPT-Live's own stop during a
   16 s `sweep_look` reaches the robot at once. Within the share window, `stop_dance` and `stop_emotion` count as one
   stop: a backstop run of either answers GPT-Live's call of the other, so GPT-Live never hears `idle` right after
   Reachy visibly stopped.
5. **go_to_sleep after a goodbye word in the same turn** (「你去睡吧，再見」): `request_goodbye` re-arms the ending:
   the next sound after the tool's answer counts as GPT-Live's goodbye, with a new fallback deadline. The ending may
   come up to 6 s later than the plain goodbye would; that is accepted, because the alternative cuts off the words
   GPT-Live says about going to sleep.

**Next step for this design:** an implementation plan (superpowers writing-plans), after the same-voice plans are
finished and merged into one branch, against that finished code.
