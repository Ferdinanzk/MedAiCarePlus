# Face Login That Picks the Right Person: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Face login matches only the biggest face, refuses when two faces are close in size or the face is too small,
judges 5 frames per login, and scores every face with a passive liveness check (log-only at first).

**Architecture:** A pure decision module (`app/services/face_login.py`) picks the main face of a frame and decides a
login from several frame verdicts. `FaceRecognitionService` produces one verdict per frame (detector, liveness, match
of the main face only) and replaces `identify_frame`. Every face login route takes up to 5 frames and returns the
decision with a reason code that both login pages turn into words.

**Tech Stack:** FastAPI, OpenVINO 2024.5 (existing face models), onnxruntime 1.19.2 (liveness), React + i18next,
the older Jinja2 login page.

**Spec:** `docs/superpowers/specs/2026-11-08-face-login-and-line-photo-design.md` (Parts 0 and 1). Parts 2 (LINE
photo) and 3 (face vectors) get their own plans after this one.

## Global Constraints

- Branch `version-4-competition`. Commit locally only; never push without the user's word. New commits, no amend, no
  trailers.
- Detector `face-detection-adas-0001`, confidence `FACE_DET_CONFIDENCE` 0.6; recognizer
  `face-reidentification-retail-0095`, cosine distance halved, `FACE_MATCH_THRESHOLD` 0.3, MIN_DIST. Unchanged.
- `FACE_MIN_AREA_RATIO = 0.05` (main face box area / frame area), `FACE_SECOND_FACE_RATIO = 0.70` (second-largest area
  / main area, `>=` means two faces), `FACE_LOGIN_FRAMES = 5`, `FACE_LOGIN_MIN_MATCHES = 3`,
  `FACE_LIVENESS_MODE = "log"` (`log` | `block`), `FACE_LIVENESS_THRESHOLD` from the chosen model's published default.
  All read from the environment in `app/config.py` with these defaults.
- Reason codes, exactly: `match`, `unknown`, `no_face`, `come_closer`, `two_faces`, `not_live`.
- Login page copy, exactly:
  - `come_closer` and `no_face`: zh-TW 「請靠近鏡頭一點。」 / en "Please come closer to the camera."
  - `two_faces`: 「有兩個人靠近鏡頭，請讓一個人靠近一點。」 / "Two people are close to the camera. Please let one person come
    closer."
  - `not_live`: 「請本人看著鏡頭。」 / "Please look at the camera yourself."
  - `unknown`: 「無法辨識臉部，請改用下方的電子郵件登入。」 / "Face not recognised. Try email login below."
- Logs never contain images, face vectors, tokens or names; one INFO line per login with the account id or `-`.
- Monitor face matching (`identify_faces`, `monitor_service.py:664`) and the check-in rule are not changed.
- **Backend gate (GATE):** Docker Desktop must be running (`docker info`). Then
  ```bash
  MSYS_NO_PATHCONV=1 docker run --rm -v "C:/Reachy_Mini/repo/MedAiCarePlus:/src" -w /src --entrypoint sh medcareai2-app \
    -c "pip install -q pytest httpx; python -m pytest tests -q -p no:cacheprovider"
  ```
  Shorthand: **GATE** `<path or -k>`. Baseline at `b4c4a97`: 3615 passed, 1 skipped.
- **Frontend gate:** `cd frontend_source && npx tsc -b --noEmit && npm run build`, and Playwright with a throwaway
  config outside the repo (testDir `./tests`, `vite preview` on port 4173), because `playwright.config.ts`'s `testDir`
  does not exist.
- semgrep is not installed: before committing auth changes, say plainly that it was skipped.

## Review Focus

1. **A face cut off at the frame's edge** (the detector clips its box). It is still the main face by its clipped area,
   and the crop for liveness and matching never fails. Test in Task 3: `test_edge_face_is_still_judged`.
2. **One of the 5 uploads is not an image.** That frame counts as `no_face`; the login is decided from the rest. All 5
   bad gives 400. Test in Task 4: `test_one_bad_frame_does_not_spoil_the_login`.
