# Pill Intake System Understanding and Redesign — 2026-07-10

## Purpose

This note records the current understanding of MedAiCarePlus's pill-intake system and proposes a system-level redesign for the live failure reported on 2026-07-10:

> A normal single pill-to-mouth movement usually does not register. The user has to repeat the movement quickly several times.

The solution must not be “give more points” or simply lower the confirmation threshold. The goal is to make the detector observe and interpret one natural intake reliably while preserving safety against false positives.

## Sources reviewed

- Current frontend loop: `frontend_source/src/hooks/useIntakeDetection.ts`
- Current intake page and camera lifecycle: `frontend_source/src/pages/Intake.tsx`
- Current backend detector: `app/services/intake_detection.py`
- Event-style classifier: `app/services/intake_detection_style.py`
- Current API route: `app/routers/api_intake.py`
- Existing tests, including `test_intake_detection_mouth_fields.py`
- Project knowledge vault: `C:\Users\user\Documents\MedCareAi`
- Especially `Intake Detection — Live vs Test Harness Discrepancy Log.md`, `ACCURACY_COMPARISON_REPORT.md`, and the June session/research notes

## Current system flow

1. The browser runs MediaPipe face and hand landmarkers on a webcam frame approximately every 100 ms.
2. The browser sends face landmarks, all detected hand landmarks, canvas dimensions, a session ID, and a browser timestamp to `/api/intake/detect`.
3. The backend keeps a `PillIngestionDetector` per user/session and a `HandTrackState` keyed as `hand_0`, `hand_1`, and so on.
4. For each observed hand, it derives mouth distance, hand shape, mouth overlap, mouth opening, head-pose proxy, approach, contact, dwell, and withdrawal features.
5. Entering the mouth zone opens an event window. The detector accumulates evidence while the hand remains near the mouth.
6. The event is scored and classified only on a later frame where that hand is observed outside the mouth zone.
7. The backend returns `confirmed`, `uncertain`, or `none`; the frontend either records the intake, asks for confirmation, or keeps watching.

## Primary root cause of the reported behavior

The live detector requires a clean, continuously observed three-part gesture:

`observed approach → observed mouth contact → observed withdrawal outside the zone`

That assumption is not valid for webcam hand tracking. The hand is most likely to disappear precisely at the important moment: fingers overlap the face, the pill is inserted, or the withdrawing hand is motion-blurred.

In `IntakeDetectionService.process_frame`, `update_hand_state` is called only for hands present in the current MediaPipe result. When `hand_landmarks` is empty, no existing hand state is updated. Consequently:

- a contact event is not closed when the hand disappears;
- elapsed time and missing-observation duration are not handled;
- withdrawal cannot be inferred from the last visible outward motion;
- the state can remain stuck at `was_near_mouth = True`;
- scoring does not happen until a later frame happens to contain `hand_0` outside the mouth zone.

This explains the user's experience unusually well. Repeating the gesture quickly is not intrinsically stronger evidence; it merely increases the chance that one request contains a visible near-mouth sample followed by a visible outside-mouth sample under the same array index.

## Other structural problems that amplify it

### 1. The frontend can overlap inference/API cycles

The loop uses `setInterval(async () => ..., 100)`. `setInterval` does not wait for its asynchronous callback. If landmark inference plus the API request takes more than 100 ms, another cycle starts. Multiple requests may be in flight, responses may arrive out of order, and `stop()` cannot cancel requests already running. This can corrupt the temporal meaning of approach, dwell, withdrawal, UI status, and callbacks.

### 2. Browser timestamps are trusted without ordering protection

The payload timestamp is generated before the request and is then used as detector time. There is no per-session sequence number and no rejection of stale or duplicate frames. Out-of-order requests can therefore move a state machine backward in time or apply old geometry after newer geometry.

### 3. `hand_0` is an array position, not a persistent tracked hand

MediaPipe's result ordering is not a stable identity contract. If two hands appear, disappear, or swap order, accumulated evidence can attach to the wrong hand. Even with one hand, disappearance and reacquisition are not modeled explicitly.

### 4. No-hand and no-face frames are treated as “nothing happened”

These frames are meaningful observations. A short hand loss immediately after strong mouth contact is expected occlusion evidence, while a long loss should abort cleanly. Currently neither transition occurs. A no-face frame also returns early without aging or safely suspending the active event.

### 5. A single-frame boundary controls event closure

`event_window_closed = state.was_near_mouth and not near_mouth` makes the decision depend on one threshold crossing. Landmark jitter can close too early; tracking loss can prevent closure forever. There is no entry/exit hysteresis or short grace period.

### 6. Temporal features are sample-dependent

