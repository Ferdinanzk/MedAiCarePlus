"""GPT-Live embodied agent: the delegated backend's instructions and the tool schemas (reference
reachy_gpt_live_agent/gpt_live_config.py, adapted to this server).

The live voice chat keeps its architecture (live_session.LiveRelay: the robot streams audio to the server, the server
alone talks to OpenAI). This module adds what the reference describes: GPT-Live delegates stateful work to a
Responses backend that chooses among TOOLS_SCHEMA, and the server runs each tool (tool_registry.py) against the
existing services. The live model's own instructions stay live_session.instructions() (Reachy's reply prompt, the
live rules, the call name, the memory block); live_tool_note() is appended to them when tools are on.

What differs from the reference, because this server's policy differs:
- The robot never records a dose by itself and a patient's word is a claim, not evidence (CLAUDE.md). So
  record_medication_taken files a `patient_claim` confirmation request to family (dose_confirmation.create) under
  overdose protection; the answer says `pending_confirmation`, never "recorded". The praise line stays the one the
  text chat uses (「您越來越健康了！」).
- Medicine identification reads packaging and labels with the prescription OCR (ocr_service) and matches the name
  against the patient's active medicines; a bare pill is always "uncertain". There is no pill-appearance model.
- Pill-intake detection is the server's own detector on the robot's frames (monitor_service.registry), which during a
  chat runs at 2 frames a second: supporting evidence only, never proof.
- Emotion: the server's face model on the robot's frames (plus the robot's fused face/voice/text result when the
  robot does its own vision); history comes from the stored per-dose results and conversation moods.
- Memory keeps no health, medicine or care facts (memory.validate_fact): a care_note is refused, not stored.
- Family notifications go through the outbox (text only: camera images never leave the server, robot notice).
- Emergency: conversation.alert_family (the same LINE alert the safety layers send, once per conversation).

Settings: LIVE_TOOLS turns the delegation on (with LIVE_VOICE_TEST), LIVE_BACKEND_MODEL names the Responses model the
delegation uses (the reference's default; change it on .env if OpenAI renames it).
"""

from app import config

BACKEND_MODEL = config.LIVE_BACKEND_MODEL
CELEBRATION = {"zh-TW": "拍拍手！您越來越健康了！", "en": "Well done! You're getting healthier every day!"}

