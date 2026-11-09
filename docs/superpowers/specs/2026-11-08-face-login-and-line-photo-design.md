# Face login that picks the right person, and medicine photos over LINE

Date: 8 Nov 2026. Branch: `version-4-competition` (made from `version-4-jingsi` @ `983a6ba`).

## Why

- **Face login can log in the wrong person.** Today one webcam frame is matched, and when several faces are in the
  picture, the one with the smallest distance to any registered person logs in. A test on 8 Nov (a selfie of two
  people; one registered from the same photo in a scratch gallery) logged in the person further from the camera.
  Nothing stops someone holding up a photo of the patient on a phone.
- **Adding medicines needs the app.** The patient already uses LINE every day. Sending a photo of the medicine bag to
  the LINE account is easier than opening the scan page.

## Decisions the user made (Telegram, 7 Nov)

1. Only the biggest face in the picture can log in. Everyone else in the picture is ignored, never matched.
2. When a second face is nearly as big, nobody logs in; the screen asks one person to come closer.
3. Face login uses 3 to 5 frames, not one.
4. Face login refuses a face that is a photo on a phone or paper (passive liveness, no blink or head turn).
5. The LINE medicine photo is sent by the patient, not family.
6. After reading the photo, LINE asks the patient first. The patient chooses: save everything, or open the app to change
   it. Nothing is saved without the patient's press.
