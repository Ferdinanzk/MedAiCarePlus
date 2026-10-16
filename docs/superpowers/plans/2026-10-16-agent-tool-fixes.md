# Agent tool fixes: dose videos, record_medication_taken, stop_dance

> For agentic workers: implement task by task, test first. Branch `agent-tools-fixes` (from
> `feature/merged-live-dose-status` @ `d2a4a8b`). Never push.

**Why:** the user tested the GPT-Live agent tools on the robot (16 Oct 2026) and marked two as broken:
`record_medication_taken` ("no video was sent to LINE") and `stop_dance` (Reachy did not stop). The user's decision
for videos (option A): every medicine intake sends its video to LINE whenever the patient has "Dose videos for
family" on, and when a video truly cannot be made, the family message says why. The consent stays.

**Evidence** (read-only investigation with an adversarial review, 16 Oct; probes in the session scratchpad):
- Two computers share one LINE bot. The bot has one webhook address, and `dose_video.public_base_url()` builds every
  video link from it. The probe (`dose_video.py:279-280`) accepts any status below 500, and the LINE proxy
  (`scripts/line-webhook/nginx.conf`) answers 404 itself for every path but a token-shaped `.mp4|.jpg`, so the probe
  passes even when the tunnel leads to the other computer's server, whose server does not have the clip. When the
  probe fails, `_store` keeps the clip unsent and logs nothing; nothing retries it.
- `record_medication_taken` (`tool_registry.py:583-685`): `clip_sent_earlier` is true whenever a watch review was
  scheduled, even when that review carried no clip, so neither message has a video while the request says it went
  earlier. A capture that finds no frames, or no consent, returns "none" silently.
- A record without `session_id` leaves an open watch unsettled (a contradicting review and a second clip go out); a
  stale `session_id` fails the record even with `patient_confirmed`; a dose waiting for family is answered
  `already_recorded`; every `ValueError` is read as "already recorded"; closing GPT-Live (a goodbye, a pause)
  cancels a record still running.
- GPT-Live's tool-mode prompt keeps the text chat's rule to praise a medicine claim itself
  (`conversation.py:138-142`), against the tool note's "delegate it", and the relay has no backstop for it, so
  「我吃完藥了」 often never reaches the tool at all.
