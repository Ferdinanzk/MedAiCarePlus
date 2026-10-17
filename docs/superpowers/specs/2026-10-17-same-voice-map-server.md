# Same voice: the server side (MedAiCarePlus), mapped read-only

Mapped on 17 Oct 2026 against `C:\Reachy_Mini\repo\MedAiCarePlus`, branch `agent-tools-fixes`, HEAD `ba9a04f`
(another job is committing: `ba9a04f` and `5943a0e` landed while this map was made; they changed only
`live_session.py` from line 1680 on, `gpt_live_config.py`, `CLAUDE.md` and `tests/test_live_tool_backstop.py`, and
every `live_session.py` line below was re-read at `ba9a04f`). Nothing was edited. All paths are relative to the
server repo unless they say otherwise.

[checked: corrected] Re-checked read-only on 17 Oct 2026; the line numbers below are those at HEAD `8010b56`. Six
more commits landed after `ba9a04f`, three of them during this check: `d37502d`, `13a6cbe`, `8215ff0`, `9b6e111`,
`3097833`, `8010b56`. Of the cited files they changed only `app/services/live_session.py` (hunks at lines 1010,
1049, 1581 and 1680, net +7 by the end of the file) and `CLAUDE.md` (the cited lines 128, 129 and 252 did not
move). So `live_session.py` lines past about 1010 are 1 to 7 lines lower than the original map had them, and every
cite past 1000 below was renumbered at `8010b56`. Two more commits (`792bf8b`, `0b0fad2`) landed at the very end
of the check; they changed `agent_bridge.py`, `tool_intents.py`, `tool_registry.py`, `CLAUDE.md` (lines 221-249)
and two tests, but no cited line: `live_session.py` is unchanged, and `agent_bridge.py:80,130` and
`CLAUDE.md:128,129,252` were re-read at `0b0fad2`, tree clean. That job is still committing, so search by the
quoted name before trusting a number. The robot repo was checked at `437b136` on `feature/merged-live-emotion` (pyproject `version = "0.15.0"`,
tree clean).

## 0. What changes the plan (read this first)

1. **`normalize_wav` neither resamples nor trims.** It only rewrites the WAV header and drops a trailing odd byte;
   the rate and channels stay what OpenAI sent (24 kHz mono in the test fixture, `tests/test_med_clips.py:39-50`).
   So today's medicine clip reaches the robot at 24 kHz with OpenAI's leading silence. The only resampler on the
   server is `reminders.to_pcm16k` (`app/services/reminders.py:404-419`, scipy `resample_poly`). The plan's
   "16 kHz PCM16 WAV, leading silence trimmed" is new code, not a generalisation.