3. **Nobody registered yet** (empty gallery). Every frame is `unknown`, no exception. Test in Task 3:
   `test_empty_gallery_gives_unknown`.
4. **The liveness model fails on one crop** (tiny or odd box). That frame's score is `None`: logged as an error; in
   `block` mode the frame is `not_live`, in `log` mode it is judged as if live. Test in Task 3:
   `test_liveness_error_by_mode`.
5. **Two registered people take turns in front of the camera during one login** (3 frames A, 2 frames B). Nobody logs
   in. Test in Task 1: `test_another_registered_person_fails_the_login`.

---

### Task 0: Bring in the LINE switch

**Files:** none edited.

- [ ] **Step 1:** `git switch version-4-competition && git merge --ff-only line-reminder-switch`. Expected: fast-forward
  to `b4c4a97`.
- [ ] **Step 2:** GATE (all). Expected: 3615 passed, 1 skipped.
- [ ] Nothing to commit. `ADMIN_USER_IDS` is set at deploy time with the user (HANDOFF §14), not here.

### Task 1: The decision rules

**Files:**
- Create: `app/services/face_login.py`
- Modify: `app/config.py` (the six settings in Global Constraints, next to `FACE_MATCH_THRESHOLD` at :60)
- Test: `tests/test_face_login_rules.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class FaceBox: x: float; y: float; w: float; h: float; confidence: float` with
    `area -> float` (`max(w,0)*max(h,0)`).
  - `main_face(boxes: list[FaceBox], frame_w: int, frame_h: int, *, min_area_ratio: float, second_ratio: float)
    -> tuple[str, FaceBox | None]`: `("no_face", None)` for no box; else the largest-area box with `"two_faces"` when
    the second-largest area `>= second_ratio *` the largest, else `"come_closer"` when largest area
    `< min_area_ratio * frame_w * frame_h`, else `"ok"`. (two_faces is checked before come_closer.)
  - `@dataclass(frozen=True) class FrameVerdict: code: str; label: str | None = None; distance: float | None = None;
    liveness: float | None = None; face_count: int = 0`. `code` is a reason code; `label`/`distance` only for
    `match`.
  - `@dataclass(frozen=True) class LoginDecision: identified: bool; label: str | None; distance: float | None;
    reason: str; frames: int` (`reason` is `match` when identified).
  - `decide(verdicts: list[FrameVerdict], *, min_matches: int, liveness_mode: str, liveness_threshold: float)
    -> LoginDecision`.

`decide`, in this order:
1. Any `two_faces` frame: not identified, reason `two_faces`.
2. In `block` mode, any frame whose `liveness` is `None` or below the threshold, among frames that found a main face:
   reason `not_live`. (In `log` mode liveness never decides.)
3. Matches with more than one distinct label: reason `unknown`.
4. At least `min_matches` frames `match` with the same label: identified, that label, the smallest distance.
5. Else the most common code among the non-match frames, ties broken `come_closer` > `no_face` > `unknown`.

- [ ] **Step 1: Write the failing tests**

```python
def test_largest_face_wins(): ...           # boxes 100x100 and 60x60 in 640x480 -> ("ok", the 100x100 box)
def test_two_faces_at_070_not_at_069(): ... # second area exactly 0.70 * main -> "two_faces"; 0.69 -> "ok"
def test_selfie_of_8_nov_is_two_faces(): ...# 331x450 and 297x376 in 1280x960 -> "two_faces" (ratio 0.749)
def test_small_face_is_come_closer(): ...   # 100x100 in 1280x960 (0.008) -> "come_closer"; 0.05 exactly -> "ok"
def test_no_box_is_no_face(): ...
def test_three_of_five_logs_in(): ...       # 3 match "ferdi" + unknown + no_face -> identified, reason "match"
def test_two_of_five_does_not(): ...        # 2 match + 3 unknown -> reason "unknown"
def test_one_two_faces_frame_fails(): ...   # 4 match + 1 two_faces -> reason "two_faces"
def test_not_live_blocks_only_in_block_mode(): ...  # 5 match, one liveness 0.1, threshold 0.5:
                                                    # block -> "not_live"; log -> identified
def test_another_registered_person_fails_the_login(): ...  # 3 "ferdi" + 2 "aisha" -> reason "unknown"
def test_reason_ties(): ...                 # 1 match, 2 come_closer, 2 unknown -> "come_closer"
def test_distance_is_the_smallest_match(): ...
```

