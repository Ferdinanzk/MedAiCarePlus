# Feature updates: priority order and implementation plan (19 October 2026)

The user's list (`featureUpdates.txt`, 19 Oct) researched against the code and the live system. **Nothing here is
built yet.** Each item: what happens today (with evidence), the change, effort, risks, and the questions only the user
can answer. Research: four read-only investigations on 19 Oct (server log, database, both repos), with the key claims
re-checked by hand.

## The order, and why

Medication safety and the core dose flow come first, then the family loop, then control, then wording and voice.
Items 0-4 need **no robot change** (robot app 0.16.0 already handles everything they send). Items 5-6 need robot app
0.17.0, so their robot parts ship together in one release.

| # | What | User's item | Where | Effort |
|---|---|---|---|---|
| 0 | A dose was filed with family on the patient's *intention* (「我要吃一下」), with praise, before it was taken | found while researching item 2 | server | S, 0.5-1 day |
| 1 | A medicine reminder in a chat must start the dose flow | 2 (first) | server | M, 1.5-2 days |
| 2 | Reachy hears family's answer and speaks about it, also mid-chat | 1 | server | M, 2-3 days |
| 3 | After a "not taken" answer, gentle encouragement (加油 / 關懷) | 3 | server, with item 2 | in item 2 |
| 4 | An "End Reachy's session" button | 4 | server (+ robot 0.17.0) | S, 1.5 days (+1 day) |
| 5 | 「阿公，吃藥的時間到了，請吃藥。」 with the call name | 2 (second) | server + robot 0.17.0 | S, 1.5 days |
| 6 | The dose-flow lines in the same voice as the chat | 5 | server + robot 0.17.0 | M, 1.5-2 days |

About 9-11 working days in all, plus robot tests with the user. Suggested release: server items 0-4 and the server half
of 5-6 first (one server deploy, no robot deploy), then robot app 0.17.0 for 4-6.

## The user's decisions (19 Oct 2026)

1. **A reminder for a medicine that is not due yet** (like secondmed at 11:24): "Tell the person to wait." Reachy
   says the medicine is not due yet and when it is; no dose task. (Item 1.)
2. **Family answers while no chat is running:** "Wake up and speak." The robot wakes, finds the patient and says it.
   (Item 2.)
3. **"Different voice":** "It should use the gpt-live voice." Reading A: every medicine line in the live voice
   (marin). (Items 5 and 6.)
4. **"Not taken" mid-chat:** "it will tell 阿公 to eat his medicine and be more empathetic." Reachy encourages the
   patient warmly to take it now. (Items 2-3.) This answer was given to the question about item 4's retry, which
   stays open (see item 4).
5. **The name before the camera confirms the patient:** "Yes, it should use the name defined in the system." The
   greeting says the call name at wake: the name set on the Reachy card, else the registered name, by today's rule
   (`memory.call_name`). (Item 5.)

---

## 0. A dose filed on the patient's intention (safety bug, not on the list)