2. **The `/clips/med/{id}` 304 does not exist.** The docstring promises it (`app/routers/api_device.py:250-252`) but
   the route never reads `If-None-Match`; it always answers 200 with the whole WAV (`:259`). On a cache miss it
   also renders inside the request (`med_clips.clip` → `render`, up to 2 tries of 40 s each,
   `app/services/med_clips.py:29-31,108-121`), while the robot waits at most 20 s for it (CLAUDE.md, "Reachy says
   the medicine's name"). No test covers 304.
3. **The key has no instructions in it, and the instructions are medicine-only.** `clip_key` hashes
   `MODEL|voice|text` (`med_clips.py:65-67`). `render` always sends the one `INSTRUCTIONS` constant
   (`med_clips.py:35-36,111-112`), which describes reminding an older adult to take a medicine; the reminders reuse
   it for every reminder, plain ones too (`reminders.py:433` builds the reminder's key with the same
   `clip_key(text, voice)`, `:442` calls `med_clips.render`). A help line or a greeting needs its own
   instructions, so the key must gain them (plan step 1), and `render` must gain an argument with a default:
   two tests replace `render` with a three-argument fake (`tests/test_med_clips.py:66`, `tests/test_reminders.py:359`).
   [checked: corrected] (the reminders fake is at `:359`, not `:362`; `:433` is the key, `:442` the render call)
4. **Marin exists only on a test installation.** Every speech render goes through `ai_settings.live_target()`,
   which is `None` unless `.env` has `LIVE_VOICE_TEST=1` and the live model's last Check passed
   (`app/services/ai_settings.py:216-229`), and `APP_ENV=prod` refuses to start with `LIVE_VOICE_TEST` on
   (`app/startup_checks.py:28-29`). The plan's "marin only while a live voice target exists" is therefore already
   the rule for medicine clips and reminders; the same gate gives no marin in production.
5. **The server has no copy of the robot-owned lines.** The check-in greeting, the rest line, the medicine line
   and the wind-down clip are written only in the robot repo. The three spoken lines are in Simplified characters:
   `medcare-reachy-bridge/medcare_reachy/bridge/session.py:74` (`CHECKIN_GREETING` 「您好！我来找您聊聊天。」),
   `:75` (`REST_LINE` 「我先休息一下，有需要再叫我哦。」), `:76` (`MEDICINE_LINE` 「该吃药啰，我们等一下再聊。」). The
   wind-down is a prerecorded manifest clip whose text is **Traditional**: `wind_down` 「這次就到這裡，請多保重！」 /
   "That's all for now. Take care!" (`bridge/clips/manifest.json:55-58`, `review_status`
   `pending_clinician_review`). [checked: corrected] (the map had put the wind-down text among the Simplified lines.)
   The server only hears `end` with reason `patient_left` or `medicine` (`live_session.py:1124-1128`
   [checked: corrected: was `:1122-1126`]). A server-side Traditional copy has
   to be written, and its words have to be decided: the server's CLAUDE.md quotes `REST_LINE` as
   「我先休息一下，有需要再叫我喔。」 (喔, `CLAUDE.md:128`) and `MEDICINE_LINE` as 「該吃藥囉，我們等一下再聊。」
   (囉, `CLAUDE.md:129`) [checked: corrected: the `MEDICINE_LINE` quote was missing], and the robot's Simplified
   text has 哦 and 啰, which zhconv would not give back.
6. **The task payload's keys are pinned exactly.** `tests/test_reachy_tasks.py:352-353` asserts `set(task) == {...}`,
   so a new field in `reachy_tasks.task_payload` must update that test. The heartbeat answer is not pinned by an
   exact key set (`tests/test_device_api.py:328-366`, `tests/test_device_auth.py:259-272`).
7. **The companion `ready` dict is pinned exactly**, so a `clip` field breaks
   `tests/test_live_companion.py:154-155` (`test_a_companion_start_is_paused_and_the_robot_says_the_opening`), and a
   `clip` field on `say` breaks every test that compares `SAY` (`tests/test_live_safety.py:27`, used at `:90` in the
   helper `_blocked`, `:210`, `:537`, and `tests/test_live_companion.py:554,573,669,706`) plus the repeat dict at
   `tests/test_live_companion.py:641-644`. [checked: corrected] (added) `tests/test_live_speak.py` reads only
   `ready["speech_text"]` (`:146`, `:166`), so a `clip` field on `ready` does not break it. The older-robot
   `ready` (no text) is pinned in
   `tests/test_live_access.py:70`, `tests/test_live_relay_chat.py:312,757,832`, `tests/test_live_start.py:241,451`
   and `tests/test_live_companion.py:763`; robot app 0.15.0 never takes that path (it is a companion robot).
8. **The relay can already play marin audio on the robot without any clip**: `LiveRelay.speak`
   (`live_session.py:793-835`) sends PCM16 16 kHz binary frames on the live socket. It is built for reminders: it
   waits for GPT-Live's sound and the robot's playback to end (up to `SPEAK_WAIT_SECONDS = 15.0`, `:169`), so it is
   no channel for the help line, which must play at once and offline.
9. **A render failure log loses the reason.** `render` raises `RuntimeError("speech API answered <status>")`
   (`med_clips.py:118`), but both callers log only `type(error).__name__` (`med_clips.py:149`,
   `reminders.py:445`), so the log says `RuntimeError` without the HTTP status.
10. **Clips are never deleted.** A renamed medicine leaves its old WAV (`med_clips.py:141-154` writes, nothing
    removes), account deletion does not touch `MED_CLIP_DIR` (no reference outside `med_clips.py`, `reminders.py`,
    `config.py`, `startup_report.py`), and `_renders` (`med_clips.py:44`) keeps one lock per key for the life of the
    process. Per-patient lines (opening with the call name) would add files the same way.
11. **The opening's "already said" signal is the robot's `playing`.** `_opening_played` is set by the robot's first
    `playing` (`live_session.py:611,1130` [checked: corrected: was `:1128`]), and a reminder check-in waits for it
    before speaking
    (`speak(after_opening=True)`, `:803-805`). An opening played from a marin WAV must still send `playing` and
    `idle`. [checked: corrected] (added) That wait is bounded: at most `OPENING_WAIT_SECONDS = 20.0` (`:172`, used
    at `:803`), after which the reminder goes ahead anyway (then up to `SPEAK_WAIT_SECONDS` more while GPT-Live or
    the robot is still sounding, `:810-812`). So a marin opening that sent no `playing` would not lose the reminder;
    it would delay it 20 s, or let it start over the opening. `tests/test_live_speak.py:148-155` pins the
    `playing`/`idle` wait.
12. [checked: corrected] (added) **A goodbye ending is decided by the server and reaches the robot as `end` reason
    `goodbye`.** A closed patient turn that `conversation.wants_to_end` accepts (`conversation.py:386`, words in
    `GOODBYES`, `:82`) calls `_goodbye_said` (`live_session.py:1515-1516`, `:1947-1953`); GPT-Live may answer once,
    and the ticker ends the chat `GOODBYE_GRACE_SECONDS = 3` (`:181`) after that answer's sound (`:1439-1445`,
    `_finish("end", "goodbye")` at `:1045-1046`). That check sits before the companion branch, so it ends companion
    chats too. `_end_chat` then sends `{"type": "end", "reason": "goodbye", "risk": false}` (`:2243`). The robot
    stores the reason as `outcomes["checkin"] = "goodbye"` (`medcare-reachy-bridge/.../bridge/session.py:661-662`),
    which is not in `COMPANION_ENDINGS`, so `_wind_down` plays the clip (`:920-924`). The plan's "no wind-down
    after a live goodbye" can therefore key on that reason with no server change. One caveat for the plan's
    reasoning ("GPT-Live already said bye"): when GPT-Live says nothing after the goodbye, the chat still ends 3 s
    after it (`:1953`), so a goodbye answer is usual, not guaranteed. The robot may also send `end` with reason
    `goodbye` itself (`ROBOT_END_REASONS`, `:222`).
13. [checked: corrected] (added) **Some server-written lines are already in GPT-Live's voice.** Besides the older
    robot's opening (`OPENING_COMMENTARY`, `:894`), the tool backstop has GPT-Live say what a tool did:
    `_run_intent` (`:1766`) sends `self.client.commentary(line)` (`:1785`), with lines from `INTENT_LINES`
    (`:112`) built by `_intent_line` (`:1868-1901`) and the claim answer (`:1853-1857`). These come out in marin
    already and need no change. `LiveClient.commentary` (`app/services/live_client.py:262-267`) warns that GPT-Live
    paraphrases and may drop a sentence, which is why the help line goes to the robot as `say`.