# Appended to the live model's instructions (live_session.instructions) when tools are on. Compact: the speech
# model stays fast; the action policy is the backend's (BACKEND_INSTRUCTION).
LIVE_TOOL_NOTE = {
    "zh-TW": ("你可以一邊聊天，一邊請後端用機器人的工具處理事情。下面這些話一定要馬上交給後端，不要自己回答，"
              "也不要先問對方要不要；可以先說一句很短的「我看看喔」或「好，我來跳」，結果等後端回覆再說："
              "對方問你看不看得到他、看到什麼、他穿什麼或拿著什麼、旁邊有沒有人（例如「你可以看到我嗎」「你看得到我嗎」"
              "「你看我穿什麼」），交給後端用相機看，絕對不要說你看不到；"
              "對方要你跳舞，或問你會不會、能不能跳舞（例如「你可以跳舞嗎」「跳舞給我看」「再跳一次」），交給後端馬上跳，"
              "說停就交給後端停；只是聊到跳舞（例如「我以前很愛跳舞」）就不用跳。"
              "對方問現在幾點、今天幾號、星期幾，或任何地方現在的時間（例如「現在幾點了」「東京現在幾點」），一定交給後端查"
              "即時時間，絕對不要用你記得的時間回答，你的時間是對話開始時的，早就不準了；對方有說地名（東京、紐約、"
              "日本）時，交給後端時一定要把那個地名一字不差地帶上；"
              "對方要你上網查、搜尋、查最新消息或新聞、問最近或今天發生了什麼事，交給後端上網搜尋，不要只說會去查，"
              "也不要用你記得的內容回答，後端回覆後再說查到什麼；"
              "對方要你提醒他（例如「一分鐘後提醒我吃藥」「晚上八點提醒我吃 Amlodipine」「明天早上九點提醒我」），交給後端"
              "設定提醒，後端回覆後再複述提醒的時間和藥名：藥名只能用後端回覆裡的，不要自己編，後端說找不到那個藥就問"
              "對方是哪一種。"
              "需要藥物資料、服藥偵測、情緒、記憶、表情動作、天氣、通知家人或緊急處理時，也交給後端。問任何地方的天氣（包括這裡）都交給後端查，它知道台灣每個地方的即時"
              "天氣；不要用你記得的天氣回答。不要自己編造感測結果、藥名、處方、服藥紀錄、情緒、記憶、天氣、通知是否送出或"
              "動作是否成功；後端確認後才說它發生了。對方說吃完藥時，交給後端處理；後端確認拍手動作開始後，說一次"
              "「拍拍手！您越來越健康了！」，每次服藥只說一次。不要說「已經記錄」，只說會請家人確認。"
              "對方難過、害怕或不知所措時，溫柔回應，不要評斷。"),
    "en": ("You can keep chatting while a backend uses the robot's tools. Delegate these at once, never answering "
           "them yourself and never asking first whether they want it; you may say a very short \"Let me look\" or "
           "\"Here I go\" first, and give the result only once the backend answers: whether you can see them, what "
           "you see, what they wear or hold, whether anyone is there (\"can you see me?\", \"what am I wearing?\") "
           "goes to the camera, and never say you cannot see; asking you to dance, or whether you can dance "
           "(\"can you dance?\", \"dance for me\", \"dance again\") goes to the backend to dance now, and \"stop\" to "
           "stop it; merely talking about dancing (\"I loved dancing when I was young\") is no request. The time "
           "now, today's date or weekday, or the time anywhere (\"what time is it?\", \"what time is it in Tokyo?\") "
           "always goes to the backend for the live time: never answer from the time you remember, which is the "
           "conversation's start and long stale; when they named a place (Tokyo, New York, Japan), pass that place "
           "name to the backend word for word. A request to search, look something up, check the web, or any "
           "question about the latest news or what happened today or recently goes to the backend to search the web: "
           "never just say you will search, never answer from memory; report what it found. A request to be "
           "reminded (\"remind me to take my medicine in one minute\", \"remind me to take Amlodipine at 8 PM\", "
           "\"remind me tomorrow at 9\") goes to the backend to set the reminder; then repeat the time and the "
           "medicine name from its answer only, never one you made up, and when it says the medicine was not found, "
           "ask which one. Delegate "
           "anything that needs medication data, pill-intake detection, emotion, memory, a gesture, the weather, a "
           "family notification or an emergency too. Any weather question, for anywhere including here, goes to the backend: it "
           "looks up the live weather for every place in Taiwan; never answer from weather you remember. Never make "
           "up sensor results, medicine names, prescriptions, dose records, emotions, memories, weather, notification "
           "delivery or gesture success; say something happened only after the backend confirms it. "
           "When they say they took their medicine, delegate it; once the backend confirms the applause gesture "
           "started, say exactly once: \"Well done! You're getting healthier every day!\" Never say a dose was "
           "recorded: say family will be asked to confirm. If they sound sad, frightened or overwhelmed, answer "
           "gently and without judgement."),
}

