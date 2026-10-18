# Version 5 improvements (planned, not built) — 18 October 2026

The list for `tool-fixed-version-05`. **Nothing here is built yet**: the user asked to plan it and not build it for
now. Item 1 is the user's idea of 18 Oct, researched the same day; the others carry over from `HANDOFF.md`
section 12 and `docs/TOOL_PRIORITIES_2026-10-18.md`.

## 1. A patient voiceprint: record and remember only the right person

**Why.** In a live chat the patient's face is checked only when a GPT-Live stretch starts
(`live_session.patient_seen_recently`, `live_session.py:386`, used at `:1215`). Once GPT-Live runs, it answers
whoever speaks, and nothing checks who said what:

- a visitor's 「我吃完藥了」 files the patient's dose for family to confirm (`record_medication_taken`,
  `tool_registry.py:665`, has no identity check), and a dose waiting for family counts as taken for overdose
  protection, so it can block the patient's real dose for up to 2 hours;
- facts are learned from any voice in the room (`memory.grounded` on every "patient" turn, `after_chat.py:279`), and
  the `remember` tool checks only memory consent (`tool_registry.py:861-866`);
- medicine reminders can be set or cancelled by anyone (a cancel with no id cancels all of them,
  `reminders.py:231-232`).

The pill watch itself is safe: the detector scores only the recognised patient's face (`monitor_service.py:520`).

**Verdict of the research** (workflow `wf_60369082-084`, 18 Oct: models, accuracy and speed, our code, privacy):
**yes, with limits.** It runs on the laptop server, needs no change on the robot and no torch, and is fast. But its
answer comes about 1.5-2 s after the person stops talking, while GPT-Live's reply starts after 0.5 s
(`HOLD_SECONDS`), so **it controls what is recorded and remembered, not what Reachy answers**. It is a helper that
catches honest mix-ups, not a lock: it does not stop a recording or a cloned voice.

**How it would work.**

1. **Consent first.** A new consent kind `voice` (scope `voice_id`), off by default, switched on by the patient
   herself, like the memory and video kinds.
2. **Enrolment on Reachy.** A guided "voice setup" chat: Reachy reads 5-8 short prompts with GPT-Live paused; the
   server keeps the answers only while the camera verifies the patient with exactly one face in view (so a family
   member helping is not enrolled). About 30-60 s of speech over a few sessions, in every language she uses with
   Reachy. Only the averaged voiceprint is stored (192 numbers, encrypted, with the model name); never the audio.
3. **The check, on the server, per closed patient turn.** A copy of each microphone frame goes into a small ring
   buffer in `LiveRelay._microphone` (`live_session.py:964`; no delay to GPT-Live). Audio while Reachy speaks, and
   about 0.75 s after it, is left out (echo). A server-side voice detector (silero VAD, ONNX) cuts the speech; when
   the relay closes the turn (`TURN_GAP_SECONDS = 1.5`), each segment is scored in a worker thread (cosine similarity
   to the voiceprint). The turn is labelled **patient**, **not sure** (under about 2 s of clean speech, or a score
   between the two thresholds), **not the patient**, or **mixed** (counts as not verified).
4. **With the face.** A clear "not the patient" voice wins even with the patient in view (an off-camera visitor); a
   clear "patient" voice wins even with her face out of view; "not sure" with the patient verified and no other face
   seen for about 15 s counts as "probably patient" (enough to file a dose for family, not to save memory). The
   camera-first rule for starting a stretch stays as it is.
5. **What the label controls.**
   - A dose claim on the patient's word (GPT-Live's `record_medication_taken` and the relay's backstop claim):
     patient → as today; not sure → as today plus a voice line in the evidence family see; not the patient → no
     pending request (answer `speaker_not_patient`) and a dose-event note to family. A record backed by the camera
     detector needs no voice check.
   - Memory: facts only from patient-labelled turns; `remember` refuses otherwise.
   - Medicine reminders and cancels need a patient label; plain reminders stay open.
   - `notify_family`: a label in the note only, never blocked.
   - **Never blocked or delayed:** the keyword screen, the risk model, OpenAI's cut-off check, the help line,
     `emergency_alert`, stopping a dance, the audio path. A visitor's risk words still matter.
   - On "not the patient", GPT-Live can be told (through the running stretch's instructions) to be polite and take no
     medicine claim or personal fact from those words; Reachy is never cut off, because the visitor may need help.

**Model.** 3D-Speaker **CAM++, Mandarin** (`3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx`): 28 MB, 7.2 M
parameters, trained on about 200k Chinese speakers, Apache License 2.0, suggested threshold 0.31, 4.32% equal error
rate on CN-Celeb. Run on the server's own onnxruntime plus `kaldi-native-fbank` (0.4 MB, Apache-2.0) for the audio
features; the image grows by about 30 MB, still with no torch. Measured on this laptop (i7-10750H, one thread): about
40-47 ms for 1.5-2 s of speech and 68-70 ms for 3 s. Backup: the Mandarin+English CAM++ (`zh_en common_advanced`,
5.98%). Not to use: Resemblyzer, SpeechBrain and wespeakerruntime (need torch); WeSpeaker's CN-Celeb model (CN-Celeb
forbids commercial use); TitaNet (English only). OpenVINO, already in the image, could also load the ONNX file.

**Limits** (from published results; none tested on Taiwanese voices over 75):