14. [checked: corrected] (added) **The repeat line is always followed by a fallback to the text chat.**
    `_start_stretch` calls `_ask_again` and then `_finish("fallback", "live_unavailable" | "error")`
    (`live_session.py:1267-1268`, `:1272-1273`). After a marin repeat line, the patient's next words go to
    `POST /conversations/{id}/turn`, whose replies stay Matcha in v1 (section 3b). On that path the voice jump
    moves to the next reply instead of going away.

## 1. `app/services/med_clips.py` in full

The file is 156 lines. Module docstring `:1-10`.

| What | Where | Detail |
|---|---|---|
| Speech endpoint | `:28` | `SPEECH_URL = "https://api.openai.com/v1/audio/speech"` |
| Model | `:29` | `MODEL = "gpt-4o-mini-tts"` |
| Timeout, retries | `:30-31` | `TIMEOUT = 40` s per request, `RETRIES = 1` (two tries in all) |
| Voices the speech API knows | `:32-33` | `VOICES` = alloy, ash, ballad, coral, echo, fable, onyx, nova, sage, shimmer, verse, marin, cedar |
| Fallback voice | `:34` | `DEFAULT_VOICE = "coral"` |
| Instructions | `:35-36` | `INSTRUCTIONS`: "A warm, calm companion robot reminding an older adult at home to take a medicine. Speak slowly and clearly; read the medicine's name in the language it is written in." |
| Sentence tables | `:37-42` | `MAX_POSITION = 8`, `ORDINALS` (zh-TW 一..八, en first..eighth), `TEXTS` (zh-TW 「請現在吃第一種藥，{name}。」 / 「接下來，請吃第{ordinal}種藥，{name}。」, en "Please take your first medicine, {name}." / "Next, please take your {ordinal} medicine, {name}."), `MAX_NAME = 60` |
| Render locks | `:44` | `_renders: dict[str, asyncio.Lock]`, one per key, never cleared |
| `language_of` | `:47-48` | anything starting with "en" is `en`, everything else `zh-TW` |
| `prompt_text` | `:51-57` | position clamped to 1..8, name stripped and cut to 60 characters; the text is Traditional/English as written, never converted |
| `voice_for(target)` | `:60-62` | the target's `voice` when it is in `VOICES`, else `coral` |
| `clip_key(text, voice)` | `:65-67` | `sha256(f"{MODEL}\|{voice}\|{text}")`, first 20 hex characters; no instructions, no language, no output format |
| `clip_path(u_id, med_id, key)` | `:70-71` | `MED_CLIP_DIR / <u_id> / med_<med_id>_<key>.wav` |
| `normalize_wav(data)` | `:74-101` | see below |
| `render(text, voice, api_key)` | `:104-121` | see below |
| `medicine_name(u_id, med_id)` | `:124-127` | `SELECT med_name FROM medication WHERE u_id = $1 AND med_id = $2`; `None` when empty |
| `clip(u_id, med_id, position, language)` | `:130-156` | see below |

**`render`** (`:104-121`). It POSTs JSON `{"model": MODEL, "voice": voice, "input": text, "instructions":
INSTRUCTIONS, "response_format": "wav"}` with `Authorization: Bearer <key>` and `timeout=TIMEOUT` (`:110-112`). A
`requests.RequestException` is kept and retried (`:113-115`); a 200 returns `normalize_wav(response.content)`
(`:116-117`); any other status becomes `RuntimeError("speech API answered <status>")` (`:118`) and stops retrying
when it is below 500 and not 429 (`:119-120`). It raises the last error (`:121`). It never logs, so the key is
never logged by it. It is a blocking function; callers run it with `asyncio.to_thread` (`:147`,
`reminders.py:442`).

**`normalize_wav`** (`:74-101`). It refuses anything that is not `RIFF....WAVE` (`:77-78`), walks the chunks
(`:79-88`), takes the `data` chunk to the end of the file when its size is a placeholder (0 or 0xFFFFFFFF) or larger
than the bytes left (`:86`), refuses a file without `fmt ` or `data` (`:89-90`) and anything but 16-bit PCM format 1
(`:91-93`), drops a partial last frame (`:94`), and writes a new WAV with the **same channel count and the same
rate** it read (`:95-101`). Output: 16-bit PCM, rate and channels unchanged (24000 Hz mono in
`tests/test_med_clips.py:46-50`), no resampling, no mixdown, no silence trimming. Errors are `ValueError`.

**`clip`** (`:130-156`). No medicine name: `None` (`:133-135`). No live target or an empty key: `None` (`:136-138`).
Text from `prompt_text`, voice from `voice_for(target)`, key from `clip_key` (`:139-141`). Under the key's lock
(`:142-143`): a file on disk is returned as is (`:144-145`); else it renders now in a worker thread (`:146-147`). A
failure logs WARNING `"medication prompt for medicine %s could not be rendered (%s)"` with the medicine id and the
exception's type name only, and returns `None` (`:148-150`). Success writes `path.with_suffix(".part")`, that is
`med_<med_id>_<key>.part` beside the final file (the suffix replaces `.wav`) [checked: corrected], then renames it
over the final path (`:151-154`), logs INFO with the medicine id, position and byte count (`:155`), and returns
`(wav_bytes, key)` (`:156`). Nothing is cached in memory; there is no negative cache, so while the API is down every
fetch tries again.

**Storage.** `MED_CLIP_DIR` defaults to `/tmp/medcare_med_clips` (`app/config.py:79-81`). The compose stack sets
`MED_CLIP_DIR: /var/medcare/clips` (`docker-compose.yml:39`) on the named volume `medclips` mounted there
(`docker-compose.yml:44`, declared at `:113`), so clips survive a recreate (comment `:37-38`). Layout:
`<MED_CLIP_DIR>/<u_id>/med_<med_id>_<key>.wav` for medicine prompts (`med_clips.py:70-71`) and
`<MED_CLIP_DIR>/reminders/<key>.pcm` (PCM16 16 kHz, no header) for reminders (`reminders.py:422-423`).
`startup_report` prints the `MED_CLIP_DIR` value in its environment line (`app/startup_report.py:38-40`).