Approach buffers store per-sample distance differences rather than velocity normalized by actual time delta. Their meaning changes with device speed, network latency, missed frames, and overlapping requests. This makes a natural slower movement less comparable with repeated fast movements.

### 7. Important mouth evidence is most fragile during occlusion

Mouth opening is accumulated only while the hand is already near the mouth. Face landmarks can be least accurate then. A natural intake may show mouth opening just before contact and mouth closure just after contact, but the current event window does not deliberately preserve these pre/post observations as part of the same sequence.

### 8. Feedback describes confidence, not the missing action

The UI mainly exposes a percentage and broad status. It does not tell the user whether the system has acquired the face, acquired the hand, recognized approach/contact, or is waiting for withdrawal. Users therefore repeat or exaggerate the gesture without knowing what was missed.

## Recommended smart solution: an observation-tolerant temporal state machine

Replace the current “score only on one exit frame” control flow with an explicit state machine. Scoring/classification can remain downstream, but it should consume a correctly assembled event rather than determine whether the event exists.

### Proposed states

1. `CALIBRATING`: collect roughly 0.5–1.0 seconds of stable face scale and mouth baseline; ensure face and hand are framed.
2. `READY`: maintain a short rolling buffer of face, mouth, and hand observations.
3. `APPROACHING`: a persistent hand track moves toward the mouth over time, preferably with pinch/loose-grip evidence.
4. `AT_MOUTH`: fingertip enters the inner contact zone or contact probability remains high for a few observations.
5. `OCCLUDED`: the tracked hand is temporarily lost shortly after contact. Preserve the event instead of freezing it.
6. `WITHDRAWING`: the hand is observed moving outward, or is reacquired outside the exit zone after a short occlusion.
7. `COMPLETE_CANDIDATE`: the temporal sequence is complete; classify as confirmed, uncertain, or rejected using all evidence.
8. `COOLDOWN`: prevent duplicate logging until the hand is clearly away and the scene returns to ready.

### Key transition rules

- Use separate entry and exit boundaries. For example, enter contact at a tighter normalized distance and exit only at a wider distance for 2–3 observations. This prevents jitter.
- If the hand disappears during `AT_MOUTH`, enter `OCCLUDED` for a short grace period instead of doing nothing.
- If the same track is reacquired outside the exit boundary within the grace period, treat that as valid withdrawal.
- If it is not reacquired but the last 2–3 visible samples showed outward velocity after strong contact, close as an uncertain candidate rather than losing the event.
- If absence exceeds the grace period with insufficient sequence evidence, reject/reset explicitly.
- Include mouth opening immediately before contact and mouth closure/head motion immediately after it. Do not require the face mesh to be perfect during the hand-over-mouth frame.
- Treat one natural event as one candidate. Repeated approaches during the same unresolved window must not accumulate into an artificial “super gesture.”

## Required data-flow changes

### Frontend capture loop

- Replace `setInterval(async ...)` with a self-scheduling loop: capture the next sample only after the previous MediaPipe inference and API request complete.
- Use `requestVideoFrameCallback` where supported, with a controlled fallback timer.
- Add a monotonically increasing `frame_seq` and include the video's media timestamp.
- Use an `AbortController` and a run-generation token so `stop()` invalidates late responses and prevents post-stop callbacks.
- Measure and expose effective inference FPS, API round-trip time, dropped samples, and in-flight count.
- Avoid copying through a full canvas unless required by the landmarker; process the video element directly where supported.

### Backend ordering and concurrency

- Serialize processing per session/detector, not merely detector creation.
- Store `last_frame_seq` and ignore stale/duplicate frames.
- Clamp or reject non-monotonic timestamps and calculate `dt` explicitly.
- Convert approach features from distance-per-sample to normalized distance-per-second.
- Age every active track on every request, including requests with zero hands or no usable face.

### Stable hand tracking

- Match current hands to existing tracks by wrist/palm position, handedness when available, scale, and predicted motion; do not use array index as identity.
- Keep a track alive through a short number/time of missed observations.
- Record track quality and landmark visibility so weak observations reduce certainty without erasing the sequence.

### Event representation

Store a compact timestamped event buffer containing:

- mouth-relative fingertip and palm positions;
- velocity and direction;
- pinch/loose-grip/flat-palm probabilities;
- mouth open ratio before, during, and after contact;
- hand/face visibility and occlusion flags;
- head motion;
- zone entry/contact/exit times;
- missing/reacquisition duration.

Classify the completed sequence from this buffer. This separates reliable event construction from the later safety decision.

## Decision policy without “more points”

Use semantic evidence and contradictions rather than accumulating rewards until a threshold is crossed.

### Confirm automatically only when

