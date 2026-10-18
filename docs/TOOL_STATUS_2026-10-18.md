# GPT-Live agent tools: status from the code (18 October 2026)

What was checked: the server on `tool-fixed-version-02` at `8826c92` and robot app 0.16.0 at `c56bbe2`. Both full test suites were run, plus the live robot tests of 17 Oct (00:39-01:02, 12:44-12:56 and 21:49-22:24 Taipei). Every status comes from the code, the tests, the logs or read-only database queries. Nothing was taken from the docs alone.

**Summary:** of the 25 tools GPT-Live is offered, 7 work on the robot, 12 work in tests only, 6 are partial, 0 are broken and 0 are not built. The 9 planned names are not tools yet: 8 are not built and 1 is skipped.

How to read the evidence:
- All times are Taipei time.
- The robot journal is stamped +01:00, so Taipei time is the journal time plus 7 h. The brief's "minus 8 h" rule does not apply to it.
- A log line "live agent tool X: ok" only means the tool returned without an error (agent_bridge.py:253). A failure carried inside the answer is still logged as "ok".

## All 25 tools in TOOLS_SCHEMA

| Tool | What it does (short) | Status | Proof | Note |
|---|---|---|---|---|
| `camera` | Takes one robot camera frame; returns only frame info (id, size) | ✅ works on the robot | Live: 21:58:30 and 21:58:51, as the first step of describe_view (server-now.log:203, :256). Tests: test_tool_registry.py, test_agent_bridge.py; robot test_reachy_hardware.py | Proven only inside describe_view: GPT-Live never called `camera` by name. Called alone, it tells GPT-Live nothing about the view. The robot logs only camera failures. |
| `identify_medicine` | Reads a medicine label on camera and matches it to the patient's medicines | ⚠️ partial | test_tool_registry.py (fake OCR). Never live | Needs LIVE_VISION_CLOUD=1. It uses the prescription OCR, which refuses a label with no dose schedule (ocr_service.py:259-267), so a plain box always comes back `uncertain`. The name match is exact: 'Allegra 60mg' does not match 'Allegra', so no medication_id comes back. Worst case is 40 s, above its 36 s timeout. |
| `verify_prescription` | Checks that a dose-N or med-N id is on this patient's prescription | 🧪 works in tests, not yet tried on the robot | test_tool_registry.py::test_verify_prescription_takes_dose_and_medicine_ids (its only test). Its SQL ran read-only on the real database | Never live. It needs an id, which identify_medicine rarely gives. Does not need cloud vision. |
| `verify_medication` | "Is this my medicine?": camera label check against the expected medicine | ⚠️ partial | test_tool_registry.py, test_medication_events.py. Never live: there has never been a dose_event row | Needs LIVE_VISION_CLOUD=1. With it off, it still queues a family LINE "label could not be read" message, though no picture was read. Same OCR schedule rule: a plain box comes back `uncertain`. |
| `describe_view` | Answers a question about what the camera sees (Gemini) | ✅ works on the robot | Live: 21:58:30 'ok in 4957 ms' (「只看到下巴和嘴唇」) and 21:58:51 'ok in 4607 ms' (shirt colour right). Tests: test_tool_registry.py, test_live_tool_backstop.py | Needs LIVE_VISION_CLOUD=1, on since 21:57:19. With it off (21:50, 66 and 59 ms), the log said "ok" but Reachy said it could not see. The "look" backstop phrases would have missed all 3 real questions. Its prompt always asks for Traditional Chinese. No notice covers sending frames to Gemini: test installations only. |
| `get_medication_schedule` | Doses now or today, with their state | ✅ works on the robot | Live: 22:00:18 'ok in 46 ms'; the reply matched the database. Test: test_tool_registry.py | The same turn also set off the stray web search (see the live findings). |
| `check_medication_due` | The current slot's doses, judged by overdose protection | ✅ works on the robot | Live: 22:23:11 (13 ms) and 22:23:27 (7 ms); also 00:39, 01:02 and 12:49. Tests: test_tool_registry.py, test_live_tool_backstop.py, test_dose_status.py | It offers only the latest due slot, and a slot is 5 min wide (dose_status.py:53). At 22:23, dose 4 (18:07) was never offered and stayed missed. |
| `start_pill_intake_detection` | Opens a camera watch for taking a pill | 🧪 works in tests, not yet tried on the robot | test_tool_registry.py, test_record_taken.py; robot stream tests | Never live. It watches the chat's 2 fps stream, but the detector is tuned for about 15 fps (intake_detection.py:93-96). It also runs only while the patient is verified. Whether it can see an intake at 2 fps has not been measured. |
| `get_pill_intake_result` | Reads the watch's result | 🧪 works in tests, not yet tried on the robot | test_tool_registry.py, test_medication_events.py | Never live. Same 2 fps limit. An `uncertain` or `not_taken` answer sends family a review after 12 s. |
| `record_medication_taken` | Files the dose for family to confirm: a LINE request plus a video clip | ✅ works on the robot | Live: 22:23:28 'ok in 18 ms' (dose_confirmation 90e39711; clip 1421f976, 40 frames). Also 12:49:39: LINE accepted the request at 12:49:43 (outbox 46). Tests: test_record_taken.py, test_request_video.py | At 22:23 family got nothing because of LINE's monthly limit (outbox 49-50, error 429), not the tool. Reachy praised the patient anyway. Both live claims were 「我吃完了」 or 「我刚吃完了」, and only GPT-Live's own call recorded them; the 「我吃完藥了」 backstop has never run live. |
| `get_current_emotion` | The robot's combined mood reading plus the server's face reading | 🧪 works in tests, not yet tried on the robot | test_tool_registry.py, test_agent_bridge.py; robot test_reachy_hardware.py, test_multimodal.py | GPT-Live has never called it. |
| `get_emotion_history` | Past moods (dose emotions and chat moods) | 🧪 works in tests, not yet tried on the robot | **No repo test.** Only the audit's scratch test (1 passed) and its 2 queries run read-only on the real database | Never live. A repo test should be added. |
| `trigger_antenna_emote` | Plays one of 6 antenna and head gestures | ✅ works on the robot | Live: 22:23:30 'ok in 4 ms' (after the record), also 12:49:40 and 01:02:21. Tests: test_agent_bridge.py; robot test_reachy_hardware.py | "ok" means the robot queued the gesture. The robot logs only failures, so the movement itself is not in any log. |
| `remember` | Stores a fact about the patient (needs memory consent) | ⚠️ partial | test_tool_registry.py, test_memory.py. Never live: all 7 chat facts came from the after-chat step | Category `event` always fails with `invalid_fact`: the schema has no event_date, and validate_fact needs one (memory.py:101-108). The same fact in other words becomes a new row. |
| `get_patient_memory` | Searches the stored facts | ⚠️ partial | test_tool_registry.py::test_get_patient_memory_searches_the_current_facts. Never live | A Chinese query must be a literal piece of the fact, because the search splits only on spaces: 「孫子」 finds the fact, 「孫子住哪裡」 does not. The chat's memory block covers the common case. |
| `notify_family` | Sends family a text note on LINE (at most 3 per chat) | 🧪 works in tests, not yet tried on the robot | test_tool_registry.py, test_outbox_dispatcher.py. Never live: there has never been a live_agent_note row | LINE's monthly limit blocks it now, but it still answers queued=true, so GPT-Live would say family were told. |
| `emergency_alert` | Safety alert to every family contact, plus the attention gesture | 🧪 works in tests, not yet tried on the robot | test_tool_registry.py::test_emergency_alert_raises_the_safety_alert_and_the_attention_gesture, test_conversation.py. Never live: the only safety_alert (13 Oct) came from the after-chat summary | Blocked by the LINE limit, but it answers alerted=true. It sets risk_flag, so a fall is handled like self-harm: a text fallback gets the help line and ends, with no summary and no memory. It does not end the chat or say the help line itself. A fall has no backstop. |
| `get_current_time` | The time now, here or in another place | 🧪 works in tests, not yet tried on the robot | test_current_time.py, test_live_tool_backstop.py | No patient has asked the time in a live chat yet. |
| `search_web` | Web search through OpenAI's hosted search | ⚠️ partial | Live: 21:59:04 'ok in 5434 ms' and 21:59:32 'ok in 8220 ms'. Test: test_web_search.py (fake HTTP) | GPT-Live's own searches work on the robot. The backstop misfired at 22:00:20 (see below). The `sources` list was empty in 3 of 3 runs. |
| `set_reminder` | A reminder that Reachy says aloud at the set time | ⚠️ partial | Live: scheduled at 22:00:46, fired at 22:02:00, said in the live voice at 22:02:31 (eca1cc28 fired via robot_live). Tests: test_reminders.py, test_live_speak.py | It works end to end on the robot. But the "my medicine" form our own schema and instruction ask for (what=null, no name or position; gpt_live_config.py:419-420, :219-220) is always refused as `nothing_to_remind` (reminders.py:207, :216-217). That happened at 22:00:44, and the log said "ok". It worked only because GPT-Live retried with what='吃藥'. |
| `cancel_reminder` | Cancels one reminder, or all of them | 🧪 works in tests, not yet tried on the robot | test_reminders.py::test_cancel_stops_the_timer_and_marks_the_row (cancel-all only) | Never live. No test cancels by id, and GPT-Live learns an id only within the same stretch. Cancelling a reminder already handed to the robot leaves its check-in task, so Reachy would wake and say the lead-in with nothing after it. |
| `get_weather` | Weather for a place in Taiwan (Nominatim, then Open-Meteo) | 🧪 works in tests, not yet tried on the robot | test_weather_lookup.py (fake web), test_live_agent_tools.py. A real lookup('台中') from this PC answered ok | Never seen live. No backstop. |
| `dance` | Plays a recorded dance (2 moves by default) | ✅ works on the robot | Live: 22:23:45 'ok in 112 ms'; also 12:50:07, 12:50:29 and 00:39:54. Tests: test_live_agent_tools.py, test_live_tool_backstop.py; robot test_reachy_hardware.py | The robot journal ends at 22:05, so there is no robot line for the 22:23 dance. The dance backstop has never fired live: every live dance was GPT-Live's own call. |
| `stop_dance` | Stops the dance at once | 🧪 works in tests, not yet tried on the robot | One live run: at 12:50:18 the backstop ran it and the robot answered ok (livetest-4.log:614-621). Tests: test_live_tool_backstop.py; robot test_reachy_hardware.py | **Changed from ✅.** The robot also answers ok when nothing was running ({status: idle}, reachy_hardware.py:285-298), and the server does not log which answer came. So the live run does not prove a dance was cut. The "0 ms" was GPT-Live's call reusing the backstop's run. It has never run on tonight's build. |
| `play_emotion` | Plays one of 17 recorded emotion moves, with a posture | 🧪 works in tests, not yet tried on the robot | test_live_agent_tools.py; robot test_expression.py | GPT-Live has never called it. The robot's express handler did run live through the automatic body-language path (journal 21:51:52-21:52:00, downcast1), but the tool's own answer path never has. |