**The live voice target and its gates.**
- `config.LIVE_VOICE_TEST` (`app/config.py:126-129`) is read once at start.
- `ai_settings.live_target(snapshot=None)` (`app/services/ai_settings.py:216-229`) returns `None` unless
  `LIVE_VOICE_TEST` is on (`:220-221`), a live row exists with `tested_at` set and no `last_error` (the last Check
  passed, `:223-225`), and its connection is `kind == "openai"` with a readable, non-empty key (`:226-228`). It
  returns `LiveTarget(api_key, model_id, voice)` (`:229`; dataclass `:89-94`, key hidden from repr).
- The card's voices are `LIVE_VOICES` (`ai_settings.py:18-19`: marin, cedar, alloy, ash, ballad, coral, echo, sage,
  shimmer, verse); all ten are in `med_clips.VOICES`, so `voice_for` returns the live voice itself.
- Saving the live model clears its Check (`ai_store.set_live`, `app/services/ai_store.py:189-196`, route
  `PUT /api/ai/live`, `app/routers/api_ai.py:335-345`). The Check is `POST /api/ai/live/check`
  (`api_ai.py:347-367`) or the automatic one at startup (`startup_report.auto_check_live`,
  `app/startup_report.py:188-215`); both save through `ai_store.save_live_check` (`ai_store.py:199-`) inside
  `ai_store.change` (`ai_store.py:104-115`), which swaps the in-memory snapshot (`ai_settings.set_current`). The live
  routes exist only with `LIVE_VOICE_TEST` (`api_ai.py:127-131`, `_live_on`).
- `APP_ENV=prod` refuses to start with `LIVE_VOICE_TEST` on (`app/startup_checks.py:28-29`).

## 2. Device endpoints that serve audio

There is one clip route: **`GET /api/device/clips/med/{med_id}`** (`app/routers/api_device.py:247-259`). No other
route serves a WAV or a line's audio; the reminders' audio goes over the live WebSocket (section 3).

- Query: `position: int = Query(1, ge=1, le=8)`, `language: str = Query("zh-TW")` (any string; `med_clips.language_of`
  folds it), `:248`.
- Auth: `device: dict = Depends(get_device)` (`:249`). `device_auth.get_device` (`app/services/device_auth.py:75-82`)
  wants a `Bearer rdv1.` device token: 401 when it is invalid or revoked, 403 `consent_required` without current
  `core` and `robot_camera` consent (`robot_consent_current`, `:71-72`). Check-in consent is not checked. The route
  is reachable only on the device port 8001: `DevicePortIsolation` (`app/main.py:131-161`) answers 404 on the wrong
  port and 401 for a device token on the public port.
- Answer: 404 `"No prompt clip for this medicine"` when `clip()` gives `None` (`:256-257`); else
  `Response(content=data, media_type="audio/wav", headers={"ETag": f'"{key}"', "Cache-Control": "private"})`
  (`:258-259`).
- **ETag/304: absent.** The docstring (`:250-252`) says "304 for a matching If-None-Match", but the route takes no
  `If-None-Match` header and never answers 304; a plain Starlette `Response` does no conditional handling either.
  The robot sends `If-None-Match` with its kept key (CLAUDE.md "Reachy says the medicine's name") and gets the full
  body every time. A miss also renders inside the request (section 1, up to about 80 s of API time).
- Test: `tests/test_medication_events.py::test_the_robot_fetches_a_spoken_prompt_or_a_404` (`:200-207`) pins 200,
  the body, `etag == '"k102zh-TW"'`, `content-type` audio/wav, 404 for an unknown medicine and 422 for
  `position=9`. Nothing tests a 304.

## 3. Every message to the robot that carries a line to speak

### 3a. The live relay (`app/services/live_session.py`, WebSocket `/api/device/conversations/live`)

The socket route is `api_device.conversation_live` (`app/routers/api_device.py:675-704`); every JSON message after
the start goes through `LiveRelay._to_robot` (`live_session.py:2290-2296`), audio through `_robot_bytes`
(`:2298-2304`) [checked: corrected: were `:2284-2290`, `:2292-2298` before the shift]. The `ready` messages
(`:754`, `:780`) and the start's `fallback` (`:675`) call `self.robot.send_json` directly. The module docstring
lists the messages (`:6-9`, companion part `:41-64`) [checked: corrected: `:41-58` cut the companion paragraph
short].