- [ ] **Step 2:** GATE `tests/test_face_login_rules.py`. Expected: FAIL (module missing).
- [ ] **Step 3:** Implement the module and the config settings.
- [ ] **Step 4:** GATE `tests/test_face_login_rules.py`. Expected: PASS.
- [ ] **Step 5:** Commit `Face login: the biggest-face and several-frame rules`.

### Task 2: The liveness model

**Files:**
- Create: `app/services/liveness_service.py`, `models/liveness/` (the ONNX file, `LICENSE` copy, `SOURCE.md` with the
  download url, the commit or release, and the SHA-256)
- Modify: `.gitignore` (an exception for the liveness ONNX, like `models/landmarks/`), `app/config.py`
  (`FACE_LIVENESS_MODEL` path, `FACE_LIVENESS_THRESHOLD` default), `app/main.py` lifespan warm-up and `/health`
  (`"liveness": true|false`), `app/startup_report.py` (one `[STARTUP] liveness:` line with the mode)
- Test: `tests/test_liveness.py`

**Interfaces:**
- Consumes: `FaceBox` (Task 1).
- Produces: `LivenessService.get_instance() -> LivenessService`; `.available: bool`;
  `.score(frame_bgr: np.ndarray, box: FaceBox) -> float | None` (probability the face is real, 0..1; `None` when the
  model is missing or inference fails, with one WARNING per failure kind, no image data).

- [ ] **Step 1:** Choose a MiniFASNet ONNX export of Silent-Face-Anti-Spoofing (Apache-2.0). Confirm its licence file
  allows use and redistribution; record input size, crop scale around the box, colour order, normalisation, and which
  output index is "real" in `SOURCE.md`. The published default threshold goes into `FACE_LIVENESS_THRESHOLD`.
- [ ] **Step 2: Write the failing tests**

```python
def test_missing_model_is_unavailable_and_scores_none(tmp_path, monkeypatch): ...
def test_sha256_matches_source_md(): ...          # skip only if the file is absent
def test_score_is_a_probability_on_a_synthetic_face(): ...  # a 640x480 frame, a box in the middle: 0 <= s <= 1
def test_tiny_box_at_the_edge_gives_a_number_or_none_never_raises(): ...
```

- [ ] **Step 3:** GATE `tests/test_liveness.py`. Expected: FAIL.
- [ ] **Step 4:** Implement: lazy `onnxruntime.InferenceSession` on CPU, one per process, crop and preprocess as
  `SOURCE.md` says, softmax, return the real-class probability. Clip the crop to the frame.
- [ ] **Step 5:** GATE `tests/test_liveness.py`. Expected: PASS.
- [ ] **Step 6:** Commit `Liveness: MiniFASNet on onnxruntime, scored per face`.

### Task 3: One verdict per frame in the recognition service

**Files:**
- Modify: `app/services/face_recognition_service.py` (replace `identify_frame` at :167; `match_enrollment_face` is
  Task 5)
- Test: `tests/test_face_login_service.py`

**Interfaces:**
- Consumes: `main_face`, `FrameVerdict`, `decide`, `LoginDecision` (Task 1); `LivenessService.score` (Task 2).
- Produces:
  - `FaceRecognitionService.detect(frame_bgr) -> list[FaceBox]` and
    `.match_box(frame_bgr, box: FaceBox) -> tuple[str | None, float | None]` (label within the threshold or `None`,
    distance or `None` for an empty gallery). Both under the service lock.
  - `.judge_frame(frame_bgr) -> FrameVerdict`: `main_face` with the config ratios; for `ok`, liveness score then
    `match_box` on the main face only (`match` or `unknown`); for `come_closer`/`two_faces` the liveness score is still
    computed for the log. No other face is ever matched.
  - `.login(frames: list[np.ndarray]) -> LoginDecision`: `judge_frame` each, `decide` with the config, then one INFO
    log line `face login: reason=<r> frames=<n> matches=<m> liveness=[0.93, 0.91, ...] mode=<log|block>` (label not
    logged; the route logs the account id).
  - `identify_frame` is removed (every caller moves to `login` in Task 4).