**Today.** Chat of 19 Oct 11:22 (u_id 2). At 11:26:25 the patient said 「好,可以,那測試藥我要吃一下」 ("OK, I'll take
the test medicine now"). At 11:26:27 GPT-Live called `record_medication_taken` (server log `03:26:27Z ... record_medication_taken:
ok`), with no camera watch, and Reachy said 「好,我來處理。拍拍手!您越來越健康了!」. The request (`f93f95a3`) went to
family as if the dose were taken. A dose waiting for family counts as taken for overdose protection, so a wrong tap or
a later real dose can be refused or doubled.

**Change.**
1. `gpt_live_config.BACKEND_INSTRUCTION` (MEDICATION COMPLETION) and `conversation.LIVE_CLAIM_PRAISE`: record only a
   *finished* intake (吃完了 / 吃過了 / 已經吃了 / "I took"); an intention (我要吃, 我等一下吃, 我去吃, "I'm going to
   take") starts the camera watch (`start_pill_intake_detection`) or asks the patient to say when it is done; never
   praise before the record's answer.
2. A server guard in `record_medication_taken`: with `patient_confirmed` and no watch named, read the patient's last
   closed turn (the relay has it) with a new `tool_intents.intends_intake()` reader, and answer `not_taken_yet`
   instead of filing. GPT-Live then says "tell me when you have finished".

**Tests.** The 11:26 words give `not_taken_yet`; 「我吃完藥了」 still files; a watch-backed record is unaffected.

**Risk.** A reader that is too wide could refuse a real claim (「我吃了」 must still pass). The open request `f93f95a3`
is still waiting for family: the user decides whether to leave it.

---

## 1. A medicine reminder in a chat must start the dose flow (user's item 2)

**Today.** Chat of 19 Oct 11:22 (u_id 2):

| Taipei | What happened |
|---|---|
| 11:23:09 | 「可以幫我提醒一下一分鐘吃藥嗎」: `set_reminder` with no medicine is refused (`nothing_to_remind`, `reminders.py:207, 216`), although the schema tells GPT-Live to pass neither name nor position for "my medicine" (`gpt_live_config.py:221-222`). |
| 11:23:35 | 「幫我提醒下一分鐘 secondmed」: reminder `604f02c4` set for 11:24:35. |
| 11:24:35 | Fired: 「時間到了，請記得吃藥：secondmed。」 said in the live voice (`delivered_via` live, `task_id` NULL). |
| 11:24:51 | Reachy answers the earlier weather question: "it kept talking". |

**Why.**
- A reminder only speaks: `reminders.fire` (`reminders.py:294-299`) calls `LiveRelay.speak`, which plays the line and
  appends `SPOKEN_NOTE` ("this was said"). No dose task is queued (none was created 11:21-11:35), so the heartbeat's
  `task_due` stays false. With no chat running, the reminder becomes a check-in chat (`_queue_checkin`), never a dose.
- GPT-Live's rules say 「不要主動提起藥物」 (`live_session.py:262`), and nothing tells it what to do after a reminder.
- No secondmed dose was due at 11:24 anyway (its only dose that day is 18:27; overdose protection is off for u_id 2).

**Change (recommended: queue a real dose task).**
1. Move the body of `manual_task` (`api_reachy.py:155-198`, the "Use Reachy" button) into a shared helper.
2. When a medicine reminder fires, pick the dose: the named medicine's open dose nearest to now, or the slot due at
   that time for "my medicine". Run the same overdose checks as the button, and bring the slot's other allowed doses.
3. Queue the task, write its id into `reminder.task_id`, and tell GPT-Live to start no new topic.
4. The robot already does the rest on 0.16.0: the next heartbeat (every 10 s) answers `task_due`, the robot waits for
   the patient to stop talking (at most 30 s), says 「該吃藥囉，我們等一下再聊。」, ends the chat, runs its own dose flow
   with the camera at full rate (`runner.py:671-673`, `session.py:225`), then starts a new check-in.
5. **A dose not due yet (the user's decision: "tell the person to wait"):** no task. Reachy says it is not time yet
   and when it is, e.g. 「secondmed 要到下午6點27分才吃喔，請再等一下。」. "Not due" is judged by the dose's own time
   (`schedule.due_from`, the 120-minute early window) even when overdose protection is off, so a reminder never starts
   a dose early.
6. A dose refused, taken or already waiting for family: no task; the line plus the reason.
7. With no chat running: queue the same dose task instead of a check-in (or, when not due, the check-in that says to
   wait).
8. Also: `set_reminder` accepts "my medicine" without a name. When it is set for a time when no dose is due, it says
   so then too (Claude's reading of "tell the person to wait"; the user can overrule it).

The other option, telling GPT-Live to run the medicine steps inside the chat, is server-only too, but it depends on
GPT-Live following five steps (item 0 shows it skipping the camera), and the chat camera runs at 2 fps, where the
intake detector is not calibrated.

**Tests.** A reminder in a live chat queues the right doses, `reminder.task_id` set, `task_due` true; not due, taken
or waiting: no task; a two-medicine slot with one refused; no duplicate task for a slot already queued; no chat: a dose
task; `set_reminder` for "my medicine". One robot test: 「一分鐘後提醒我吃藥」 with a dose due.

**Risks.** 10-40 s pass between the reminder line and 「該吃藥囉」. (A dose hours early is ruled out by decision 1.)

**Questions.**
1. ~~If no dose is due, start it early or only remind?~~ Decided: tell the patient to wait.
2. ~~Warn when it is set?~~ Planned as Claude's reading of decision 1; the user can overrule it.
3. After the dose, a new chat (the robot's current behaviour), or continue the same one?
4. Both lines (the reminder, then 「該吃藥囉」), or only 「該吃藥囉」?

---

## 2 and 3. Reachy hears family's answer, and encourages after "not taken" (user's items 1 and 3)

**Today.** Family's LINE tap reaches `handle_postback`, the only caller of `dose_confirmation.resolve`
(`dose_confirmation.py:417`). "Taken" records the dose (`caregiver_confirmed`); "not taken" puts it back to pending or
missed (`_restore`). LINE replies go to family. **Nothing reaches the robot or a running chat.** A request nobody
answers expires after 2 hours and tells no one. On 19 Oct 11:00:20 family tapped "not taken" while a live chat ran;
Reachy said nothing until someone in the room said 「阿公好像沒有吃藥」.

**Change (server only; robot 0.16.0 already plays everything needed).**
1. New `app/services/dose_news.py`, `after_resolution(result)`, called after the transaction commits, from
   `handle_postback` and from the expiry. It sorts the result: `taken`, `taken_not_recorded` (overdose protection
   refused it), `not_taken_now` (may still be taken), `not_taken_missed` (expired), `not_taken_later` (not due yet),
   `unanswered` (say nothing).
2. "May still be taken" is judged now with `dose_safety.verdicts`, and expiry is checked even when protection is off
   (with protection off a 10-hour-old dose would otherwise be encouraged).
3. The running chat (`reminders.find_relay`): a new `LiveRelay.announce_dose(news)` that waits for a quiet moment
   like `speak`, never after a risk. GPT-Live running: the new status in its instructions, then commentary (it says
   it in its own words and keeps talking). Paused: a fixed line in the live voice (`speak`). Taken: the applause.
4. **No chat (the user's decision: "wake up and speak"):** queue a check-in task that carries the news (like
   `reminders._queue_checkin` carries a reminder). The robot wakes, finds the patient, and the chat opens with the
   news right after its greeting; for "not taken, may take now" the dose task of step 5 instead. A new
   `announced_at` column on `dose_confirmation` makes sure it is said once (also when a chat starts meanwhile). Text
   chat: a note in the next reply's prompt.
5. **"Not taken, may take now" (item 3; the user: "tell 阿公 to eat his medicine and be more empathetic"):** the warm
   encouragement below, then the dose task of item 1, so the robot's dose flow runs and the camera watches the dose
   (encouragement alone could lead to an unwatched second dose after a wrong tap).

**Suggested words** (`{name}` from `memory.call_name`):
- Taken: 「{name}，家人剛剛確認您已經吃藥了，拍拍手！您越來越健康了！」
- Not taken, may take now: 「{name}，家人說這次的藥可能還沒吃到喔。沒關係，我在這裡陪您，現在慢慢把藥吃了好嗎？您照顧好身體，家人也會很放心，加油！」
- Not taken, missed: 「{name}，家人說這次的藥沒有吃到。這次已經錯過了，請不要補吃，下一次按時吃就好。沒關係，我陪著您。」
- Not taken, not due yet: 「家人說這次的藥還沒吃。現在還不用吃，時間到了再吃就好。」
- Taken but not recorded: no praise. 「家人回覆了，這次的藥沒有記錄為已服用；如果覺得不舒服，請告訴家人。」

No medical advice; the encouragement at most once per chat (the medicine rules allow two); the help line untouched; no
new LINE message.

**Tests.** The hook runs once per answer, only after the commit, and never breaks the webhook; the six outcomes with
protection on and off; commentary while running, the live-voice line while paused, nothing after a risk; news kept and
said once in the next chat; the text chat's note.

**Risks.** A wrong "not taken" tap can lead to a second dose (hence the camera-watched dose task). GPT-Live may
reword or drop a sentence of commentary. Two answers at once must merge into one line.

**Questions.**
1. ~~No chat running?~~ Decided: wake up and speak. New: should the robot also wake at night (for example between
   22:00 and 07:00), or keep the news for the morning?
2. ~~Only encourage?~~ Decided: tell 阿公 to take it, warmly; the plan adds the camera-watched dose task.
3. If the patient insists "I did take it", keep encouraging, or stop and ask family again?
4. "Taken": GPT-Live's own praise, or the fixed line?
5. Should family get a LINE note when a request expires unanswered (today nothing)?

---

## 4. An "End Reachy's session" button (user's item 4)

**Today.** No way to stop a running robot session from the web app. The Intake page's "End session"
(`Intake.tsx:580`) ends only the browser's camera session; the robot's 8042 page has only Restart, which resumes the
task. The Reachy card loads the robot status once and never refreshes it (`ReachyCard.tsx:63-70`).

**Change, server only (works with robot app 0.16.0).**
1. `/api/reachy/status` gains a `session` field: the held task, its conversation, whether a live chat runs, whether
   the chat has a risk flag.
2. `POST /api/reachy/stop` (behind `get_consented_user`): refuses when nothing runs or after a risk (the help line must
   play); aborts only the held task with reason `stopped_from_app` (no immediate retry); ends the open conversation
   with that reason and starts the after-chat work (the safety summary still runs); ends the robot's camera session
   (the robot then finds its task gone and sleeps, within about a second); for a live chat, a new `LiveRelay.stop()`
   sends a closing line and `end`.
3. The dose is never written: it stays pending, and the missed-dose rules (LINE reminders, retries, the missed alert)
   apply as usual. A family request already filed stays open.
4. One button, 「結束 Reachy 目前的服藥與聊天」, on the Reachy card and the dashboard's Reachy section, shown only while a
   session runs (the card polls every 5 s then), with a confirmation.

**Robot app 0.17.0 (about 1 day more):** the heartbeat names the task to stop (0.16.0's `stop_all` has no task id and
could stop the next task), Reachy says a short line before sleeping, a text chat whose camera never opened stops at
once, and an "I finished" claim in its 8 s wait is filed instead of lost.

**Tests.** Server: only the held task aborted, no retry queued, no dose row changed, camera ended, conversation reason
and after-chat, refused after a risk, consent gate. Playwright: the button shows only during a session. One robot test
in each state.

**Risks.** Words spoken right after the stop are refused unscreened: screen refused turns with the keyword list and
alert family on a match. A record or request made just before the stop stands.

**Questions.**
1. Who may press it (assumed: the patient's own web login)?
2. Should Reachy come back at the missed-dose retry for that slot (recommended: yes)? Still open: the user's answer
   of 19 Oct to this question was about the "not taken" case (decision 4).
3. What should Reachy say (e.g. 「好的，我先休息了，有需要再叫我。」), and always go to sleep?
4. Should a queued "Talk to Reachy now" task be cancelled too?
5. One button, or a separate "end the chat only"?

---

## 5. 「阿公，吃藥的時間到了，請吃藥。」 (user's item 2, second part)

**Today.** The line is the robot's wake greeting 「您好！吃藥的時間到了。」 (`wake_greeting`,
`bridge/clips/manifest.json:5-9`), played at the start of every dose task (`session.py:307`) in the robot's Matcha
voice, before the patient is found. The robot never receives the call name (the task payload's `patient_name` is the
registered name and is unused).

**Change.**
- Server: a new per-patient line `dose_greeting` in `voice_lines.py` (beside `opening` and `reminder_lead`):
  「{name}，吃藥的時間到了，請吃藥。」, or 「您好！吃藥的時間到了，請吃藥。」 when no name passes the rules; English too. The
  route, the consent gate and the render-ahead already handle per-patient lines; robot 0.16.0 ignores the new line.
- Robot 0.17.0: fetch `dose_greeting` before the greeting (at most about 5 s), play it when the copy is current, else
  the Matcha `wake_greeting`. Matcha cannot say a custom name (it drops words it cannot read, such as Latin letters),
  so the fallback has no name.

**Questions.**
1. Exact words: keep 「請吃藥」 right before 「請現在吃第一種藥，…」 (it may sound repeated)?
2. The retry line 「提醒您，現在是吃藥的時間。」: add the name too?
3. ~~Say the name before verification?~~ Decided: yes, at wake.
4. ~~Which name?~~ Decided: "the name defined in the system": the Reachy card's call name, else the registered name
   (today's `memory.call_name` rule); 「您好」 only when neither passes the name rules.

---

## 6. The dose-flow lines in the chat's voice (user's item 5)

**Today.** In one dose the patient hears the robot's Matcha voice (「您好！吃藥的時間到了。」, 「吃完藥以後，請跟我說…」,
「謝謝您！」), the live voice "marin" naming the medicine (server-rendered, `med_clips.py`), then marin again in the chat.
The same-voice work left the dose-flow lines out on purpose (spec non-goal v1).

**Two readings of "different voice".**
- A (most likely): the voice jumps. Fix: the same-voice "v2": add the dose-flow lines (`wake_greeting`, `reminder`,
  `say_when_done`, `thanks`, `help`, `confirm_with_caregiver`, `already_taken`, `searching`, `waiting_for_patient`,
  `med_prompt_generic`) to `voice_lines.FIXED_LINES`; robot 0.17.0 plays them in the live voice, with the Matcha WAV as
  the fallback.
- B: the user wants another voice for reminders. OpenAI's speech model offers 13 voices (alloy, ash, ballad, coral,
  echo, fable, nova, onyx, sage, shimmer, verse, marin, cedar); a "reminder voice" setting would bring back a voice
  jump between the reminder and the chat.

Recommendation: A. Cost is negligible (each line is rendered once, about US$0.001).

**Limit for both 5 and 6.** Server-rendered lines exist only while a live voice target exists, which needs
`LIVE_VOICE_TEST` (test only; `APP_ENV=prod` refuses it). Outside test mode everything falls back to Matcha. Making it
work for real patients needs its own switch and a notice that the call name goes to OpenAI (Phase 2b).

**Decided:** Reading A. The user: "It should use the gpt-live voice." Every dose-flow line, the call-name greeting of
item 5 included, is rendered in the live voice (marin), with the robot's Matcha WAV only as the fallback.

---

## Before building

- These items fit `tool-fixed-version-05`. When building starts, the questions deferred on 18 Oct are asked again too
  (the test medicine's leftover times, a face lock in chat, the voiceprint questions).
- Robot app 0.17.0 is deployed over SSH (`REACHY_SSH_PASSWORD` only in the environment), restarted through the
  daemon, and published to the Hugging Face space branch of the same name; this PC cannot push the robot's GitHub
  repo.
- Gates: server `pytest` and, for frontend changes, `npx tsc -b` and `npm run build`; robot tests; one real-robot test
  per item with the user.