| Message | Where it is sent | Fields | Where its words come from | Fixed or per patient |
|---|---|---|---|---|
| `ready` (robot older than 0.9.0) | `:754` | `type`, `conversation_id` | no words: GPT-Live says the opening itself (`OPENING_COMMENTARY`, `:274`, sent at `:894`) | not used by robot app 0.15.0 |
| `ready` (companion, robot app 0.9.0+, so 0.15.0) | `:780-781` in `_open_companion` (`:759-784`) | `type`, `conversation_id`, `speech_text` = `conversation.speech_text(self.opening, self.language)` | `self.opening = memory.opening_line(language, name)` (`:724`); for a reminder's check-in task `REMINDER_OPENING[language].format(name=name)` or `REMINDER_OPENING_NONAME[language]` (`:725-727`; texts `:175-176`). `name` is `memory.call_name(conn, u_id, memory_on)` (`:714`) | **per patient** (call name), two languages, two variants (opening, reminder lead-in) |
| `say` reason `repeat` | `_ask_again` `:1313-1323` (called from `_start_stretch`, `:1241`, at `:1267,1272`, each followed by a `fallback`, finding 14) [checked: corrected: were `:1311-1321`, `:1265,1270`] | `type`, `speech_text`, `reason: "repeat"` | `REPEAT_LINES[language]` (`:291-292`): 「不好意思，我剛剛沒聽清楚，可以再說一次嗎？」 / "Sorry, I didn't catch that. Could you say it again?" | **fixed** per language |
| `say` reason `risk` (the help line) | `_end_blocked` `:2115-2140`, the `say` at `:2123-2124` (from `_wrap_up` `:2179-2201`, after `block` `:2079-2113` decided `risk`) [checked: corrected: were `:2109-2118`, `:2173-2192`, `:2073-2107`] | `type`, `speech_text`, `reason: "risk"` | `conversation.HELPLINE[language]` (`app/services/conversation.py:77-80`); stored as a Reachy turn with `{"live": {"say": "risk", "risk_source": ...}}` by `_store_risk` (`live_session.py:2153`, the store at `:2173`) [checked: corrected: was `:2165-2169`] | **fixed** per language |
| binary audio (a reminder, and a reminder check-in's reminder) | `LiveRelay.speak` `:793-835`, from `reminders.fire` (`reminders.py:284`; `audio()` at `:293`, `relay.speak` at `:297`) [checked: corrected: was `:296-302`] or `_say_reminder` (`live_session.py:837-850`, started at `:756,783`) | PCM16 mono 16 kHz in 3200-byte frames (`SPEAK_CHUNK_BYTES`, `:170`); no JSON; the microphone is muted to GPT-Live for the line plus `SPEAK_TAIL_SECONDS = 0.8` (`:171,818`); stored as a Reachy turn `{"live": {"say": reason}}` (`:826`); GPT-Live told through `SPOKEN_NOTE` (`:173-174,832`) | `reminders.SENTENCES` (`reminders.py:46-51`), built in `reminders.create` (`:208-219`) with the medicine name from the database; already **marin** (`reminders.audio`, `:426-453`) | per reminder (medicine name or the patient's words) |
| `end` | `_end_chat` (`:2221`) sends it at `:2243`; `_after_failure` `:652` [checked: corrected: was `:2237`] | `type`, `reason`, `risk` | no words; but the reason `goodbye` is what the robot needs for "no wind-down after a live goodbye" (finding 12) | none |
| `fallback`, `paused`, `not_seen`, `flush`, `express`, tool requests | `:675`, `:2266` (`_fall_back`, `:2246`), `:654`; `:1357`; `:1214`; `:1178`, `:1345`, `:1984`, `:2112`; `:1521-1567`; `agent_bridge.py:80-130` [checked: corrected: were `:2260`; `:1355`; `:1212`; `:1176`; `:1519-1565`; the other `flush` sites added] | no spoken text in any of them | none |
| [checked: corrected] (added) GPT-Live `commentary` (no robot message) | `:894` (older robot's opening), `:1785` (`_run_intent`, the tool backstop) | none to the robot: GPT-Live says it in its own voice | `OPENING_COMMENTARY` + the opening; `INTENT_LINES` (`:112`) via `_intent_line` (`:1868-1901`) | already marin (finding 13) |

**`stop` with `say=...` is robot-side only.** `LiveChat.stop(reason, say)` is in
`medcare-reachy-bridge/medcare_reachy/bridge/live.py:345`; its words are the robot's `REST_LINE` and
`MEDICINE_LINE` (`bridge/session.py:75-76`). The robot then sends `{"type": "end", "reason": "patient_left" |
"medicine"}`, which the relay reads at `live_session.py:1126-1128` against `ROBOT_END_REASONS` (`:222`)
[checked: corrected: was `:1122-1126`]. The server
sends nothing back with words, stores no turn for those lines, and has no copy of them (finding 5). Likewise the
ANNOUNCE greeting (`bridge/session.py:74,295-298`, said with Matcha before the chat starts) and the wind-down clip
(`bridge/session.py:920-924`; skipped after `patient_left` and `medicine` by `COMPANION_ENDINGS`, `:63`, but played
after a goodbye) never reach the server. [checked: corrected] (added) The ANNOUNCE greeting is said only for a
check-in task (`_announce`, `:295-300`); a dose task plays the manifest clip `wake_greeting`, or `reminder` for a
`missed_retry`, instead. After a check-in task's live chat the wind-down follows at once (`_after_checkin`,
`:899-905`); after a dose task's chat it comes only after `POST_SLOT_OBSERVE` (`_post_slot`, `:907-914`,
`POST_SLOT_SECONDS = 120.0`, `:78`).

**How `speech_text` is made** (`app/services/conversation.py:400-407`): `clean_reply` (`:394-397`) strips markup
including `()（）【】` and emoji, folds spaces and cuts to 240 characters; for `zh-TW` it is then converted to
Simplified with `zhconv` (`zh-cn`), because the Matcha voice reads Simplified only. English is left as is. So the
Traditional text the plan wants to key on is `self.opening`, `REPEAT_LINES[...]` or `HELPLINE[...]` before this call.

**Where the per-patient opening's inputs change** (for "render ahead when the call name changes"):
- `memory.opening_line(language, name)` (`app/services/memory.py:148-152`) prefixes `conversation.OPENING`
  (`conversation.py:71-72`: 「今天感覺怎麼樣？想跟我聊聊嗎？」 / "How are you feeling today? Would you like to chat?")
  with `name，` (zh) or `name, ` plus a lower-cased first letter (en).
- `memory.call_name(conn, u_id, memory_on)` (`memory.py:264-272`): the patient's memory `name` entry while memory
  consent is current, else `call_name_of(reachy_call_name, "user".name)` (`memory.py:162-166`).