- Short speech is unreliable: a 1-2 s 「我吃完藥了」 is at the edge, so "not sure" will be common.
- Older voices: one ECAPA model went from 20.88% to 5.66% error on Mandarin speakers aged 75-85 only after
  fine-tuning (SeniorTalk).
- A TV or two people talking, distance (about 9-12% across 0.1-4 m), dialects (11-14.5%), a cold, same-sex relatives
  in the home, and speaking a language she did not enrol in all raise errors.
- The thresholds cannot be copied from papers (0.31 on the model card, 0.6 in the sherpa-onnx example): they need
  about a week of this household's own turns.
- It does not reduce what reaches OpenAI (GPT-Live still hears every voice), and it does not cover the text chat or
  the robot's own 「我吃完了」 listener in its dose flow.

**Privacy.** Under Taiwan's PDPA a voiceprint is personal data but not a "special category" (our reading of the PDPC
Preparatory Office's letter of 2 Jan 2026 on iris data; no ruling on voiceprints was found); the basis is consent
(Article 19(1) item 5) after the Article 8 notice. GDPR Art. 9 and the EDPB's voice-assistant guidance treat voice
identification as sensitive biometric data, so build it that way: off by default, enrolled by the patient herself,
only the 192 numbers stored, encrypted, in this server's database, never sent to OpenAI, the robot, LINE or family,
deleted at once on withdrawal (in the consent transaction) and on account deletion; a visitor's voice compared in
memory only, never stored or labelled; a card on Reachy tells visitors. **Two gaps in today's code come first:**

- `deletion_ledger.replay`'s consent branch only re-adds the withdrawn consent row and deletes no data
  (`app/services/deletion_ledger.py:115-126`), so a restored backup would bring a withdrawn voiceprint back;
- the robot notice still says no audio leaves the home (`app/legal/robot/2026-10/en.json:36` and `:76`), while
  GPT-Live already receives the room's audio: the voice notice can only ship with, or after, the Phase 2b live-voice
  notice.

Also open: who is the operator (data controller), and who may consent for a patient with dementia or under
guardianship (a question for a lawyer or the PDPC).

**Size and rollout.** About 3-5 days of building and tests, almost all in this repo (new `app/services/voiceprint.py`,
the model in `models/` pinned by SHA-256, `live_session.py`, `tool_registry.py`, `after_chat.py`, the consent kind and
its notice, the web app's voice setup), then **1-2 weeks of "shadow mode"**: labels computed and logged, nothing
blocked, compared with turns where she was alone and verified on camera, to set the two thresholds. Then the gates go
on. The robot repo needs no change. Before it: re-enrol the patient's face from the robot's own camera
(`docs/TOOL_PRIORITIES_2026-10-18.md` item 5), because the voice fuses with the face.

**Questions for the user** (open):

1. When the voice is "not sure", should a dose claim still go to family with a voice note (recommended), or be refused?
2. Who else is often in the room (a spouse, a same-sex adult child, a caregiver)? Would they give a few labelled test
   recordings, used once to set the thresholds and then deleted?
3. Does the patient speak Taiwanese Hokkien or Hakka to Reachy, or only Mandarin (and English)?
4. Should a visitor's goodbye still end the patient's chat?
5. Is a week of shadow mode before the gates go on all right?

**Sources:** the CAM++ model cards on ModelScope
([zh-cn](https://www.modelscope.cn/models/iic/speech_campplus_sv_zh-cn_16k-common/summary),
[zh_en](https://www.modelscope.cn/models/iic/speech_campplus_sv_zh_en_16k-common_advanced/summary));
[3D-Speaker](https://github.com/modelscope/3D-Speaker);
[sherpa-onnx speaker models](https://github.com/k2-fsa/sherpa-onnx/releases/tag/speaker-recongition-models);
[kaldi-native-fbank](https://pypi.org/project/kaldi-native-fbank/);
[ERes2NetV2 short-utterance results](https://arxiv.org/abs/2406.02167); [SeniorTalk](https://arxiv.org/html/2503.16578);
[CAM++](https://arxiv.org/html/2303.00332v3); [the 3D-Speaker dataset](https://arxiv.org/html/2306.15354);
[Taiwan PDPA](https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=I0050021). The full research result is in the session
that wrote this (workflow `wf_60369082-084`).

## 2. Carried over for version 5

From `HANDOFF.md` section 12 and `docs/TOOL_PRIORITIES_2026-10-18.md`:

1. **The server's access log writes each family video link in full, token included** (`/api/media/line/...`), while
   the design keeps only the token's SHA-256: mask that path in the access log. First, because it is a security fix.
2. The P0 fixes of the priority list: the spoken cue when the camera does not recognise the patient (item 8), the quiet
   pause counted from the end of Reachy's reply (item 9), honest delivery answers when LINE refuses (item 10), the real
   status in the tool log line (item 11), the due check listing every open dose (item 6), the rule for an expired
   family request (item 7, the user decides), a send-by time for outbox messages (item 3).
3. The startup report logging `line: FAILED` after the public link recovers (`startup_report.py:185-186`).
4. The face lock (`head_tracking`, with the design changes of priority item 21).
5. The OCR test on the robot (`identify_medicine`, `verify_medication`).
6. The new tools' implementation plan (robot app 0.17.0).
7. After the robot app starts with no task (for example when the Reachy Mini Control app plays its wake-up move and
   starts it), the robot stays in the wake-up pose until its next task: the app calls `sleep` only at the end of a task
   (robot `session.py:940, 1288, 1314`). Seen on 18 Oct 19:38-20:22. Put it to sleep at start when no task is running.