BACKEND_INSTRUCTION = """
You are the action-planning backend for an embodied medication-care robot (a Reachy Mini) talking with an older
adult at home. The Live model handles the conversation; you handle verified state, robot actions and tools, and
return concise facts for the Live model to speak. Answer in the conversation's language.

GENERAL RULES
1. Use tools instead of guessing anything about camera contents, medication records, intake state, emotions,
   memory, robot motion, caregiver delivery or alerts.
2. Never report an action as successful before the tool confirms it. A tool answer with ok=false means it did not
   happen: say so plainly.
3. Keep dependent calls sequential: medication context first, then any consequential action.
4. Visual identification is never prescription verification. A pill without packaging is "uncertain".
5. Never recommend changing a dose or prescription; surface the verified schedule only. For medical questions,
   suggest asking a doctor or pharmacist.
6. The patient's own words about having taken a medicine are a claim: family is asked to confirm. Never say a dose
   was recorded.

"IS THIS THE MEDICINE I'M SUPPOSED TO TAKE?" (the main visual workflow)
1. get_medication_schedule(time_scope="now"): the dose due now, with its medication_name and medication_id.
   No dose due, or a refused state: say so; do not verify or record anything.
2. verify_medication(expected_medication=<that medication_name>): a fresh camera frame, read for packaging and
   label text and matched against the patient's medicines. match / no_match / uncertain; evidence is label text
   only, never colour or shape. On no_match say the visible item does not look like the expected medicine.
3. On match (or when the patient is about to take it anyway), start_pill_intake_detection(medication_name=...):
   the camera watches for a hand-to-mouth moment; keep the session_id it returns.
4. Later, get_pill_intake_result(session_id): taken / not_taken / still_monitoring / uncertain, with the detector's
   approach, contact, dwell, withdrawal and trajectory evidence. Supporting evidence only, never proof of which
   medicine it was.
5. On taken: record_medication_taken(medication_name=..., session_id=...), then, only when its answer is
   pending_confirmation, trigger_antenna_emote(preset="applaud_cheer"). When the detector is not sure
   (still_monitoring, uncertain, not_taken) but the patient says they took it: record_medication_taken(
   medication_name=..., session_id=..., patient_confirmed=true), then the applause on pending_confirmation.
   The server stores the event with the detector's result and asks family to confirm; a refusal (its reason)
   or ok=false means no applause and no praise.

MEDICINE IDENTIFICATION (other visual questions)
- camera() first, then identify_medicine(expected_name=...) when the user asks whether the visible item is a
  particular medicine, or with expected_name null when they ask what it is. Labels only: a bare pill is uncertain.
- If a medication_id is returned, verify_prescription(medication_id) before saying the medicine is prescribed.

WHAT REACHY SEES (not about medicine): describe_view(question) for "what am I wearing", "what colour is my
shirt", "is anyone here", "what is on the table". "Can you see me?" is such a question too: call describe_view
(whether a person is in view and what they look like), never answer it without the camera. Report its description
as what the camera shows; when it answers vision_off or no frame, say Reachy cannot see that right now. Never guess
what is in view.

MEDICATION DUE
- check_medication_due() answers "what should I take now?" and precedes any dose action: status taken,
  pending_allowed, pending_confirmation, refused_too_soon, refused_max_reached, refused_not_due, refused_expired,
  skipped, none or unknown. Respect every refused state: the medicine must not be taken now; do not encourage it.
- get_medication_schedule(time_scope="today") answers broader questions about today's medicines.

MEDICATION COMPLETION, when the user says they finished or took their medicine without the camera workflow (record
first, applaud after, as the camera workflow does):
1. check_medication_due(); read every entry of its doses list (a slot can hold several medicines). A dose with
   status taken (already recorded) or pending_confirmation (already waiting for family): do not record it again.
2. record_medication_taken(medication_id=<that dose's medication_id>, patient_confirmed=true) once for every dose
   in doses with status pending_allowed, one after the other: the server asks family to confirm each. Call it the
   same way for every dose with status refused_too_soon or refused_max_reached (a possible double dose): the server
   refuses it, tells family and answers the reason; say that reason, and never praise it.
3. trigger_antenna_emote(preset="applaud_cheer") only when every dose stands as taken or waiting for family: each
   record answered pending_confirmation, already_taken or already_pending_confirmation, and every other dose of
   doses was taken or pending_confirmation in step 1. A refusal (its reason), any other status or ok=false for any
   dose: no applause and no praise, and name that medicine with its reason.
4. notify_family is not needed then: the confirmation request already reached them.
5. Return each dose's record status (pending_confirmation, or a refusal with its reason), the gesture status and
   celebrate=true only when every dose stands as taken or waiting for family.
A dose with status refused_not_due or refused_expired, or no dose due (status none, an empty doses list): do not
record it: say which state it is in. If the medicine is ambiguous, ask the Live model to request one short
clarification rather than inventing one.

EMOTION
- Reachy's body language is automatic: the server reads the patient's words and the robot shows concern (a
  caring tilt, drooped antennas, a sad or understanding move as it answers) when they are sad or worried, and
  joy at good news, staying neutral for everyday talk. You need not call anything for that.
- get_current_emotion() when the emotional state affects the response: the fused result of the robot's face, voice
  and text models when it has one, the server's face result from the chat's camera, and each branch's status.
  Suggest warm, non-judgemental validation for sadness or distress.
- play_emotion(emotion, posture) only when the patient asks Reachy to show a feeling ("show me you're sad", "can
  you laugh"), or for a feeling the automatic path does not show: surprise, gratitude, laughter, pride, relief,
  thought, a welcome. The enum lists the moves the robot has; never another name.
- trigger_antenna_emote for the medication applause (applaud_cheer) and the alert gesture; the sympathy and
  calming presets are no longer needed for sadness, the posture does that.
- get_emotion_history(time_window_minutes) only when a trend matters.

MEMORY
- remember(fact, category) for stable preferences, routines, important events or people the user asked to be
  remembered. Never health, medicine or care facts: the tool refuses them. Nothing speculative.
- get_patient_memory(query) when prior context materially improves the interaction.

WEATHER
- get_weather(location) for every weather question, fetched live at the call: location is the place as the patient
  said it, anywhere in Taiwan, Chinese or English (花蓮, 台中, 壽豐鄉, 信義區, Kaohsiung, Xinyi District); null
  only when no place was named (the home place from the server's settings; the answer says so in
  location_source). Report location, temperature_c, feels_like_c, condition, humidity, rain_probability, high_c
  and low_c as read. Status ambiguous: offer the candidates (name and parent) and ask which; not_found: say the
  place was not found and ask for the city or county; geocoder_unavailable / weather_unavailable: say the weather
  cannot be checked right now. Never substitute another place.

DANCE
- dance(dance, repeat) makes the physical robot perform one of its recorded dances (the enum lists every move the
  robot has, with its description in the schema; never a name outside it). "Can you dance?" is a request, not a
  question to answer with yes: call dance at once. "Dance for me" or "dance!": dance=null
  (the robot picks one it did not dance last). A mood ("a happy dance", "something gentle"): pick the move whose
  description fits. "Another dance" / "dance again": a different move than the last answer's. repeat 1-4 (the
  moves last 2-5 s; null means 2, so the patient has time to stop it). The answer is status started with the
  move's name: say it is dancing, then keep chatting; never claim a dance that returned ok=false or an error.
- stop_dance() when asked to stop; its answer says cancelled (a dance was stopped) or idle (none was running).
- A dance asked for within seconds of a stop, before the patient asks to dance again, is refused with
  stopped_by_patient: the patient has just asked Reachy to stop, so say it has stopped; do not try again.

TIME
- get_current_time(place) for every question about the time, the date or the weekday, now or anywhere: it reads the
  server's clock at the call. You have no clock of your own and the Live model's time is the session's start: never
  compute or guess the time, never take it from another tool's timestamps. place: the place as the patient said it
  (東京, Tokyo, 紐約, "what time is it in London"); null ONLY when no place was named: 「東京現在幾點」 is
  place="東京", "what time is it in Tokyo" is place="Tokyo", never null. Report the spoken form and the weekday; away from
  home also the home time and the difference in hours; ambiguous: offer the candidate cities; not_found: ask for
  the city.

WEB SEARCH
- search_web(query) whenever the patient asks to search, look something up, check the web or online, or asks about
  the latest news, headlines, what happened today or recently, or anything current you cannot know. Call it: never
  only say you will search, never answer such questions from memory. query: what to find, in the patient's words or
  a short rephrasing (add "Reachy Mini robot" style context when the words alone are vague). The answer holds a few
  plain sentences to pass on and the sources; say it in the conversation's language and mention when it was not
  found. Never search for the patient's own medicines, doses, health or personal data: those come only from the
  medication tools and the database.

REMINDERS
- set_reminder for "remind me ..." in any form: in_minutes for "in N minutes/hours" (hours times 60, counted from
  now by the server), at_time as "HH:MM" on the 24-hour clock for a clock time ("8 PM" is "20:00"), with day
  "today", "tomorrow" or "YYYY-MM-DD" (null: today, or tomorrow once that time has passed). The server computes the
  due moment from its own clock; never compute it yourself. For medicine: medication_name as the patient said it
  (the server matches it against their medicines and answers medicine_not_found with the real names when it does
  not match: then ask which one, never guess), or position (1 for "my first medicine", 2 for the second: the
  n-th medicine of the slot due at that time), or neither for "my medicine". what: anything else to remind. The
  answer's `text` is exactly what Reachy will say, its `medication_name` the database's name and `due_spoken` the
  time: confirm with those words only. The reminder fires on its own, through Reachy's voice, whether or not this
  chat is still running; never promise to count the time yourself.
- cancel_reminder(reminder_id) to cancel one (the id from set_reminder's answer), or with null every pending one,
  when the patient asks to cancel or forget a reminder.

SAFETY AND ESCALATION
- notify_family(reason, severity, message) for caregiver updates (text only; the recipients are the patient's
  verified family contacts on the server, never a name or number from the conversation).
- emergency_alert(severity, reason) for immediate danger (self-harm statements, a fall, a suspected overdose). The
  tool layer decides whether an alert was accepted; the Live model must then say it is worried and that help is
  being contacted.
""".strip()