- Writes that change it: the Reachy card's call name, `PATCH /api/reachy/settings` (`app/routers/api_reachy.py:112-131`,
  the UPDATE at `:129`); the patient's memory `name` entry, `POST /api/memory` and `PATCH /api/memory/fact`
  (`app/routers/api_memory.py:52-80`) and its deletes (`api_memory.py:83-104`); memory consent, `POST /api/consent`
  (`app/routers/api_consent.py:29`). A `name` entry never comes from a chat (`memory.py:86-95`: chat facts may not be
  kind `name`). The registered `"user".name` is never updated after registration (no UPDATE in `app/`).
- The reminder lead-in depends on the same name (`live_session.py:726`).

### 3b. The device API over HTTP

| Route | Where | Fields with words | Built from |
|---|---|---|---|
| `GET /tasks/next`, `GET /tasks/current` (the check-in task payload) | `api_device.py:127-136` → `reachy_tasks.task_payload` (`app/services/reachy_tasks.py:66-100`) | **no line to speak.** Keys: `task_id, slot_time, reason, attempt, status, expires_at, patient_name, auto_record, microphone, checkin, live, doses` (the dict `:79-99`). `patient_name` is the registered `"user".name` (read at `:73-76`, set at `:82`), not the call name; `live` is `LIVE_VOICE_TEST and paired and checkin and live_target() is not None` (`:92-93`). A reminder's text sits in `reachy_task.detail` (`reminders.py:332-334`), which the payload does not send. [checked: corrected: were `:64-100`, `:81-99`, `:70-73`, `reminders.py:331-333`] | n/a |
| `POST /heartbeat` | `api_device.py:707-733` | **no line.** Answer `{"stop_all": False, "microphone", "server_time", "task_due"}` (`:733`), or `{"stop_all": True, "microphone": False, "server_time"}` for a revoked device or withdrawn consent (`:710-711`). The robot already reports `missing_clips: int | list[str]` (`HeartbeatPayload`, `:92-99`), stored in `reachy_device.status_detail` (`:712-720`) | n/a |
| `POST /conversations` (text chat opening) | `api_device.py:427-461` | `reply`, `speech_text` (`_spoken`, `:417-424`) [checked: corrected: was `:416-424`] | a reminder task: the reminder's text (`:441-444`); a `checkin` task: a **fixed** schedule line (`:445-449`) [checked: corrected: was `:445-450`]; otherwise `memory.opening_line(language, name)` (`:451`), per patient. The live chat never uses the fixed check-in line (`live_session.py:724`) |
| `POST /conversations/{id}/turn` | `api_device.py:464-581` | `reply`, `speech_text` | `HELPLINE` (`:545,558`), `CLOSING` (`:565`, `conversation.py:73-74`), the model's reply (`:567`), `FALLBACK` (`conversation.py:75-76`) inside `reply_with_metrics` |
| Any dose route refused by overdose protection (409) | handler `app/main.py:111-115` | `reply`, `speech_text` | `dose_safety.refusal_body` (`app/services/dose_safety.py:458-465`), per dose |
| `GET /clips/med/{med_id}` | section 2 | WAV | `med_clips.prompt_text`, per medicine and position |

The plan keeps the text chat, the refusals and the dose-flow clips on Matcha (v1), so only the live rows of 3a plus
the robot-owned lines are in scope.

## 4. Where rendering ahead could run, and how a failure is logged today

**Startup.** The lifespan is `app/main.py:44-105`.
- The AI snapshot (with the live target) is loaded at `:61` [checked: corrected: was `:59`]; `startup_report.gpt_live` judges the live model at
  `:70-72` (`app/startup_report.py:95-121`; status `ok`, `off`, `no_model`, `no_connection`, `wrong_kind`,
  `key_unreadable`, `no_key`, `check_failed`, `no_check`).
- `reminders.reload()` (`main.py:96-100`, `reminders.py:393-400`) re-arms reminders; it renders nothing.
- `startup_report.finish_in_background(...)` (`main.py:101`, `startup_report.py:236-240`) runs `finish`
  (`:218-234`) after the servers are up: network probes, the LINE link, then `auto_check_live` when the status was
  `no_check` or `check_failed` (`:228-233`; `:188-215`: up to `LIVE_CHECK_ATTEMPTS = 4` tries,
  `LIVE_CHECK_RETRY_SECONDS = 20.0` apart, `:34-35`). **This is the natural place for rendering the fixed lines
  ahead**: only after it can `live_target()` be non-`None` on a fresh start whose Check had been cleared. The task
  pattern to copy keeps the task in `_tasks` (`:45,236-240`) [checked: corrected: was `:43`]. Note that `finish`
  runs `auto_check_live` only for `no_check` or `check_failed`; when the stored Check already passed, the target
  exists from `main.py:61` on, so a render-ahead hook must run whatever the status was.
- `[STARTUP]` lines come from `startup_report.mark(stage, ok, detail, pending=, warn=)` (`:55-65`): INFO when OK,
  WARNING `DEGRADED` with `warn` or `pending`, ERROR `FAILED` otherwise; `GET /health` carries them
  (`main.py:220-232` [checked: corrected: the route starts at `:220`], `summary`, `:253-260`). `STAGES` (`:29-30`) lists the stage names; no test pins it.
- The scheduler (`app/jobs/scheduler.py:12-112`) has a "once now, then every 30 min" pattern in
  `checkin_background` (`:89-101`: `next_run_time=now`, `misfire_grace_time=None`, a plain function run in a worker
  thread) and a minute job `reminders` (`:102-110`). A periodic retry of failed fixed-line renders fits either.