- one coherent track approaches the mouth;
- meaningful fingertip/pill-delivery geometry is observed;
- contact or a contact-adjacent occlusion occurs;
- withdrawal/reacquisition follows in the correct temporal order;
- no strong contradiction indicates face touching, mouth covering, drinking, or unrelated waving.

### Ask for one-tap confirmation when

- approach and contact are strong but withdrawal is hidden by brief tracking loss;
- the complete sequence exists but mouth evidence is occluded;
- a supported palm-dump style is temporally valid but object geometry is ambiguous.

### Reject/reset when

- the hand starts at the face with no approach history;
- there is repeated face touching or long mouth covering;
- motion order is invalid;
- the observation gap is too long;
- multiple repeated gestures occur without a valid single sequence.

This policy improves recall through better observation handling, not through looser confidence values.

## User flow improvements

The camera interaction should guide one ordinary motion:

1. Show `Position your face and hand in the frame` until calibration is stable.
2. Show `Ready — take your pill normally` only when the system can actually observe both.
3. On approach/contact, show `Got it — move your hand away normally` so the user knows the event has been acquired.
4. If tracking is briefly lost, show `Keep your face visible` without resetting immediately.
5. If the sequence is ambiguous, ask `Did you take the pill?` once. Do not make the user reenact it.
6. Always retain manual confirmation as a safe fallback; label it as confirmation, not failure.

The percentage meter should be secondary or removed. A stage indicator is more actionable and less likely to train users into fast repeated movements.

## Implementation order

### P0 — Fix the live event pipeline

1. Make the frontend loop single-flight and cancel-safe.
2. Add `frame_seq`; serialize and order frames per session.
3. Advance active states on zero-hand frames.
4. Add contact-loss grace and reacquisition-based withdrawal.
5. Add zone hysteresis and reset/timeout rules.

These changes directly target the reported “repeat quickly several times” symptom.

### P1 — Make event construction robust

1. Replace index-based hand IDs with persistent matching.
2. Use `dt`-normalized velocity and timestamped buffers.
3. Add pre-contact and post-contact mouth context.
4. Separate event assembly from semantic classification.
5. Return the explicit state and missing required observation to the UI.

### P2 — Improve classification and validation

1. Gather representative live sessions from the actual target cameras/devices, with consent and privacy controls.
2. Label temporal boundaries: approach, contact, occlusion, withdrawal, and non-intake action.
3. Build a replay harness that sends landmarks with original timestamps, dropped frames, jitter, request delay, and reordered-response simulations.
4. Evaluate event-level recall/precision and time-to-decision, not only best frame confidence.
5. Tune geometry or train a lightweight temporal classifier only after the state/transport defects are fixed.

## Tests that should be added before rollout

- One normal intake with 200–500 ms hand loss at contact still produces one candidate.
- Hand loss without prior approach/contact produces no candidate.
- Reacquisition outside the exit zone closes the original event exactly once.
- Repeated fast gestures do not combine evidence across separate candidates.
- Delayed/out-of-order/duplicate frames do not mutate state.
- A stopped frontend run ignores all late responses.
- Hand result order swapping does not swap track histories.
- Slow natural intake and fast natural intake both work because velocity uses real `dt`.
- Mouth opening just before occlusion is attached to the event.
- Face scratch, chin rest, talking with a hand nearby, mouth covering, drinking, and waving remain negative or uncertain as appropriate.
- Two simultaneous hands do not mix evidence.
- Event timeouts reset to `READY` without leaving stale `was_near_mouth` state.

## Telemetry needed for diagnosis

For development and opt-in test sessions, log structured event traces rather than only final confidence:

- session/run ID and `frame_seq`;
- state transition and transition reason;
- effective sample FPS and request latency;
- face/hand visibility;
- persistent track ID and association quality;
- normalized contact distance and velocity;
- zone/contact/occlusion/reacquisition flags;
- event start/end timestamps;
- final decision and semantic contradiction;
- whether the user manually confirmed or rejected an uncertain event.

Do not log raw video by default. Landmark/event traces are usually sufficient and are safer for a healthcare application.

## Definition of success

- A user can take one pill with one normal, unexaggerated movement.
- The system responds after the first approach-contact-withdrawal sequence, even with a short hand-tracking gap.
- It never requires repeated fast motions to build evidence.
- Exactly one intake is recorded per completed user action.
- Ambiguous cases prompt once rather than silently recording or forcing reenactment.
- Performance is reported by device/browser group using event-level recall, precision, uncertain rate, false auto-confirm rate, and median decision latency.

## Immediate recommendation

Do not begin by changing `CONFIRM_THRESHOLD`, contribution values, or penalties. First implement the P0 pipeline fixes and replay the same landmark trace under normal, dropped-frame, delayed, and out-of-order conditions. The present symptom is primarily a broken observation/state-transition problem; score tuning would only hide it and could create unsafe false confirmations.

