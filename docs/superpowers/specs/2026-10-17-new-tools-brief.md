# New agent tools: brief for the design document (17 Oct 2026)

The user approved (Telegram, 17 Oct 13:02) building these GPT-Live agent tools, which exist in Pollen's
conversation app but not in MedAiCarePlus: `go_to_sleep`, `volume_control`, `head_tracking`, `robot_status`,
`forget`, `move_head`, `sweep_look`, `stop_emotion`. `idle_do_nothing` is skipped (Pollen retired it). They are to be
built AFTER the same-voice work (robot app 0.16.0, server branch `same-voice`), so the design targets the code as it
will be after that work: read the same-voice spec and the two plans first (paths below), then today's code.

Sources, in this order:
1. `2026-10-17-new-tools-research.md` beside this file (the raw research results): the earlier research (workflow `wf_fb441483-88d`: what each Pollen tool
   does, the SDK calls, what already exists in our two repos) and the investigation (`wf_df3c0bb1-f2a`). The
   probe scripts and SDK notes stayed in the session's scratch folder on the PC where this was written (not
   committed); the research file quotes what mattered.
2. The same-voice spec and plans (the code these tools will sit on):
   `C:\Reachy_Mini\worktrees\MedAiCarePlus-same-voice\docs\superpowers\specs\2026-10-17-same-voice-design.md`,
   `...\plans\2026-10-17-same-voice-server.md`, `...\plans\2026-10-17-same-voice-robot.md`.
3. Today's code, READ-ONLY (two jobs are committing to these branches right now; read, never edit):
   server worktree `C:\Reachy_Mini\worktrees\MedAiCarePlus-same-voice` (branch `same-voice`; the agent layer is
   `app/services/gpt_live_config.py` TOOLS_SCHEMA + BACKEND_INSTRUCTION, `agent_bridge.py`, `tool_registry.py`
   (ToolRegistry, ServerCareBackend, RelayRobotLink._ask), `tool_intents.py` (the backstop phrase tables),
   `live_session.py` (LiveRelay: _on_robot_message answer types, _want_tool / run_intent backstops, the stop hold);
   robot repo `C:\Reachy_Mini\repo\medcare-reachy-bridge` (branch `same-voice`; `medcare_reachy/bridge/live.py`
   LiveChat._control handles the server's tool requests, `reachy_hardware.py` ReachyMiniHardware, `session.py`,
   `follow.py` FaceFollower, `runner.py` heartbeat).
   The server's main checkout `C:\Reachy_Mini\repo\MedAiCarePlus` is on `agent-tools-fixes` (`26bfae2`), the base
   of `same-voice`; the earlier tool fixes (plan `docs/superpowers/plans/2026-10-16-agent-tool-fixes.md` and its
   `CLAUDE.md` section "GPT-Live agent") explain how a tool call flows: GPT-Live → AgentLiveClient (one call at a
   time under _tool_lock) → ToolRegistry.<name> → RelayRobotLink._ask({type, request_id}) → robot LiveChat._control
   → ReachyMiniHardware → `<type>_result` back → the registry's answer → GPT-Live; plus the relay's backstop that
   runs a tool from the patient's words when GPT-Live does not delegate within the grace.

## Rulings (Claude's calls, binding unless the code shows one is impossible; then say so)

- **go_to_sleep**: GPT-Live calls it when the patient asks Reachy to sleep / rest / go away (「去睡覺」, 「你休息吧」,
  "go to sleep"). It ends the live chat the way a goodbye does (GPT-Live says a short goodbye first, then the chat
  ends with reason `goodbye`, the session's wind-down rules apply, `robot.sleep` as today). No new robot state.
  A backstop intent runs it when GPT-Live does not.
- **volume_control**: sets the speaker volume with the SDK's silent volume command (the research found the exact
  call), clamped to 20-100 percent so the robot can never be muted (the help line must stay audible); relative
  words (「大聲一點」 / 「小聲一點」 / "louder" / "quieter") move by 20 points; absolute (「音量50」) set it. The
  robot answers the new level; the level is reported in `robot_status`. It is NOT persisted across restarts in v1
  (say so). A backstop intent for the relative forms.
- **head_tracking**: a patient-only follower during the live chat: the robot keeps its head on the verified face box
  the server already sends with the camera frames (the same `follow.FaceFollower` the dose watch uses, no new
  detection on the robot). ON by default when a live chat starts; GPT-Live can turn it off/on (「不要一直看我」,
  「看著我」, "stop following me", "look at me"); it pauses by itself during a dance, an emotion move, a `move_head`
  / `sweep_look`, and the SAY/STILL gestures, and resumes after. Never during the help line (still posture wins).
  The tool answers the new state. A backstop intent for both forms.
- **robot_status**: read-only; answers what the robot reports: app version, volume, head tracking on/off, whether a
  dance or emotion is running, camera ok (frames flowing), microphone muted or not, the time since the chat
  started, and the robot's own uptime. No battery (the Reachy Mini is mains-powered). Nothing that names the
  patient. GPT-Live reads it out in plain words.
- **forget**: the patient asks Reachy to forget something (「忘記我剛剛說的」, 「不要記得我住哪」, "forget that").
  The tool deletes the matching memory facts (the app's memory delete; match by the fact's text or kind, patient
  only, memory consent current), reads the memory back to confirm what is left, and answers what was forgotten so
  GPT-Live can confirm it in its own words. **The same-chat relearn gap**: today the after-chat memory extraction
  can learn the fact again from this chat's transcript; the design must close it (for example: a forgotten fact is
  recorded as "do not relearn" for this conversation and the extractor skips it; the spec says exactly how).
  Nothing is deleted without the patient's clear ask; a vague ask gets a question back, not a deletion.