**When a check-in task is queued** (per-patient lines): every task goes through
`reachy_tasks.enqueue_reachy_task` (`reachy_tasks.py:103-149`). Callers: "Talk to Reachy now"
(`app/routers/api_reachy.py:133-149`, enqueue at `:145`), a reminder's check-in (`reminders._queue_checkin`,
`reminders.py:321-338`, enqueue at `:328`), the missed-dose job (`app/jobs/missed_dose_job.py:151`), the manual
task (`api_reachy.py:188`) and the retry after a held task (`reachy_tasks.py:284`). Every check-in reaches the robot
through `_lease_once` (`reachy_tasks.py:160-176`) and `task_payload`.

**When the live target changes**: `PUT /api/ai/live` (`api_ai.py:335-345`, clears the Check), `POST /api/ai/live/check`
(`api_ai.py:347-367`) and `startup_report.auto_check_live` (`startup_report.py:188-215`). A changed voice changes
every key.

**The existing render-ahead precedent**: `reminders.create` calls `_render_ahead(text, lang)` (`reminders.py:224`)
right after the row is written; `_render_ahead` (`:456-460`) starts `audio()` as a background task kept in
`_renders` (`:67`). `fire` calls `audio()` again (`:293` [checked: corrected: was `:297`, which is the
`relay.speak` call]), which hits the memory cache `_audio` (`:68`, never cleared) or the disk file.

**How a render failure is logged today**
- `med_clips.render` raises and never logs (`med_clips.py:104-121`).
- `med_clips.clip` logs WARNING `medication prompt for medicine <id> could not be rendered (<ExceptionType>)` and
  returns `None` (`:148-150`); the route then answers 404 (`api_device.py:256-257`).
- `reminders.audio` logs WARNING `[REMINDER] the voice could not render '<first 40 characters>' (<ExceptionType>)`
  and returns `None` (`reminders.py:443-445`); the relay then says nothing (`live_session.py:800-801`).
- Neither puts the HTTP status in the log (finding 9), neither marks a `[STARTUP]` stage, and neither keeps a
  negative cache or a retry timer.

## 5. Tests that pin each part (file::test, line)

**med_clips**
- `tests/test_med_clips.py::test_the_sentence_names_the_medicine_and_its_place_in_the_slot` (`:22`): `prompt_text`.
- `tests/test_med_clips.py::test_the_key_follows_the_words_and_the_voice` (`:31`): `clip_key` is stable, 20
  characters, changes with text and voice; `voice_for` gives marin and falls back to `DEFAULT_VOICE`. It does not
  pin the hash formula, so adding instructions to the key keeps it green.
- `tests/test_med_clips.py::test_a_streamed_wav_gets_a_truthful_header_and_odd_bytes_are_dropped` (`:46`): pins that
  `normalize_wav` keeps **24000 Hz, 1 channel** and 2401 frames. A resample or trim inside `normalize_wav` breaks
  it; a new function beside it does not.
- `tests/test_med_clips.py::test_clip_renders_once_per_key_and_keeps_it_on_disk` (`:79`): the on-disk name
  `med_<id>_<key>.wav` under `<MED_CLIP_DIR>/<u_id>/`, one render per key, a rename re-renders. Its fake `render`
  takes `(text, voice, api_key)` (`:66`).
- `tests/test_med_clips.py::test_no_clip_without_a_medicine_a_key_or_when_the_api_fails` (`:93`): `None` without a
  name, target or key, and on failure; the log says "could not be rendered" and never the key.

**The clip route**
- `tests/test_medication_events.py::test_the_robot_fetches_a_spoken_prompt_or_a_404` (`:200`): 200 with ETag, 404,
  422. No 304 test exists.

**Reminders (marin through speak)**
- `tests/test_reminders.py::test_sentences_name_the_medicine_from_the_database_not_the_request` (`:75`): the sentences.
- `tests/test_reminders.py::test_a_reminder_during_a_live_chat_is_said_by_its_relay` (`:252`),
  `::test_without_a_chat_the_robot_is_sent_as_a_checkin_task` (`:259`),
  `::test_without_a_robot_the_patient_gets_a_line_text_else_it_is_undelivered` (`:265`),
  `::test_a_relay_that_cannot_speak_falls_through_to_the_robot` (`:275`),
  `::test_a_reminder_fires_only_once` (`:281`): the delivery order in `fire`.
- `tests/test_reminders.py::test_the_voice_is_resampled_to_the_robots_16_khz` (`:332`): `to_pcm16k` 24 kHz mono and
  16 kHz stereo to 16 kHz mono.
- `tests/test_reminders.py::test_audio_needs_a_live_connection_and_caches_what_it_rendered` (`:350`): no target, no
  audio; one render, then the memory cache, then the disk file. Its fake `render` takes three arguments (`:359`
  [checked: corrected: was `:362`]).

**The relay's lines**
- `tests/test_live_companion.py::test_a_companion_start_is_paused_and_the_robot_says_the_opening` (`:149`): the exact
  companion `ready` dict with `speech_text` 「王奶奶，今天感觉怎么样？想跟我聊聊吗？」 (`:154-156`).
- `tests/test_live_speak.py::test_a_checkin_task_made_for_a_reminder_opens_with_a_lead_in_then_says_it_after_the_opening`
  (`:117`): `ready` carries the lead-in 「王奶奶，我来提醒您一件事。」 (`:146`), the reminder waits for `playing`/`idle`
  (`:148-155`), then plays the PCM and stores both turns.
- `tests/test_live_speak.py::test_a_task_without_a_reminder_opens_as_before` (`:163`): the plain opening in `ready`.
- `tests/test_live_speak.py::test_a_line_waits_for_gpt_lives_sound_then_plays_mutes_the_microphone_and_is_stored`
  (`:33`), `::test_no_line_without_audio_after_a_risk_or_once_the_chat_is_over` (`:66`),
  `::test_a_line_waits_while_the_robot_still_plays_and_gives_up_after_speak_wait_seconds` (`:81`),
  `::test_a_paused_companion_chat_plays_the_line_and_keeps_no_echo_as_pre_roll` (`:100`): `speak`.