- `stop_dance`: the robot side works (the real `play_move` stops within a step). The stop reaches the robot 2-6 s after
  the word (the backstop waits for GPT-Live's reply + 2.5 s, or the words + 6 s), while a default dance is one move of
  about 2-5 s. The stop detector (`tool_intents.py:81-82`) is anchored, so 「不要再跳了」, 「日希，停下來」, "Reachy, stop"
  are missed. A pending dance intent survives a stop, so a dance can start after it.

## Global constraints

- Server repo `C:\Reachy_Mini\repo\MedAiCarePlus` only. Python 3.11 in Docker; tests with
  `.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider` (baseline: 2415 passed, 0 failed).
- Test first: each behaviour below gets a failing test, then the change. Existing tests that pin the old behaviour
  are updated in the same task, with the reason in the commit message.
- Match the files' style: plain-English comments, the same naming, no new dependencies.
- Log lines a person needs: one WARNING per lost or late video with the reason; no keys, tokens or patient names in
  logs. Video tokens never appear in logs.
- Update `CLAUDE.md` where it describes the changed behaviour, in the same commit.
- One commit per task on `agent-tools-fixes`, message ending with
  `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`. Never push, never merge.

## Task 1: a video link must reach this server; an unsent clip is retried

Files: `app/services/dose_video.py`, `app/routers/api_dose_video.py`, `app/jobs` (the `dose_videos` job is
`dose_video.cleanup`), tests (`tests/test_dose_video*.py` or a new `tests/test_dose_video_link.py`).

1. A per-process probe token: 43 characters, the same shape as a real video token, made once at import
   (`secrets.token_urlsafe(32)`), never stored, never logged.
2. `GET /api/media/line/<probe token>.jpg` answers 200 with a short fixed marker body and `Cache-Control: no-store`;
   any other unknown token keeps today's answer. (The LINE proxy forwards token-shaped `.jpg` paths, so the probe
   travels the same path as a family member's video.)
3. `public_base_url()` probes `{base}/api/media/line/<probe token>.jpg` and accepts the base only for a 200 with the
   marker. A 404 (the proxy, or another computer's server), a 5xx or no answer is "no public link". It records the
   reason (`no LINE token`, `LINE webhook endpoint error`, `tunnel not answering`, `tunnel reaches another server`)
   and logs one WARNING when the state or reason changes (not on every call). Keep the cache times.
4. `_store`: a clip kept unsent for lack of a link logs WARNING `dose video <video_id> for intakes <ids> kept unsent:
   no public link (<reason>)`.
5. The `dose_videos` job (every 2 min) retries: a video not yet sent, not deleted, younger than the 4 h limit, whose
   confirmation request (`dose_confirmation` with `resolution IS NULL` sharing an intake) is still open, gets
   `send_with_confirmation` once a public link answers. Idempotent: the outbox dedupe key already stops a second send.
   A video whose request was answered or expired is left to the existing deletion rules.
6. Tests: the probe rejects a 404 and a wrong body, accepts the marker; the probe route answers only its own token;
   a WARNING is logged once per reason; a clip stored without a link is sent by the job when the link returns, and
   only once; a resolved request's clip is not sent by the job.

## Task 2: the family request tells the truth about the video

Files: `app/services/tool_registry.py`, `app/services/dose_video.py`, `app/services/dose_confirmation.py`,
`app/routers/api_device.py` (the robot's own `/tasks/{id}/confirmation` path), tests.

1. `record_medication_taken`: `clip_sent_earlier` is true only when the watch's review message was actually sent with
   a clip (from the review task's outcome; add what is needed to `dose_event` to know that). A review that was
   cancelled, failed or carried no clip means the clip goes with the request (`dose_video.capture`).
2. Before the request is created, the server knows whether a clip can come (`dose_video` helper: consent current and
   at least `MIN_FRAMES` fresh frames buffered). The request's evidence carries `video`: `with_request`,
   `sent_earlier`, `none:no_consent` or `none:no_frames`. The robot's confirmation path does the same.
3. `_request_messages` renders it in both languages: with_request → the video follows in the next message;
   sent_earlier → as today; none:no_consent → no video, because "Dose videos for family" is off for this patient;
   none:no_frames → no video, because the camera had no recent frames. (A clip stored but not yet sent for lack of a
   link is covered by Task 1's retry; its WARNING names it.)
4. Tests: each evidence value renders its line in zh and en; a review that carried no clip does not set
   sent_earlier and the request's clip is captured; the robot path sets the same values.

## Task 3: record_medication_taken never loses a legitimate request

Files: `app/services/tool_registry.py`, `app/services/dose_confirmation.py`, `app/services/agent_bridge.py`,
`app/services/live_session.py`, tests.

1. An open watch for the same dose (same medicine, or `names_match` on the name) is settled by a record even when no
   `session_id` is given: its pending review is cancelled, the watch is spent, and the clip goes with the request.
   The detector's verdict still authorises a record only for the watch the call names.
2. With `patient_confirmed` true, an unknown or spent `session_id` is treated as "no detector evidence" (logged at
   INFO), not as an error. Without the patient's word it stays an error.
3. A dose already waiting for family answers `already_pending_confirmation` (`family_asked` false), never
   `already_recorded`.
4. `dose_confirmation.create` raises a dedicated `NothingToConfirm(ValueError)` when there is no pending or missed
   dose; the tool catches only that one, so a database or encoding error is an error, not "already recorded".
5. Closing GPT-Live (an ending or a companion pause) lets a running `record_medication_taken`, `notify_family` or
   `emergency_alert` finish (each within its `TIMEOUTS` entry) instead of cancelling it; other tools are cancelled
   as today. The companion's quiet pause waits while such a tool runs.
6. Tests for each point; the existing tests that pin the old answers are updated.

## Task 4: 「我吃完藥了」 reaches the tool

Files: `app/services/conversation.py`, `app/services/gpt_live_config.py`, `app/services/tool_intents.py`,
`app/services/live_session.py`, tests (`tests/test_live_tool_backstop.py`, `tests/test_conversation.py`).

1. `reply_prompt(tools=True)` no longer tells GPT-Live to praise a medicine claim itself (zh and en): with tools it
   delegates the claim and praises only after the backend's answer. The text chat's prompt (tools off) is unchanged.
2. `BACKEND_INSTRUCTION`'s completion flow records first and applauds only after `record_medication_taken` answers
   `pending_confirmation` (or the dose was already taken / waiting for family), as the camera workflow does.
3. A new backstop intent `took_medicine`: the patient's first-person, completed intake (「我吃完藥了」, 「我吃過藥了」,
   「藥吃了」, 「我已經吃藥了」, "I took my medicine", "I've taken my pills"). Never for another person (「他吃完藥了」),
   the future or a plan (「我八點要吃藥」, 「我等一下吃藥」), a negation (「我還沒吃藥」), a question (「我吃藥了嗎」) or food
   (「我吃完飯了」).
4. When GPT-Live has not delegated it within the usual grace, the relay runs `check_medication_due`, then
   `record_medication_taken` with `patient_confirmed` true for the due dose only when the due check allows it, and has
   GPT-Live say the outcome (the same `INTENT_LINES` mechanism as the other backstops: asked family to confirm /
   already taken / waiting for family / not recorded and why). It never records a refused dose.
5. Tests: the intent table above (positives and negatives); the backstop runs the two tools in order and records only
   an allowed dose; GPT-Live's own delegation within the grace means no second run.

## Task 5: stop_dance stops at once

Files: `app/services/tool_intents.py`, `app/services/live_session.py`, `app/services/tool_registry.py`,
`app/services/gpt_live_config.py`, tests (`tests/test_live_tool_backstop.py`, `tests/test_tool_registry.py`).

1. While a dance may still run (`dancing()`), a stop phrase anywhere in the patient's clause is a stop: 「停」,
   「停下來」, 「停止」, 「不要跳了」, 「不要再跳了」, 「別跳了」, 「不跳了」, 「好了，停」, with a name or 「你」 in front
   (「日希，停下來」), and "stop", "please stop", "Reachy, stop". Never 「不要停」, 「停車」, 「停電」, 「不停」.
2. The relay runs `stop_dance` at the first transcript fragment that holds the stop, without waiting for GPT-Live's
   reply; GPT-Live's own later `stop_dance` call shares that run.
3. A stop drops any pending dance intent, and a `dance` call (GPT-Live's or the backstop's) that arrives within
   `STOP_HOLD_SECONDS` (10) after a stop and before the patient asks to dance again is refused with
   `stopped_by_patient`.
4. A dance with no `repeat` plays `DEFAULT_DANCE_REPEAT` (2) moves, so there is time to stop it; the schema text and
   `BACKEND_INSTRUCTION` say so.
5. Tests: the phrase table (positives and negatives) while dancing and not dancing; the stop runs within one relay
   step of the fragment; no dance starts after a stop; a new dance request after the stop dances again.

## Done when

- Full server suite green (2415 + the new tests), and each task's behaviour is covered by a test that failed before.
- `CLAUDE.md` describes the new behaviour (dose videos, the agent tools section, the backstop intents).
- A live test on the robot: 「我吃完藥了」 → a family request with its video on LINE (or a message that says why
  not); 「跳舞給我看」 then 「不要再跳了」 → the dance stops.