- [ ] **Step 1: Write the failing tests** (fake `detect`, `match_box` and liveness through monkeypatch; no models)

```python
def test_only_the_main_face_is_matched(): ...        # 2 boxes, big unregistered + small registered -> unknown,
                                                     # match_box called once, with the big box
def test_edge_face_is_still_judged(): ...            # box partly outside the frame -> verdict, no exception
def test_empty_gallery_gives_unknown(): ...
def test_liveness_error_by_mode(): ...               # score None: block -> not_live; log -> match
def test_login_logs_one_line_without_names(caplog): ...
def test_model_unavailable_gives_no_face_and_error(): ...  # keeps today's "Model not loaded" error text
```

- [ ] **Step 2:** GATE `tests/test_face_login_service.py`. Expected: FAIL.
- [ ] **Step 3:** Implement. `detect` converts the detector's `Result` (`position`, `size`, `confidence`) to
  `FaceBox`; `match_box` runs landmarks and the identifier on `[roi]` exactly as `match_enrollment_face` does today.
- [ ] **Step 4:** GATE `tests/test_face_login_service.py`, then GATE (all). Expected: PASS; anything that imported
  `identify_frame` fails only until Task 4, so run GATE all after Task 4 if it does.
- [ ] **Step 5:** Commit `Face login: one verdict per frame, only the biggest face matched`.

### Task 4: The routes take 5 frames

**Files:**
- Modify: `app/routers/api_face.py` (`/identify` :33, `/login` :51), `app/routers/api_auth.py` (`/api/auth/face-login`),
  `app/routers/auth.py` (`/auth/face-frame` :54, `/auth/confirm-login` :69)
- Test: `tests/test_face_login_routes.py`

**Interfaces:**
- Consumes: `FaceRecognitionService.login` (Task 3).
- Produces: each route takes multipart `files` (1 to `FACE_LOGIN_FRAMES` images; more is 422 `too_many_frames`; a
  file over 2 MB is 413). An undecodable file is a `no_face` frame; all undecodable is 400 `Invalid image`. Answers keep
  today's keys (`identified`, `name`, `distance`, `face_count`, `error`, plus `token`/`u_id` where they had them) and
  add `reason` and `frames`.

- [ ] **Step 1: Write the failing tests** (fake service `login`, the fake pool of `tests/test_face_enroll.py`)

```python
def test_login_with_five_frames_issues_a_token(): ...
def test_reason_is_returned_when_not_identified(): ...   # two_faces -> {"identified": False, "reason": "two_faces"}
def test_six_frames_is_422(): ...
def test_one_bad_frame_does_not_spoil_the_login(): ...   # 4 good + 1 garbage; and 5 garbage -> 400
def test_every_face_route_uses_login(): ...              # /api/face/login, /api/face/identify,
                                                         # /api/auth/face-login, /auth/face-frame
def test_confirm_login_needs_a_ticket(): ...             # see Step 3
```

- [ ] **Step 2:** GATE `tests/test_face_login_routes.py`. Expected: FAIL.
- [ ] **Step 3:** Implement. **Ruling (pre-existing hole):** `/auth/confirm-login` today creates a session for any
  `name` posted to it, so the face rules would not protect the older page. `/auth/face-frame` now answers a
  `login_ticket` when identified: `URLSafeTimedSerializer(SECRET_KEY, salt="face-login-ticket")` over the face label,
  valid 60 s. `/auth/confirm-login` takes `ticket` instead of `name` and refuses a missing, bad or expired one with 400
  (no redirect). The register redirect for a recognised face with no account keeps working from the ticket's label.