7. The LINE medicine photo is for every patient.
8. Bring the LINE medication-reminder switch (branch `line-reminder-switch`, `b4c4a97`) into this branch. The switch
   card is for the admin only (Ferdinan's account in `ADMIN_USER_IDS`). The paused banner stays for everyone.
9. (Telegram, 8 Nov) "Check in the app" is not a link for now: the app is not reachable from outside the home (see
   Part 2). A real link comes with the domain work.
10. (8 Nov) The photo check starts in measure-and-log mode: it scores and logs every login but blocks nothing, until
    the threshold is set from real logins.
11. (8 Nov) The current consent is enough for testing and the competition. Before real patients use the LINE photo,
    the notice must name medicine photos and the OCR service (a release gate, below).
12. (8 Nov) Order: face login first, then the LINE photo.
13. (8 Nov) Registration keeps only face vectors, not photos, and login compares with the stored vectors. Built after
    the LINE photo (Part 3).

## Goals

- The person nearest the camera is the only one who can log in by face.
- A photo of a registered face on a phone or on paper does not log in, in a casual attempt.
- A patient can add medicines by sending a photo in LINE, confirm in LINE, or fix them in the app.

## Non-goals

- Stopping a skilled attacker (3D mask, replayed video injected into the browser).
- A new face recognition model. `face-reidentification-retail-0095` stays (LFW 0.9947; the newer free InsightFace and
  EdgeFace weights are non-commercial only).
- Family members adding medicines over LINE.
- Changing the monitor's multi-face camera view (`identify_faces`, `monitor_service.py:664`); it is not a login.

## Anti-goals (a failure even if it technically works)

- A real patient in normal room light is refused so often that they give up on face login.
- A medicine is saved from a LINE photo without the patient pressing a button.
- A LINE photo from someone who is not the linked patient adds medicines to that patient.
- Any of this goes out to a LINE group (the patient's name or medicines shown to others).

## Part 0: bring in the LINE switch

- `line-reminder-switch` is `983a6ba` plus 11 commits, so it merges into `version-4-competition` as a fast-forward.
- Everything in it stays as built (CLAUDE.md "LINE reminder switch (Nov 2026)", HANDOFF §14), including the free LINE
  reply (`LineService.reply_messages`, `outbox.enqueue(reply_token, received_at)`, reply only when
  `source.type == "user"`, `api_notify._usable_reply_token`).
- At deploy, `ADMIN_USER_IDS` is set to Ferdinan's `u_id` in `.env` (the user confirms the number at the real check).
- The new LINE messages below are `kind` other than `med_reminder`, so the switch never pauses them.

## Part 1: face login

### Where it applies

Every route that turns a face into a session uses the same rule. Today they all call
`FaceRecognitionService.identify_frame` (`app/services/face_recognition_service.py:167`):

- `POST /api/face/login` (`app/routers/api_face.py:51`), used by the React login page
  (`frontend_source/src/pages/Login.tsx`, `aiApi.faceLogin`).
- `POST /api/auth/face-login` (`app/routers/api_auth.py`), no caller found in the repos.
- `POST /auth/face-frame` (`app/routers/auth.py:54`), the older HTML login (`static/js/face_login.js`).
- `POST /api/face/identify` (`api_face.py:33`): public, returns a name but no token.

### The rule for one frame

1. Detect every face (`face-detection-adas-0001`, confidence 0.6, as today).
2. The **main face** is the one with the largest box area.
3. **Too small:** if the main face's box covers less than `FACE_MIN_AREA_RATIO` of the frame (start 0.05), the frame
   answers `come_closer`.
4. **Two people:** if the second-largest face's area is at least `FACE_SECOND_FACE_RATIO` of the main face's area
   (start 0.70), the frame answers `two_faces`. In the 8 Nov selfie the ratio was 0.75, so it would answer `two_faces`.
5. **Liveness:** the main face crop goes through a passive anti-spoofing model. Below `FACE_LIVENESS_THRESHOLD` the
   frame answers `not_live`.
6. **Match:** only the main face's descriptor is matched against the gallery (cosine distance halved, threshold
   `FACE_MATCH_THRESHOLD` 0.3, as today). Over the threshold the frame answers `unknown`. Other faces are never matched.

Measured on 8 Nov (laptop CPU, same models): detection about 10 ms, landmarks 1 ms, descriptor 7 ms, total 19 ms per
1280x960 frame. Ferdinan's own photo matched at 0.15; an unregistered face was 0.41.

### Several frames per login

- The login page takes 5 frames about 200 ms apart and sends them in one request.
- The server judges each frame with the rule above. Login succeeds only when **at least 3 frames name the same person,
  every one of those passed liveness, and no frame names a different registered person**.
- Claude's call: a single `two_faces` frame fails the whole login (decision 2: nobody logs in), and so does a single
  `not_live` frame once the photo check blocks (decision 4: every frame must pass). `come_closer` and `unknown` frames
  may sit beside 3 matching frames.
- When login fails, the reason shown is the most common frame answer, in this order of priority: `not_live`,
  `two_faces`, `come_closer`, `unknown`.
- The older routes that send a single frame: single-frame sessions are no longer issued. Claude's call: the HTML page
  (`static/js/face_login.js`) and `/auth/face-frame` are changed to send 5 frames the same way; `/api/auth/face-login`
  takes several files like `/api/face/login`. `/api/face/identify` uses the same rule and also needs 5 frames.

### Liveness model

- Passive single-image anti-spoofing, MiniFASNet (Silent-Face-Anti-Spoofing, Apache-2.0), run as ONNX on
  `onnxruntime` (already `requirements.txt:13`, 1.19.2) on CPU.
- The builder picks the exact ONNX export, checks its licence file, records its source and checksum, and adds it under
  `models/` the way the other models are.
- **Measure and log first (decision 10).** `FACE_LIVENESS_MODE` is `log` by default: every frame's liveness score is
  computed and logged (one INFO line per login: the scores, the result, no image and no name beyond the account id),
  and nothing is refused for it. `block` turns the refusal on. Login works the same in `log` mode as without the check.
- `FACE_LIVENESS_THRESHOLD` starts from the model's published default. Before `block` is switched on, it is set from
  the logged real logins plus a test on this laptop's webcam (Claude's call: the user's own face, Ferdinan, in normal
  and dim light, then a phone screen and a printout of a gallery photo; the patient's own device and room later). The
  measured scores go in the HANDOFF.
- In `block` mode, if the model file is missing, face login is refused with a clear server log, never silently
  allowed. In `log` mode a missing model logs one WARNING at startup and login goes on.

### What the login page says

- `come_closer`: "Please come closer to the camera."
- `two_faces`: "Two people are close to the camera. Please let one person come closer."
- `not_live`: "Please look at the camera yourself."
- `unknown`: "Face not recognised. Try email login below." (today's text).
- Each in Traditional Chinese and English through the existing i18n (`frontend_source/src/i18n.ts`).

### Registration

Enrollment (`POST /api/face/enroll`, `save_enrollment`) already stores one detected face per photo. Claude's call: the
same biggest-face and two-faces rule applies there, so a second person in a registration photo is never stored. No
liveness on registration in this job.

## Part 2: medicine photos over LINE

### The flow

1. The patient sends a photo to the LINE account in a one-to-one chat.
2. The webhook (`app/routers/api_notify.py` `line_webhook`) handles `message` events of type `image`. Group and room
   chats are ignored (no reply).
3. **Who is it.** The sender's `source.userId` must equal an active patient's `"user".line_id`.
   - Not linked to anyone: reply "Please link your LINE in the app first."
   - Linked only as a family contact (`family_contacts.line_id`): reply "Only the patient can add medicines by photo."
   - The patient's consent must be current, the same check as `get_consented_user` (the photo goes to Gemini, a cloud
     service). If not current: reply "Please open the app and accept the terms first." Nothing is downloaded.
4. Start LINE's loading animation (`POST /v2/bot/chat/loading/start`, `loadingSeconds` 60, one-to-one only), then
   download the photo (`GET /v2/bot/message/{messageId}/content`; the builder confirms the host, `api-data.line.me`,
   against the LINE reference). Refuse files over 10 MB.
5. Read it with the existing `OCRService.process_image` (Gemini, Ollama fallback), off the event loop
   (`asyncio.to_thread`), within `OCR_GEMINI_IMAGE_BUDGET` (45 s, `app/config.py:50`). The webhook itself answers LINE at
   once; the reading runs as a background task.
6. Answer with the free reply token if it is still inside `REPLY_WINDOW_SECONDS` (50 s, from Part 0), else by push.
   - Could not read / no medicine found: "I couldn't read this photo. Please take it again in good light."
   - Read: a message listing each medicine in plain words (name, amount per dose, times per day or slots, days), with a
     note on any that are already saved, one **Save all** button (a postback: saves, then answers "Saved N
     medicines."), and the line "To change them, open the Scan page in the app." (no link, decision 9).
7. Nothing is saved until the patient presses Save all or saves in the app.
8. Claude's call: each photo is its own draft and its own reply, also when LINE delivers several photos in one batch.

### The draft

- A new table holds each read photo as a draft: a random id (unguessable, used in the postback data and the app link),
  the patient's `u_id`, the OCR result JSON, created time, a 24-hour expiry, and a status (pending, saved, expired).
- The photo itself is not kept after reading. Claude's call: less patient data stored; the cards in the app show no
  photo preview for a LINE draft.
- The account export (`app/routers/api_account.py`) and account deletion include the drafts.

### Save all

- The postback's sender must be the draft owner's LINE id, and the draft must be pending and not expired. Otherwise:
  "This list has expired. Please send the photo again." A second press answers "Already saved."
- Saving follows exactly what the scan page would save with no changes: the same course start, last day, slots and
  dose limits as `frontend_source/src/lib/scan.ts` (`draftFrom`, `courseStart`, `lastDayFor`, `slotsForCount`,
  `doseLimits`, `medicationPayload`), and the same server checks as `POST /api/medications`.
  - That logic lives in the frontend today. The builder ports it to the server once and makes the scan page and Save all
    share one definition, or proves with tests that both give the same rows for the same OCR result.
- A medicine the scan page would flag (`markDuplicates`: already saved; `draftProblem`: missing or impossible values)
  is not saved by Save all. The answer says which ones were skipped and offers Check in the app.

### Changing them in the app

- Why no link (decision 9): the public tunnel lets through only LINE's webhook and the dose-video links
  (`scripts/line-webhook/nginx.conf`: every other path is 404), and its address changes at each restart
  (`PUBLIC_BASE_URL`). Opening the scan page, the medication API and face login to the internet is the domain work's
  job (out of scope in `2026-11-02-voice-id-and-line-admin-design.md`).
- The Scan page shows the patient's pending, unexpired LINE drafts at the top ("From LINE: 2 medicines, 14:05"). Opening
  one fills the usual cards from its OCR result, so editing, unticking, the duplicate warning and Save work as they do
  today. Saving marks the draft saved. Only the owner's drafts are listed (`get_consented_user`).

### Limits

- Claude's call: at most 10 photos per patient per hour (Gemini cost). Over that: "Too many photos right now, please
  try again later."
- All these LINE messages are `kind` other than `med_reminder`.

## Part 3: face vectors instead of face photos (decision 13)

### Today

- Registration (`api_face.save_enrollment`, `api_face.py:156`; the older `auth.py` `/register-photos`) saves cropped
  face photos as `<label>-<n>.jpg` in `models/face_recognition/face_gallery` (bind-mounted, `docker-compose.yml:45`).
- At every start and after each registration, `FacesDatabase` (`models/face_recognition/faces_database.py`) computes a
  256-number descriptor per photo (`face-reidentification-retail-0095`); login matches against those.
- The nightly backup tars the folder (`scripts/backup/backup.sh:18`, mounted read-only at `docker-compose.yml:108`).
  Account deletion and the restore ledger remove a label's photos (`app/services/deletion_ledger.py:36-63`).
  `enrollment-status` counts the photos (`api_face.py:316`).

### The change

- Registration computes the descriptor from the detected main face (Part 1's biggest-face rule) the same way login
  does, stores it in the database (a new table: the user, the face label, the vector, created time; one row per
  registration photo), and keeps no photo on disk.
- The recognition service builds its gallery from those rows (at start, and after a registration or a deletion),
  not from the folder. Matching is unchanged (cosine distance halved, MIN_DIST, threshold 0.3), so accuracy is the
  same.
- Account deletion and the restore ledger delete the rows; the account export includes that vectors exist (count and
  dates), Claude's call: not the numbers themselves. `enrollment-status` counts rows. The backup no longer needs the
  gallery folder once it is empty (the database dump carries the vectors).
- **Existing photos:** a one-time step turns each photo in the folder into a row. The folder stays until the user has
  tested login with the vectors; it is deleted only with the user's OK.
- A future model change means everyone registers again (old vectors cannot be redone without photos). The vectors are
  still biometric data: same protection as the photos had (never logged, never sent to the browser).
- The notice's line about face-login photos (`app/legal/core/2026-10/`, "photos you take for face login: stored on the
  home server") then describes vectors: part of the release gate below.

## Release gate (decision 11)

The core notice (`app/legal/core/2026-10/`) never names medicine photos or the OCR service (Google Gemini), and lists
LINE only for reminders and alerts. The Scan page already has this gap. Before any real patient sends medicine photos
over LINE, a new notice version must cover them (that bumps `TERMS_VERSION`, which re-prompts everyone, and needs the
memory and video notices for the new version too). The same new version should say that face login keeps vectors, not
photos (Part 3). The user writes the text. Until then the feature is for testing and the competition. CLAUDE.md
records the gate.

## Order (decisions 12 and 13)

Part 0, then Part 1 (face login), then Part 2 (the LINE photo), then Part 3 (face vectors). Each part is reviewed and
testable on its own.

## Tests the builder must add

- Face, per frame: largest box wins; a bigger unregistered face in front blocks a registered one behind (`unknown`);
  `two_faces` at ratio >= 0.70 and not at 0.69; `come_closer` under 0.05; a spoof score under the threshold gives
  `not_live`.
- Face, several frames: 3 of 5 same person logs in; 2 of 5 does not; one `two_faces` frame fails; in `block` mode one
  `not_live` frame fails, in `log` mode it does not and the score is logged; a frame naming another registered person
  fails; every face route uses the rule.
- LINE photo: unlinked sender, family sender, consent not current, group chat, file too big, OCR failure, a good read
  with the reply and with a push; Save all by the owner, by someone else, twice, after 24 hours; skipped duplicates and
  problem medicines; the same rows as the scan page for the same OCR result; rate limit.
- Face vectors: registration writes rows and no file; login matches a stored vector; deletion and the ledger replay
  remove the rows; the one-time conversion makes one row per photo and leaves the folder; a vector never appears in a
  log line, an API answer or the export.
- The consent enforcement test (`tests/test_consent_enforcement.py`) still passes with the new routes.
- Frontend: the new login messages; the Scan page listing and opening a LINE draft; tsc and build clean.

## Left to the builder

- The message layout in LINE (template or Flex), within the content above.
- Table and column names, the postback data format, config names other than those given.
- How the login page shows progress while it takes the 5 frames.

## Sources

- Code: the files and lines named above, on `983a6ba` (and `b4c4a97` for Part 0).
- Face test of 8 Nov: scratch script `measure.py` in the session's scratchpad, same models and thresholds as the server.
- Research: OpenVINO model docs for `face-reidentification-retail-0095`; InsightFace model zoo licence; EdgeFace model
  card; Silent-Face-Anti-Spoofing / MiniFASNet ONNX reimplementation (CelebA-Spoof 98.2%, own test only); LINE
  "Display a loading animation" (developers.line.biz/en/docs/messaging-api/use-loading-indicator/) and "Get content"
  (developers.line.biz/en/reference/messaging-api/).