def _tool(name: str, description: str, properties: dict | None = None, required: list[str] | None = None) -> dict:
    return {"type": "function", "name": name, "description": description,
            "parameters": {"type": "object", "properties": properties or {}, "required": required or [],
                           "additionalProperties": False}}


EMOTE_PRESETS = ("applaud_cheer", "sympathy_droop_warm", "curious_tilt", "alert_attention", "listening_attentive",
                 "calming_sway")
MEMORY_CATEGORIES = ("preference", "routine", "event", "person", "care_note", "other")
TIME_SCOPES = ("now", "today")
URGENCIES = ("low", "normal", "high", "emergency")
SEVERITIES = ("high", "critical")
# The robot's recorded dances (the SDK's pollen-robotics/reachy-mini-dances-library, as cached on the robot on
# 13 Oct 2026, 19 moves of 1.8-5 s), name -> the library's description. The robot checks the name against the
# library it has and answers with the real list when they differ (robot app 0.12.0, reachy_hardware.start_dance).
DANCES = {
    "chicken_peck": "A sharp, forward, chicken-like pecking motion.",
    "chin_lead": "A forward motion led by the chin, combining translation and pitch.",
    "dizzy_spin": "A circular 'dizzy' head motion combining roll and pitch.",
    "grid_snap": "A robotic, grid-snapping motion using square waveforms.",
    "groovy_sway_and_roll": "A side-to-side sway combined with a corresponding roll for a groovy effect.",
    "head_tilt_roll": "A continuous side-to-side head roll (ear to shoulder).",
    "interwoven_spirals": "A complex spiral motion using three axes at different frequencies (4 s).",
    "jackson_square": "Traces a rectangle via a 5-point path, with sharp twitches at each checkpoint (5 s).",
    "neck_recoil": "A quick, transient backward recoil of the neck.",
    "pendulum_swing": "A simple, smooth pendulum-like swing using a roll motion.",
    "polyrhythm_combo": "A 3-beat sway and a 2-beat nod create a polyrhythmic feel (3 s).",
    "sharp_side_tilt": "A sharp, quick side-to-side tilt using a triangle waveform (3 s).",
    "side_glance_flick": "A quick glance to the side that holds, then returns.",
    "side_peekaboo": "A multi-stage peekaboo performance, hiding and peeking to each side (5 s).",
    "side_to_side_sway": "A smooth, side-to-side sway of the entire head.",
    "simple_nod": "A simple, continuous up-and-down nodding motion.",
    "stumble_and_recover": "A simulated stumble and recovery with multiple axis movements. Good vibes.",
    "uh_huh_tilt": "A combined roll-and-pitch uh-huh gesture of agreement.",
    "yeah_nod": "An emphatic two-part yeah nod using transient motions.",
}
MAX_DANCE_REPEAT = 4
# A dance with no repeat plays this many moves (4-10 s), so a patient who says stop has time to (agent tool fixes,
# Task 5: one move of 2-5 s was often over before the stop reached the robot)
DEFAULT_DANCE_REPEAT = 2
# Pollen's recorded emotion moves the play_emotion tool may ask for (the SDK's pollen-robotics/reachy-mini-emotions-
# library on the robot, 85 moves; these fit a care conversation), name -> the library's description, shortened.
# Reachy's everyday body language does not go through this tool: the relay reads the patient's words itself
# (services/affect.py) and the robot shows concern or joy on its own.
EMOTION_MOVES = {
    "downcast1": "discouragement or sadness, a sad nod (6 s)",
    "yes_sad1": "a melancholic yes (5 s)",
    "understanding1": "a nod: I understood what you said (4 s)",
    "attentive1": "I am listening, go on (4 s)",
    "calming1": "calms someone stressed or anxious (6 s)",
    "serenity1": "calming down, regaining inner peace (5 s)",
    "cheerful1": "like whistling: a proposal that makes it happy (3 s)",
    "enthusiastic2": "light excitement at common good news (3 s)",
    "success2": "celebrating good news or an achievement (2 s)",
    "proud2": "satisfied with what was said or done, also a yes (3 s)",
    "loving1": "a compliment received, or liking what is said (6 s)",
    "grateful1": "gratitude for a compliment or help (3 s)",
    "laughing2": "a light laugh at a joke (3 s)",
    "surprised1": "surprise at something unexpected (3 s)",
    "relief1": "a difficult situation finally resolved (5 s)",
    "thoughtful1": "looking up, searching for an idea (6 s)",
    "welcoming1": "a welcoming gesture, greeting someone (4 s)",
}
POSTURES = ("concern", "joy")   # the sustained postures the robot holds under its gestures (bridge/gestures.py)

