# Reachy stood silent after a reminder (17 October 2026)

The assessment of the live test of 17 Oct 2026, 22:02-22:24 (workflow `wf_75a110fa-d56`: four read-only investigators, a synthesis, two skeptics and a final merge, from the server and robot logs and the database). Code: server `tool-fixed-version-02` @ `8826c92`, robot app 0.16.0 @ `c56bbe2`. Times are Taipei time.

**Why Reachy stood still after you said you'd finished your medicine**

**1) The cause**

The camera matched the patient at 22:02:25. Three seconds later it was still seeing a face but no longer recognising it as the patient. The patient said "I finished" at 22:02:47, which was 19 to 22 seconds after the last match. The server only starts the live voice if the patient was matched in the last 15 seconds, so it refused, and the robot's only reaction to that refusal is a log line. So Reachy said nothing. Why recognition failed is still open. Weak face photos on file are at least as likely as the head angle.

**2) Evidence** (times are Taipei; server code is C:\Reachy_Mini\repo\MedAiCarePlus, robot code is C:\Reachy_Mini\repo\medcare-reachy-bridge)

- 22:02:25.46 "status=verified distance=0.135 box_px=128". The chat started at 22:02:27.64. At 22:02:28.69 the log says "status=mismatch ov_faces=1 mp_faces=1", meaning a face was there but did not match.
- The reminder was said at 22:02:31.46. The chat starts paused, and saying the reminder never starts the live voice (live_session.py:767, 797-839).
- At 22:02:47.88: "the patient was not seen in the last 15.0 s; GPT-Live stays paused" (live_session.py:202, 386-395, 1214-1219). The robot only logged "not seen by the camera lately" (live.py:517-519).
- When the patient spoke, the face was in view but not recognised: 22:02:46.14 and 22:02:51.66 both show "mismatch ov_faces=1 mp_faces=1".
- Recognition was already weak before the head moved. There were mismatches during the search at 22:02:08, 22:02:13 and 22:02:24. After recovery it kept flipping even with a large, close face: mismatches at 22:23:37, 22:23:42, 22:23:48 and 22:23:52, face boxes 159-265 px.
- From 22:03:41 to 22:22:41, every camera line says no face at all. The server refused speech 46 times, the last at 22:20:43.
- It recovered once the patient leaned in. Matched at 22:22:44, live voice started at 22:23:08, the medicine was recorded at 22:23:28, and the chat ended with a goodbye at 22:24:04.
- The two family messages failed with "LINE API 429 monthly limit".

**3) What the patient got, and what is lost**

After the reminder, Reachy went silent and still. "I finished" was not turned into text, checked or saved. While the live voice is paused, the server keeps only the last 2 seconds of sound (live_session.py:203, 1225-1238). The robot's own speech recogniser is off during a live chat (session.py:650). Risk checks and goodbye detection only run once the live voice is on (live_session.py:1481, 1519).

From 22:03:41 nobody was in front of the camera, so the later voices were probably not the patient talking to Reachy (not checked). Worst case: someone says "I have chest pain" while not recognised. That gets no risk check, no help line and no family alert. The silent chat also never ends while any voice reaches the robot (live.py:811, session.py:818-823).

**4) What to do now at the robot**

- Sit right in front of Reachy and close: every match tonight had a large face box. Look at its camera for 1-2 seconds before speaking.
- If it does not answer within about 3 seconds, lean in, look at it, and say it again.
- Check the dashboard for the dose record, and tell family yourself, because LINE is over its monthly limit.
- Try enrolling your face again using photos from the robot's own camera at your usual seat. No code is needed.

**5) Fixes, best first**

1. **Recommended:** the server speaks a short line when it refuses. It should wait until the patient stops talking and pick the words from what the camera sees:
   - Face seen but not matched: "I can't see your face clearly, come a little closer and look at me."
   - No face at all: "I can't see you, please sit in front of me."
   
   It should always answer the repeat after a cue, and after 2 failed cues say a closing line and end the chat. The installed robot app already plays it (live.py:509-512), in the Matcha voice (lines.py:26, 58, 81). Privacy cost: none.
2. The server logs the unmatched face's distance and size, numbers only (monitor_service.py:649-652). Privacy cost: none.
3. Listen for about 30 seconds right after a reminder. This is the only fix that would have heard tonight's "I finished". Privacy cost: up to 30 seconds of room sound goes to OpenAI, and it breaks the camera-first rule. That is your decision.
4. Next robot release (nothing blocks it: tools/deploy_to_robot.py exists):
   - Look up, then scan, when not seen.
   - Only accepted speech should keep the chat awake.
   - Log the head angle.
5. Add "tell me when you're done" only when the reminder is spoken. Not in the reminder template, because LINE sends the same text (reminders.py:297, 307).
6. Count the 30-second quiet pause from the end of Reachy's reply, not from the patient's last words (live_session.py:1060-1062). The first chat lost an answer this way at 21:51:11.

Don't lengthen the 15-second window. Don't tilt the chat head angle until the new logs show the head angle is the cause.

**6) What changes for the planned head_tracking tool**

As written, it would not have helped. It follows only the matched-face box, which the server sends only while the patient is matched (monitor_service.py:241), and there was none from 22:02:28 to 22:22:43. Changes:

- Aim at the face at the moment of the match.
- Add a numbers-only "aim box" sent when exactly one face is in view. It must never count as "seen".
- Look up, then scan, when not seen.
- Return to the last matched angle, never to level. This also applies to move_head's 8-second return and sweep_look's final hold_head.
- Make the tracked angle the starting point for gestures.
- Log the head angle.
- Have its status answer say whether the patient is in view.

**7) Still unverified**

- What the camera actually saw: head angle, face photos, or the patient moving.
- Which camera took the 3 enrolled face photos.
- The head angle at the match (it is not logged).
- Where the patient was from 22:03:41 to 22:22:41, and who was speaking. The robot journal ends at 22:05:20.
- Whether recording the 18:27 dose at 22:23 was correct (that dose now shows pending_confirmation).
- Whether the live voice stays quiet if fix 3 starts it.
- Handing a refused chat to today's text chat (session.py:661-664) instead of ending it. Not tested.