Corrections to the old sheet (finished_tool-v1.md):
- **stop_dance:** changed from ✅ to 🧪 (reasons in the table). The words that started the next dance were 「跳給我看」, not 「繼續跳」.
- **record_medication_taken:** the "30 ms" was the tool's own run time; LINE accepted the request about 4 s later. "「我吃完藥了」 always reaches the tool" is only half proven, because the backstop half never ran live.
- **"The other 22 tools":** only 6 of them ran live (the camera step only inside describe_view), and 6 are partial.

## Names that are not tools yet

None of these 9 names is in TOOLS_SCHEMA or in any .py file. A call by one of these names would answer `unknown_tool` (tool_registry.py:1088-1090).

| Name | Status | What exists today to build on (file) | Planned |
|---|---|---|---|
| `go_to_sleep` | 🔲 not built | The goodbye ending: GOODBYES (conversation.py:82) and _finish('end','goodbye') (live_session.py:1049-1050). On the robot: ReachyRobot.sleep (media.py:686-689) | Robot app 0.17.0 (approved 17 Oct) |
| `volume_control` | 🔲 not built | Nothing in the app. SDK 1.11.0 has SetVolumeCmd (reachy_mini/io/protocol.py:386-390) and the daemon has /api/volume/set; both were read in the reachy-mini-tcu venv copy, not on the robot | Robot app 0.17.0 (approved 17 Oct) |
| `head_tracking` | 🔲 not built | FaceFollower (follow.py:25-77), which today runs only during the dose watch (session.py:1126-1132), and the server's target_box | Robot app 0.17.0 (approved 17 Oct) |
| `robot_status` | 🔲 not built | Heartbeat status_detail (api_device.py:748-760, runner.py:649-652) and /api/reachy/status (api_reachy.py:48-79). Volume, tracking, running move and microphone state are not collected anywhere yet | Robot app 0.17.0 (approved 17 Oct) |
| `forget` | 🔲 not built | In-app deletes only: api_memory.py:95-108 and memory.delete_facts (memory.py:461-479). Gap today: a note deleted during a chat can be learned again from that same chat (memory.py:287-289) | Robot app 0.17.0 (approved 17 Oct) |
| `move_head` | 🔲 not built | ReachyRobot goto, set_head and hold_head (media.py:563-585), and the motion worker's _goto (reachy_hardware.py:196). Gap today: English "look up" goes to the search backstop (tool_intents.py:103) | Robot app 0.17.0 (approved 17 Oct) |
| `sweep_look` | 🔲 not built | ReachyRobot.look_around (media.py:587-588), used only in the search before a task (session.py:343-346). It is not in the stoppable motion slot yet | Robot app 0.17.0 (approved 17 Oct) |
| `stop_emotion` | 🔲 not built | The robot's stop_dance already cancels an emotion move (reachy_hardware.py:285-298, checked with fakes). But the posture stays, and the server never treats an emotion move as running | Robot app 0.17.0 (approved 17 Oct) |
| `idle_do_nothing` | 🔲 skipped | Nothing, on purpose | Not to be built: Pollen retired it (the user's decision) |

The user's face-lock idea ("can you see me" / "look at me", where the head locks onto the patient) would become part of `head_tracking`. Today "look at me" runs describe_view, a camera look (tool_intents.py:90), so the two sets of phrases must be kept apart.

## Found in the live tests, not fixed yet

- **Reachy stays silent when the camera does not recognise the patient.**
  - The server logged 46 lines of "the patient was not seen in the last 15.0 s; GPT-Live stays paused" in chat 579eedf3, from 22:02:47 to 22:20:43.
  - The robot journal has "the patient's voice did not restart GPT-Live: not seen by the camera lately" at 21:51:11 (chat 75372f55), 22:02:48 and 22:03:04.
  - The robot only logs `not_seen` and says nothing (live.py:517-519; live_session.py:1215-1219). So the patient talked and got no answer.
  - Identity kept flipping between verified and mismatch every few seconds. The patient spoke next at 22:23.
- **Stray backstop web search at 22:00:20.**
  - The patient said 「…你可以帮我查一下吗」 (22:00:14), then asked about medicine in a separate turn (22:00:18).
  - The backstop searched the fragment "]哎,然后你可以帮我查", and its answer was mixed into Reachy's medicine reply at 22:00:35.
  - Cause: the search words are read once per turn and never checked again, a new turn does not drop the waiting search, and GPT-Live's get_medication_schedule call does not count as an answer (live_session.py:1597-1608, :1621-1638).
- **LINE's monthly limit (200/200) blocks every family message.**
  - Since 22:23 every LINE push has failed with 'LINE API 429: You have reached your monthly limit.': outbox 49 (the dose request), 50 (its video) and 51 (the 23:24 one-hour follow-up of the dose request), now at 18, 18 and 12 attempts.
  - The last push LINE accepted was outbox 48, at 13:50 on 17 Oct.
  - Until the quota resets or the plan changes, no confirmation request, note or safety alert reaches family. The tools still answer queued or alerted.
- **set_reminder:** the documented medicine form was refused at 22:00:44 and logged as "ok" (see the table).
- **A logged "ok" can hide a failure.** describe_view at 21:50 said "ok" (66 and 59 ms) while Reachy said it could not see. set_reminder's refusal at 22:00:44 also logged "ok". Cause: agent_bridge.py:253 logs whether the call returned, not the answer's status.
- **check_medication_due skipped a dose.** Dose 4 (18:07) was never offered at 22:23 and stayed missed.
- **No family answer has ever come back.** The requests of 01:02, 12:49 and 22:23 all expired. The old sheet's reason (LINE "Use webhook" switched off) could not be checked from here.
- **The request still says the video comes next when it cannot.** Its text promises "the video follows in the next message" even when there is no public link (dose_confirmation.py:55; clip_outlook never checks the link). At 12:49 the video came 5.5 min late. This is old item 2, still open.

Found in the code by this audit and not seen live (details in the table): verify_medication with cloud vision off still messages family; the medicine OCR refuses labels without a dose schedule; remember cannot store an `event`; get_patient_memory misses Chinese questions; emergency_alert sets risk_flag; cancel_reminder leaves a robot task behind; the pill watch at 2 fps has never been measured.

## Test results

- **Server, whole suite:** `3178 passed, 1 warning in 92.86s (0:01:32)`, exit code 0. The warning is a starlette DeprecationWarning, not about any tool.
- **Robot, whole suite:** `1 failed, 798 passed, 6 skipped, 1 warning in 49.77s`.
  - The failure is medcare_reachy/bridge/tests/test_filler_echo.py::test_a_failing_phrase_costs_nothing_but_a_log_line. It tests the "thinking" filler phrase, not a tool, and is the known Windows timing flake: run alone, it passed 3 of 3 times.
  - The 6 skips are missing test resources on this PC (the MediaPipe reference set, the MJPG writer, the emotion model, the full app repo).
- **Targeted tests:** every tool test the auditors ran passed, with no flakes. That includes set_reminder and stop_dance, re-run for this sheet: server 3 passed, robot 2 passed.
- get_emotion_history has no repo test (`pytest -k emotion_history` selects nothing).
- Full suite logs:
  - C:\Users\AIDL-14\AppData\Local\Temp\claude\C--Reachy-Mini\1ff25281-c5da-4818-bbf1-9fd9035013bc\scratchpad\toolaudit\work\server-suite.txt
  - C:\Users\AIDL-14\AppData\Local\Temp\claude\C--Reachy-Mini\1ff25281-c5da-4818-bbf1-9fd9035013bc\scratchpad\toolaudit\work\robot-suite.txt