- **move_head**: look left / right / up / down / centre, with the SDK's bounded head goto (the research has the
  ranges), relative to the current pose, small bounded steps (say the degrees), never while the help line is said;
  head tracking pauses for it and resumes.
- **sweep_look**: the existing `look_around()` (the hardware already has it); answers when done; head tracking
  pauses for it.
- **stop_emotion**: stops whatever expressive move runs (a dance or an emotion): `stop_dance` plus `express(None)`
  as the research found; shares the stop hold rules of `stop_dance` (the earlier fixes) so a dance refused after a
  stop stays refused until the patient asks again. The backstop stop phrases that today run `stop_dance` run this
  when an emotion (not a dance) is running.
- **Every tool**: a `TOOLS_SCHEMA` entry with a plain description (Traditional Chinese examples where GPT-Live
  needs them), a `ToolRegistry` method, a `RelayRobotLink._ask` request type with a result type that
  `LiveRelay._on_robot_message` routes, the robot's `LiveChat._control` handler and the hardware method, a
  `BACKEND_INSTRUCTION` sentence where GPT-Live needs guidance (never praise, never promise what the tool did not
  answer), tests on both sides (the fake robot and the fake GPT-Live harnesses that exist), CLAUDE.md's agent
  section and the robot README. Robot app version 0.17.0 (after 0.16.0 same voice). Timeouts per tool in
  `TIMEOUTS`. Robot app 0.16.0 must keep working with the new server (an unknown request type is answered with an
  error the registry turns into "the robot cannot do that yet", never a crash or a hang).
- **Consent**: head tracking and robot_status add no new data use (the camera frames and the robot's heartbeat
  already flow in a chat). `forget` needs the patient's memory consent to be current (as the memory routes do).
- **Out of scope**: GPT-Live seeing the camera; battery; persisting the volume; any change to the dose flow.

## What the design document must contain
Intent and why; goals, non-goals, anti-goals (a help line made inaudible; a tool that moves the head during the
help line; a memory deleted on a vague ask; a chat that hangs on a tool the robot does not know); constraints; the
design per tool (server side, robot side, the message types and their fields, the backstop phrases, the answer
shapes, timeouts); the tests to add on each side (file names that exist today); and "left to the builder".
Mark the rulings above as Claude's calls. Plain simple English. Keep it to what a planner needs.