## Implementation record — 2026-07-10

### Changes made

- `frontend_source/src/hooks/useIntakeDetection.ts`: replaced the overlapping async `setInterval` with a recursive, single-flight `setTimeout` loop. Each run has its own generation-qualified session ID, `AbortController`, and monotonically increasing `frame_seq`. Stop/unmount aborts the request, invalidates the generation, ends the backend run, and ignores late responses.
- `frontend_source/src/lib/ai-api.ts`: added the sequence/session response contract, explicit stage and missing-observation fields, candidate ID, and abort-signal support.
- `app/routers/api_intake.py`: made `frame_seq` required at the API boundary.
- `app/services/intake_temporal.py`: added the temporal event control plane and persistent spatial hand association. It uses timestamp-normalized velocity, separate contact/exit distances, an occlusion grace period, event timeout, cooldown, pre/contact/post mouth context, and outside-zone reacquisition as withdrawal.
- `app/services/intake_detection.py`: rejects duplicate/stale/out-of-order sequence numbers before detector mutation; advances no-face/no-hand observations through the temporal pipeline; converts existing geometry features into temporal observations; cleans temporal and sequence state on session end/expiry. The legacy scorer remains available for its existing offline tests, but no longer decides whether a live event exists.
- `frontend_source/src/pages/Intake.tsx`: maps detector stages to actionable guidance: calibrating, ready, approaching, contact, withdrawal, captured, and reset.
- `test_intake_temporal_pipeline.py`: added deterministic replay and service integration coverage.

### Architectural decisions

Event assembly is now distinct from semantic acceptance. A complete approach/contact/withdrawal produces one `COMPLETE_CANDIDATE`; pill-like grip plus mouth evidence can auto-confirm, while cover/drink-like or otherwise ambiguous completed sequences return `uncertain` for the existing manual confirmation UI. Missing observations change temporal state but do not add positive evidence. Cooldown prevents a second gesture from strengthening or duplicating the completed event.

Persistent identity uses nearest-neighbor palm-center association with a bounded match radius and loss grace. This is intentionally lightweight because MediaPipe currently supplies landmarks but no stable tracking identifier. The active track remains attached across hand array reordering and brief disappearance.

### Verification performed

On 2026-07-10:

- `python -m unittest test_intake_temporal_pipeline.py test_intake_detection_mouth_fields.py -v`: **22/22 passed**.
- Temporal replay passed one normal-speed, one slow, and one fast natural intake, each producing exactly one confirmed candidate.
- Replay passed 200, 350, and 500 ms contact losses, including reacquisition beyond the exit boundary.
- Permanent hand loss reset without a candidate.
- Delayed, duplicate, and out-of-order sequence requests were rejected without state mutation.
- Ending a run cleared sequence state; frontend generations and abort checks cover stopped runs receiving late responses.
- Repeated fast gestures produced no duplicate candidate during cooldown.
- Swapped hand ordering and two simultaneously visible hands preserved the active identity.
- Synthetic scratch, chin-near, talking-near, and waving traces did not confirm. Mouth-cover and drinking-like completed traces were routed to manual confirmation, not auto-recording.
- Existing mouth-feature/style scorer regression tests remained green (12/12).
- `npm.cmd run build --prefix frontend_source`: TypeScript and Vite production build passed (1,790 modules transformed).
- `git diff --check`: no whitespace errors (only the repository's Windows line-ending notices).

### Evidence of improvement

The former live path required a later visible outside-mouth frame under the same `hand_0` array index and could leave contact open forever during loss. The replay now completes the first natural sequence after visible withdrawal or outside-zone reacquisition, including a 500 ms loss, and returns only one candidate. Reordered transport frames are rejected before they can alter the detector. This directly removes the mechanisms that made repeated fast movements appear necessary.

### Limitations and remaining risks

- These are landmark-level deterministic replays, not a newly collected labeled camera corpus. Real-device event recall, false auto-confirm rate, and latency still require consented validation across target cameras, lighting, skin tones, hand sizes, pill sizes, and mobility patterns before clinical reliance.
- Nearest-neighbor tracking can still become ambiguous when two hands cross at nearly the same location. A future MediaPipe handedness/embedding signal should be incorporated when available.
- Current semantic acceptance is conservative and geometry-based. Drinking/covering is intentionally manual fallback, but broader negative video validation is still needed.
- The npm install reports four dependency advisories (three low, one high); the production build passes, but dependency remediation is outside this detector change.
- Raw video is not stored by this implementation. Structured, privacy-preserving landmark transition telemetry is recommended for field validation.