- `tests/test_live_companion.py::test_a_stretch_that_cannot_start_asks_the_patient_to_say_it_again` (`:632`): the
  exact `say` repeat dict (`:641-644`).
- The help line `say`: `tests/test_live_safety.py` constant `SAY` (`:27`) and helper `_blocked` (`:83-95`, the
  assert at `:90`), used by most tests in that file, e.g.
  `::test_a_screening_hit_holds_at_once_and_a_confirmed_hit_blocks_the_chat` (`:104`); also `:210` and `:537`; and in
  `tests/test_live_companion.py` at `:554`, `:573`, `:669`, `:706` (imported `SAY`, `:32`).
- Older-robot `ready` without text: `tests/test_live_access.py::test_the_chat_re_checks_its_access_every_10_s_and_goes_on_while_it_is_allowed`
  (`:58`, assert `:70`), `tests/test_live_relay_chat.py::test_the_opening_goes_to_gpt_live_as_commentary_quoted_as_data`
  (`:306`, `:312`), `:757`, `:832`, `tests/test_live_start.py::test_a_start_that_passes_every_check_connects_then_stores_the_chat`
  (`:235`, `:241`), `tests/test_live_start.py:451`, `tests/test_live_companion.py::test_an_older_robot_gets_todays_live_chat`
  (`:758`, `:763`).
- The robot's `end` reasons: `tests/test_live_companion.py::test_the_robot_ends_a_companion_session_for_medicine_or_rest`
  (`:590`).
- [checked: corrected] (added) The server's goodbye ending (finding 12):
  `tests/test_live_relay_chat.py::test_a_goodbye_ends_the_chat_3_s_after_gpt_lives_answer` (`:622`) and
  `::test_a_goodbye_gpt_live_does_not_answer_ends_the_chat_3_s_later` (`:642`), both on the older-robot relay; no
  companion test pins a goodbye ending.
- The opening's name: `tests/test_live_start.py::test_without_a_name_and_in_english` (`:366`);
  `tests/test_conversation.py::test_without_memory_consent_the_opening_uses_the_cards_call_name` (`:1387`) and
  `::test_start_and_a_normal_turn` (`:732`) for the text chat.

**Payload and heartbeat**
- `tests/test_reachy_tasks.py::test_lease_next_leases_oldest_queued_and_returns_payload` (`:347`): the exact payload
  key set (`:352-353`).
- `tests/test_reachy_tasks.py::test_a_task_says_live_when_every_condition_holds` (`:919`),
  `::test_live_is_the_default_whatever_the_robots_old_live_switch_says` (`:926`),
  `::test_a_task_says_no_live_when_any_condition_is_missing` (`:938`): the `live` flag; `:923` also asserts the
  OpenAI key never appears in the payload.
- `tests/test_device_api.py::test_heartbeat_updates_device_and_extends_leases` (`:328`),
  `::test_heartbeat_reports_microphone_consent` (`:347`), `::test_heartbeat_says_whether_a_medicine_task_waits`
  (`:359`), `::test_a_companion_heartbeat_keeps_the_held_task_alive` (`:368`),
  `::test_a_failing_task_due_read_answers_false_and_keeps_the_heartbeat_writes` (`:914`);
  `tests/test_device_auth.py::test_heartbeat_reports_stop_all_for_revoked_device_or_withdrawn_consent` (`:259`).
  None compares the whole answer, so a new key does not break them.

**Startup**
- `tests/test_startup_report.py::test_gpt_live_says_exactly_why_live_voice_is_off` (`:60`),
  `::test_the_automatic_check_runs_only_without_a_passing_check_and_saves_its_result` (`:99`),
  `::test_a_refused_key_is_not_retried_and_says_so` (`:135`),
  `::test_marks_build_the_health_summary_with_degraded_and_pending_stages` (`:66`).
- `tests/test_startup_checks.py` (`:60-66`): prod refuses `LIVE_VOICE_TEST`.

**Docs**
- `tests/test_live_voice_docs.py::test_the_docs_name_each_live_message_and_both_ends_use_it` (`:180`): every name in
  `SERVER_SENDS = ("ready", "flush", "say", "fallback", "end")` and `ROBOT_SENDS` (`:38-39`) must appear in
  CLAUDE.md's live voice section, in `live_session.py` and in the server's **old robot copy**
  `reachy_app/medcare_reachy/bridge/live.py`, not the medcare-reachy-bridge repo. A new message type would need all
  three; a new field on `ready`/`say` would not.
- `::test_every_code_name_in_the_section_is_in_the_code` (`:169`) and
  `::test_every_code_name_in_the_companion_text_is_in_the_code` (`:175`): every backticked name in those sections
  must exist in `CODE_GLOBS` (`:33-35`: `app/`, `sql/init.sql`, `reachy_app/`, the frontend, `.env.example`).
  [checked: corrected] The sections are not CLAUDE.md's alone: the first test is parametrized over `SECTIONS`
  (`:23-27`), the live voice sections of `CLAUDE.md`, `reachy_app/README.md` and `HANDOFF.md`; the second over
  `COMPANION_PARTS` (`:28-31`), CLAUDE.md's "How it ends" bullet and the README's companion section. The checks are
  also stricter than "the name exists" (module docstring `:4-14`): a `NAME = value` span must equal every
  definition of NAME in the code (for example `SPEAK_WAIT_SECONDS = 15.0`), a documented route `/api/device/...`
  must be declared in `app/routers/api_device.py`, and a file path must exist. A robot-side name that exists only in
  medcare-reachy-bridge (for example a new 0.16.0 function) must not be backticked there.