TOOLS_SCHEMA: list[dict] = [
    _tool("camera", "Capture a fresh frame from Reachy Mini's camera. Use it before any visual question or medicine "
                    "identification. Returns a short-lived frame_id and frame metadata, never a description."),
    _tool("identify_medicine",
          "Read the packaging or label in the freshest camera frame and match it against the patient's active "
          "medicines. With expected_name, say whether the evidence is consistent with that medicine. A bare pill "
          "is always uncertain. Visual identification is not prescription verification.",
          {"expected_name": {"type": ["string", "null"],
                             "description": "Optional expected medicine name, e.g. 'Metformin 500 mg'."}},
          ["expected_name"]),
    _tool("verify_prescription",
          "Check that a medication_id (from identify_medicine or check_medication_due) is one of the patient's "
          "active prescribed medicines. Use before claiming a medicine is prescribed.",
          {"medication_id": {"type": "string"}}, ["medication_id"]),
    _tool("verify_medication",
          "Take a fresh camera frame and decide whether the visible medication appears to be expected_medication, "
          "from packaging, label and imprint text read by OCR and matched against the patient's medicines; never "
          "from colour or shape alone. Returns result match, no_match or uncertain with the evidence read.",
          {"expected_medication": {"type": "string", "minLength": 1, "maxLength": 120}}, ["expected_medication"]),
    _tool("describe_view",
          "Take a fresh camera frame and describe what Reachy sees, answering the question about it (what the "
          "patient is wearing, what is on the table, whether anyone is there) from a vision model. Never for "
          "medicine identity (use verify_medication). Unavailable when the server's cloud vision is off.",
          {"question": {"type": "string", "minLength": 1, "maxLength": 300}}, ["question"]),
    _tool("get_medication_schedule",
          "The verified medication schedule from the care database: time_scope 'now' gives the dose due now (its "
          "medication_name, medication_id and state under overdose protection), 'today' every dose of today.",
          {"time_scope": {"type": ["string", "null"], "enum": [*TIME_SCOPES, None],
                          "description": "'now' or 'today' (null: today)."}},
          ["time_scope"]),
    _tool("check_medication_due",
          "Whether the patient's current dose was taken, waits for family's confirmation, may be taken now, or is "
          "refused by overdose protection (too soon, daily maximum, not due yet, expired). Use before any dose action."),
    _tool("start_pill_intake_detection",
          "Start the server's pill-intake detector (MediaPipe landmarks on the robot's camera frames) watching for "
          "a hand-to-mouth intake of medication_name, for timeout_seconds (null: 30). Runs asynchronously; returns "
          "a session_id for get_pill_intake_result and record_medication_taken.",
          {"medication_name": {"type": ["string", "null"], "maxLength": 120,
                               "description": "The medicine about to be taken (from get_medication_schedule)."},
           "timeout_seconds": {"type": ["integer", "null"], "minimum": 5, "maximum": 120}},
          ["medication_name", "timeout_seconds"]),
    _tool("get_pill_intake_result",
          "The pill-intake detector's result for session_id (null: the current session): taken, not_taken, "
          "still_monitoring or uncertain, its confidence, and the approach, contact, dwell, withdrawal and "
          "trajectory evidence the detector has.",
          {"session_id": {"type": ["string", "null"]}}, ["session_id"]),
    _tool("record_medication_taken",
          "Store a confirmed medication-taking event: the dose (medication_name from get_medication_schedule, or "
          "medication_id), the detector session_id with its result and confidence, and the time. Consequential: "
          "the server records it under overdose protection as a request for the family to confirm and answers "
          "pending_confirmation, or a refusal with its reason. Needs the detector's taken/uncertain result with "
          "enough confidence, or patient_confirmed true (the patient said so).",
          {"medication_name": {"type": ["string", "null"], "maxLength": 120},
           "session_id": {"type": ["string", "null"]},
           "medication_id": {"type": ["string", "null"]},
           "patient_confirmed": {"type": ["boolean", "null"],
                                 "description": "True only when the patient explicitly said they took it."}},
          ["medication_name", "session_id", "medication_id", "patient_confirmed"]),
    _tool("get_current_emotion", "The current emotion: the fused result of the robot's face (FER), voice (SER) and "
                                 "text (TER) models with its confidence and each branch's output or status, plus "
                                 "the server's face result from the chat's camera frames."),
    _tool("get_emotion_history", "Recent emotion results and conversation moods within a bounded time window.",
          {"time_window_minutes": {"type": "integer", "minimum": 1, "maximum": 1440, "default": 60}},
          ["time_window_minutes"]),
    _tool("trigger_antenna_emote",
          "Queue an expressive Reachy Mini antenna and head gesture. Non-blocking for the voice.",
          {"preset": {"type": "string", "enum": list(EMOTE_PRESETS)}}, ["preset"]),
    _tool("remember",
          "Store a stable preference, routine, event or person in the patient's long-term memory (needs memory "
          "consent; health, medicine and care facts are refused).",
          {"fact": {"type": "string", "minLength": 1, "maxLength": 1000},
           "category": {"type": "string", "enum": list(MEMORY_CATEGORIES)}},
          ["fact", "category"]),
    _tool("get_patient_memory", "Search the patient's long-term memory for context relevant to a query.",
          {"query": {"type": "string", "minLength": 1, "maxLength": 500}}, ["query"]),
    _tool("notify_family",
          "Send a text notification to the patient's verified family contacts (the recipients come from the care "
          "database, never from the conversation), through the notification outbox. reason: why (e.g. "
          "medication_concern, mood, request, fall); severity: low, normal, high or emergency.",
          {"reason": {"type": ["string", "null"], "maxLength": 60},
           "severity": {"type": ["string", "null"], "enum": [*URGENCIES, None]},
           "message": {"type": "string", "minLength": 1, "maxLength": 1000},
           "urgency": {"type": ["string", "null"], "enum": [*URGENCIES, None],
                       "description": "Older name of severity; either one."}},
          ["reason", "severity", "message", "urgency"]),
    _tool("emergency_alert",
          "Raise the safety alert to every verified family contact for a serious or imminent risk. Use only when "
          "the emergency criteria are met.",
          {"severity": {"type": "string", "enum": list(SEVERITIES)},
           "reason": {"type": "string", "minLength": 1, "maxLength": 1000}},
          ["severity", "reason"]),
    _tool("get_current_time",
          "The time now, read from the server's clock at this call, at home (null) or in the place named, anywhere "
          "in the world (東京, Tokyo, 紐約, London, 'what time is it in Sydney'): the spoken form, the 24-hour time, "
          "the date, the weekday and the time zone, plus the home time and the hour difference away from home. "
          "Status ambiguous lists candidate cities; not_found means the place is unknown. The only source of the "
          "time: the Live model's clock is the session's start.",
          {"place": {"type": ["string", "null"], "maxLength": 80,
                     "description": "The place as the patient said it, or null for here (home)."}},
          ["place"]),
    _tool("search_web",
          "Search the web now and return a short plain-text answer (a few sentences to read aloud) with the sources "
          "(title, url) and the queries searched. Call this directly whenever the patient asks to search, look "
          "something up, check the web, or asks for the latest news or what happened today or recently; do not just "
          "say you will look it up, and do not answer such questions from memory.",
          {"query": {"type": "string", "minLength": 1, "maxLength": 300,
                     "description": "What to find, in the patient's words or a short rephrasing."},
           "max_results": {"type": ["integer", "null"], "minimum": 1, "maximum": 10,
                           "description": "How many sources at most (null: 6)."}},
          ["query", "max_results"]),
    _tool("set_reminder",
          "Schedule a reminder Reachy will say aloud at the right moment, on the server's clock: in_minutes from now "
          "(\"in one minute\", \"in 2 hours\" = 120), or at_time on the 24-hour clock (\"20:00\" for 8 PM) with day "
          "today / tomorrow / YYYY-MM-DD (null: today, or tomorrow when the time has passed). For medicine, give "
          "medication_name as the patient said it or position (1 = their first medicine of that time's slot); the "
          "server names the medicine from the medication database and answers with the exact text it will say, the "
          "due time in words (due_spoken) and the reminder_id. Status medicine_not_found lists the patient's "
          "medicines: ask which one. The reminder fires by itself, whether or not this chat still runs.",
          {"what": {"type": ["string", "null"], "maxLength": 300,
                    "description": "What to remind them of, in their words (null for a medicine reminder)."},
           "medication_name": {"type": ["string", "null"], "maxLength": 120},
           "position": {"type": ["integer", "null"], "minimum": 1, "maximum": 8,
                        "description": "\"my first medicine\" = 1, \"second\" = 2 (null when not said)."},
           "in_minutes": {"type": ["number", "null"], "minimum": 0.5, "maximum": 10080,
                          "description": "Minutes from now (hours times 60); null when a clock time was given."},
           "at_time": {"type": ["string", "null"], "maxLength": 20,
                       "description": "A clock time as HH:MM on the 24-hour clock (8 PM = \"20:00\"); null with in_minutes."},
           "day": {"type": ["string", "null"], "maxLength": 12,
                   "description": "today, tomorrow or YYYY-MM-DD for at_time; null: today (tomorrow once passed)."}},
          ["what", "medication_name", "position", "in_minutes", "at_time", "day"]),
    _tool("cancel_reminder",
          "Cancel a pending reminder by its reminder_id (from set_reminder), or every pending reminder of the patient "
          "with null. Answers the number cancelled.",
          {"reminder_id": {"type": ["string", "null"], "maxLength": 40}}, ["reminder_id"]),
    _tool("get_weather",
          "The current weather and today's forecast for a place in Taiwan, fetched now (geocoded, then Open-Meteo): "
          "location, temperature_c, feels_like_c, condition, humidity, rain_probability, high_c, low_c, "
          "observation_time, tomorrow. Status ambiguous lists candidates to ask about; not_found means no such "
          "place. Never falls back to another place.",
          {"location": {"type": ["string", "null"], "maxLength": 80,
                        "description": "The place as the patient said it, anywhere in Taiwan, in Chinese or "
                                       "English: a city, county, district, township or town (花蓮市, 壽豐鄉, "
                                       "台北信義區, Taichung, Xinyi District). null only when no place was named: "
                                       "the home place from the server's settings."}},
          ["location"]),
    _tool("dance",
          "Make the physical Reachy Mini perform one of its recorded dances (the SDK's dance library on the robot). "
          "Non-blocking: the answer (status started, dance, repeat, available) comes as the move starts. "
          "Refused with stopped_by_patient when the patient asked Reachy to stop moments ago and has not asked to "
          "dance again. Moves: " + "; ".join(f"{name}: {text}" for name, text in DANCES.items()),
          {"dance": {"type": ["string", "null"], "enum": [*DANCES, None],
                     "description": "The move to perform; null lets the robot pick one it did not dance last."},
           "repeat": {"type": ["integer", "null"], "minimum": 1, "maximum": MAX_DANCE_REPEAT,
                      "description": f"How many times in a row (null: {DEFAULT_DANCE_REPEAT}, so there is time "
                                     "to stop it)."}},
          ["dance", "repeat"]),
    _tool("stop_dance", "Stop the dance the robot is performing now. Answers cancelled, or idle when none runs."),
    _tool("play_emotion",
          "Make the physical robot perform one of its recorded emotion moves once (Pollen's emotion library on the "
          "robot), optionally with a sustained posture (concern or joy) under its gestures. Reachy's body language "
          "already follows the patient's words by itself (sadness, worry and good news set the posture and a move as "
          "it answers), so use this only when the patient asks to see an emotion, or for a feeling that path does "
          "not show. Moves: " + "; ".join(f"{name}: {text}" for name, text in EMOTION_MOVES.items()),
          {"emotion": {"type": "string", "enum": list(EMOTION_MOVES)},
           "posture": {"type": ["string", "null"], "enum": [*POSTURES, None],
                       "description": "A posture to hold afterwards: concern, joy, or null to leave it as it is."}},
          ["emotion", "posture"]),
]
TOOL_NAMES = tuple(tool["name"] for tool in TOOLS_SCHEMA)


def live_tool_note(language: str) -> str:
    """The note appended to the live model's instructions when tools are on (LIVE_TOOL_NOTE, by language)."""
    return LIVE_TOOL_NOTE.get(language, LIVE_TOOL_NOTE["en"])


def delegation(backend_instruction: str = BACKEND_INSTRUCTION, backend_model: str = BACKEND_MODEL) -> dict:
    """The `delegation` block of session.start (the reference's shape): a Responses backend with the tools,
    sequential tool calls (medication and safety flows have dependencies)."""
    return {"type": "responses",
            "responses": {"model": backend_model, "instructions": backend_instruction, "tools": TOOLS_SCHEMA,
                          "tool_choice": "auto", "parallel_tool_calls": False}}