- [ ] **Step 4:** GATE (all). Expected: all pass (baseline + new).
- [ ] **Step 5:** Commit `Face login routes: several frames, a reason code, a ticket for the older page`
  (semgrep skipped, say so).

### Task 5: Registration uses the same main-face rule

**Files:**
- Modify: `app/services/face_recognition_service.py` (`match_enrollment_face` :201), `app/routers/api_face.py`
  (`save_enrollment` :156)
- Test: `tests/test_face_enroll.py` (extend)

**Interfaces:**
- Consumes: `detect`, `main_face`, `match_box` (Tasks 1 and 3).
- Produces: `match_enrollment_face` picks the main face with `main_face` instead of `rois[0]` and returns
  `{"box", "label", "distance", "error", "reason"}`, `reason` one of `ok`, `no_face`, `two_faces`, `come_closer`.
  `save_enrollment` refuses photo `i` for `two_faces` and `come_closer` with the same status and shape it uses today
  for "No face detected in photo {i}", the message naming the reason.

- [ ] **Step 1: Write the failing tests**: a registration photo with two similar-size faces is refused and nothing is
  saved; a photo with one small and one big face stores the big face's crop; a too-small face is refused.
- [ ] **Step 2:** GATE `tests/test_face_enroll.py`. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** GATE (all). Expected: PASS.
- [ ] **Step 5:** Commit `Face registration: the biggest face only, and never two people`.

### Task 6: Both login pages send 5 frames and show the reason

**Files:**
- Modify: `frontend_source/src/lib/ai-api.ts` (`faceLogin`, `identifyFace`: take `File[]`, append each as `files`,
  return `reason` and `frames`), `frontend_source/src/pages/Login.tsx` (`handleFaceLogin` :79), `frontend_source/src/i18n.ts`
  (keys `login.face.comeCloser`, `login.face.twoFaces`, `login.face.notLive`, `login.face.unknown`, both languages,
  copy from Global Constraints), `static/js/face_login.js` (5 frames per attempt, the ticket),
  the older login template's confirm form (hidden `ticket` instead of `name`)
- Test: `frontend_source/tests/face-login.spec.ts`

**Interfaces:**
- Consumes: the route answers of Task 4.

- [ ] **Step 1: Write the failing Playwright tests** (mock `/api/face/login`, fake camera via
  `--use-fake-device-for-media-stream`): one press sends exactly 5 files; each reason shows its copy in English after
  switching the language; `match` with a token goes to `/dashboard`; the button stays disabled while the 5 frames are
  taken and sent.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement: take a frame every 200 ms (5 in about 1 s) from the video into JPEG blobs (quality 0.9,
  as today), then one request. Show "Scanning…" / 「掃描中…」 while it runs. The older page sends 5 frames per attempt
  instead of one every 1.5 s, waits for the answer before the next attempt, and posts the ticket.
- [ ] **Step 4:** Frontend gate and the spec. Expected: tsc and build clean, spec passes.
- [ ] **Step 5:** Commit `Login pages: 5 frames per face login, and why it failed`.

### Task 7: Docs

**Files:**
- Modify: `CLAUDE.md` (a subsection "Face login: the biggest face, 5 frames, liveness (Nov 2026)" under Auth: the rule,
  the settings, the reason codes, log mode, the ticket), `HANDOFF.md` (section 15: what changed, how to switch
  `FACE_LIVENESS_MODE` to `block` after measuring, the measurement still to do with the user), `.env.example` (the six
  settings, commented, with defaults).

- [ ] **Step 1:** Write them, with file:line for every claim.
- [ ] **Step 2:** GATE (all) once more. Expected: PASS.
- [ ] **Step 3:** Commit `Docs: face login picks the biggest face`.

### After the tasks (controller, with the user)

- Rebuild with `.\scripts\rebuild-app.ps1` only while no dose session runs, then log in with the user on this laptop:
  normal light, dim light, a phone photo and a printout. Record the liveness scores in HANDOFF §15. Switching to
  `block` is the user's call.
