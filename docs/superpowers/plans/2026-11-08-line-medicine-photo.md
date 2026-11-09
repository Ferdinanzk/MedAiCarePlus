# Medicine Photos over LINE: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A patient sends a photo of a medicine bag to the LINE account; the server reads it with the existing OCR,
answers with the list and one **Save all** button, and saves exactly what the Scan page would save, only after the
patient presses it. Pending LINE lists also show on the Scan page.

**Architecture:** The Scan page's save rules (`frontend_source/src/lib/scan.ts`) are ported once to a pure Python
module (`app/services/scan_rules.py`), held equal to the TypeScript by one shared golden file that both test suites
read. A new service (`app/services/line_photo.py`) handles an image event: who sent it, consent, a 10-an-hour limit, a
draft row per photo, then a background task that downloads, reads and answers. A signed postback runs Save all in one
transaction through the same insert code as `POST /api/medications`. A small consent-gated API lets the Scan page
list and open pending drafts.

**Tech Stack:** FastAPI, asyncpg (no ORM, fake pools in tests), `requests` for LINE (sync, run in threads), the
existing `OCRService`, the notification outbox, React + i18next.

**Spec:** `docs/superpowers/specs/2026-11-08-face-login-and-line-photo-design.md`, Part 2 (lines 139-199) and the
release gate (231-238). Read the code map first where a task names it: it lists exact lines for everything below.

## Global Constraints

- Branch `version-4-competition`. Commit locally only; never push. New commits, no amend, no trailers.
- Nothing is saved without the patient's press (Save all) or a save on the Scan page.
- Only one-to-one chats: an image from a group or room gets no reply and no processing.
- The photo is never stored or logged. Logs never contain LINE user ids, reply tokens, message ids, OCR text or
  medicine names: one line per photo `"[LINE] photo: <outcome>"` and per Save all `"[LINE] photo save: <outcome>"`.
- Limits: photo download at most `10 * 1024 * 1024` bytes; at most 10 photos per patient per rolling hour (every
  photo that got a row counts, refused and failed ones included); a draft lasts 24 hours from the photo.
- LINE endpoints: loading animation `POST https://api.line.me/v2/bot/chat/loading/start`, body
  `{"chatId": <sender>, "loadingSeconds": 60}` (5 to 60, multiples of 5); content
  `GET https://api-data.line.me/v2/bot/message/{messageId}/content` (confirm the host against LINE's "Get content"
  reference before writing it). Postback `data` at most 300 characters.
- Every new LINE message to a linked patient goes through `outbox.enqueue(..., reply_token, received_at)` with
  `kind="line_photo"`, priority 1, then `outbox.wake()`. Never `med_reminder`. Answers to a sender who is not a linked
  patient use `asyncio.to_thread(line_svc.reply_text, reply_token, sender, text)`, as the verification-code reply does.
- Every LINE HTTP call and `OCRService.process_image` run off the event loop (`asyncio.to_thread`); OCR inside
  `asyncio.wait_for(..., OCR_GEMINI_IMAGE_BUDGET if svc.gemini_api_key else OLLAMA_TIMEOUT)`.
- Message copy, exactly (zh-TW first, then English, one string, as the existing LINE replies are):
  - not linked: 「請先在 App 裡連結您的 LINE。/ Please link your LINE in the app first.」
  - family: 「只有病人本人可以用照片新增藥物。/ Only the patient can add medicines by photo.」
  - consent: 「請先打開 App 並同意使用條款。/ Please open the app and accept the terms first.」
  - too many: 「目前照片太多，請稍後再試。/ Too many photos right now, please try again later.」
  - too big: 「這張照片太大了，請傳小一點的照片。/ This photo is too large. Please send a smaller one.」
  - unreadable: 「我看不清楚這張照片，請在光線好的地方再拍一次。/ I couldn't read this photo. Please take it again in good light.」
  - busy: 「讀取服務現在忙碌中，請過幾分鐘再傳一次。/ The reader is busy right now. Please send the photo again in a few minutes.」
  - list footer: 「要修改的話，請打開 App 的掃描頁。/ To change them, open the Scan page in the app.」
  - nothing to save: 「沒有可以直接儲存的藥，請打開 App 的掃描頁檢查。/ Nothing can be saved directly. Please check on the Scan page in the app.」
  - saved: 「已儲存 {n} 種藥。/ Saved {n} medicines.」
  - already: 「已經儲存過了。/ Already saved.」
  - expired: 「這份清單已過期，請重新傳送照片。/ This list has expired. Please send the photo again.」
  - save failed: 「儲存失敗，請在 App 的掃描頁再試一次。/ Couldn't save. Please try again on the Scan page in the app.」
  - Button label 「全部儲存 Save all」; button text 「按「全部儲存」就會加入您的藥物清單。」
- New authenticated routes use `Depends(get_consented_user)`; nothing is added to the exempt lists of
  `tests/test_consent_enforcement.py`.
- `sql/init.sql` runs whole at every start: only `IF NOT EXISTS` forms.
- **Backend gate (GATE):** Docker Desktop running (`docker info`), then
  ```bash
  MSYS_NO_PATHCONV=1 docker run --rm -v "C:/Reachy_Mini/repo/MedAiCarePlus:/src" -w /src --entrypoint sh medcareai2-app \
    -c "pip install -q pytest httpx; python -m pytest tests -q -p no:cacheprovider"
  ```
  Shorthand **GATE** `<path or -k>`. Baseline at `ae431cb`: 3668 passed, 1 skipped.
- **Frontend gate:** `cd frontend_source && npx tsc -b --noEmit && npm run build`; the scan unit tests with
  `node --experimental-strip-types --test --test-isolation=none tests/scan.unit.ts` (Git Bash needs the flag);
  Playwright with a throwaway config outside the repo (testDir `./tests`, `npx vite preview --port 4173`).
- semgrep is not installed: a commit touching `api_notify.py`, `line_service.py` or the new routes says plainly that
  the scan was skipped.

## Rulings in this plan (Claude's calls, for the user to check)

1. Replies are bilingual strings (no language column exists on `"user"`).
2. A busy or failed reader gets its own "busy" text, so a patient is not told to retake a good photo.
3. A photo over 10 MB gets the "too big" text (the spec only said refuse).
4. Two active patients with the same LINE id: treated as not linked (refused).
5. Duplicates inside one photo are both saved, as the Scan page does today.
6. Save all uses today's date in `MEDCARE_TIMEZONE` and the page's fixed 120 early-dose minutes.
7. The Scan page marks a LINE draft saved only when every ticked card saved.
8. Expired drafts are deleted by the nightly retention job (and after a restore); no new scheduler job.

## Review Focus

1. **LINE delivers the same photo event twice** (a redelivery). One draft, one read, one answer. Task 3:
   `test_redelivered_photo_is_read_once`.
2. **The reader finishes after the 50 s reply window.** The answer goes by push, not a dead reply token. Task 3:
   `test_late_read_goes_by_push` (asserts the outbox row's `received_at` is the webhook's).
3. **Consent is withdrawn between the photo and the press.** Save all refuses with the consent text and saves
   nothing. Task 4: `test_save_all_rechecks_consent`.
4. **A photo hosted outside LINE** (`message.contentProvider.type == "external"`). No download; the unreadable text.
   Task 3: `test_external_photo_is_not_downloaded`.
5. **Midnight in Taipei while the server's clock is UTC.** Save all's "today" is the Taipei date. Task 4:
   `test_save_all_uses_the_taipei_date`.

---

### Task 1: The Scan page's save rules in Python, held equal by a golden file

**Files:**
- Create: `app/services/scan_rules.py`
- Create: `tests/scan_parity.json`, `frontend_source/tests/scan_parity.gen.ts`
- Modify: `frontend_source/tests/scan.unit.ts` (one new test that reads the golden file)
- Test: `tests/test_scan_rules.py`

**Interfaces:**
- Produces (all pure, dicts in the TypeScript shapes, `today` an ISO `YYYY-MM-DD` string):
  `medicines_of(result: dict) -> list[dict]`, `course_start(result: dict, today: str) -> str`,
  `last_day_for(start: str, days: int | None, use_before: str | None) -> str`,
  `draft_from(med: dict, result: dict, today: str) -> dict` (keys as `MedicineDraft`: include, name, dosage, doseForm,
  stock, unitsPerDose, timesPerDay, maxDaily, days, lastDay, slots, customTimes, asNeeded, source, status),
  `same_name(a: str, b: str) -> bool`, `mark_duplicates(drafts: list[dict], active_names: list[str]) -> list[dict]`,
  `draft_problem(draft: dict, today: str | None = None) -> str | None` (the TS codes),
  `dose_limits(draft: dict) -> dict`, `medication_payload(draft: dict, result: dict) -> dict`.

- [ ] **Step 1: Generate the golden file from scan.ts.** `scan_parity.gen.ts` imports scan.ts and writes
  `tests/scan_parity.json` as `{"cases": [{"name", "today", "result", "active_names", "expected": {"drafts",
  "problems", "payloads"}}]}`, where drafts are `markDuplicates(medicinesOf(result).map(m => draftFrom(m, result,
  today)), active_names)` without `source`, problems are `draftProblem(d, today)` per draft, and payloads are
  `JSON.parse(JSON.stringify(medicationPayload(d, result)))` per draft. Cases, at least:
  the three-medicine receipt of `scan.unit.ts` dispensed 2026-09-28 and 2026-10-03 with TODAY 2026-10-04; as needed
  with a 3-4 range and interval 6; QOD (interval 48, morning); `stock` absent vs `stock: null`; stock 2.5 and
  units 1.005 (half rounding); duplicates `"Gastro F.C. tablets"` vs `"GASTRO FC TABLETS"` and a full-width
  `"ＧＡＳＴＲＯ　ＦＣ"`; no slots and no meal marks (schedule null); days 0; `date_dispensed` `"2026-02-30"` and
  `"2026-13-01"` (JS rolls them over); `use_before` `"N/A"`; a 6-times-a-day medicine with custom times. Run:
  `cd frontend_source && node --experimental-strip-types tests/scan_parity.gen.ts`.
- [ ] **Step 2: Node reads it back.** New test in `scan.unit.ts`: `'the golden file matches scan.ts'` recomputes each
  case and `assert.deepEqual`s it. Run the scan unit tests: 16 pass.
- [ ] **Step 3: Failing Python test.** `tests/test_scan_rules.py::test_golden_cases_match_scan_ts` loads
  `tests/scan_parity.json` and asserts, per case, drafts (minus `source`), problems and payloads equal the expected
  JSON (`json.loads(json.dumps(...))` on the Python side). Plus `test_js_rounding` (`_js_round(2.5) == 3`,
  `_js_round(-2.5) == -2`, `_round2(1.005)` equals JS `Math.round(1.005*100)/100`), `test_absent_stock_differs_from_null`,
  `test_impossible_date_rolls_over_like_js` (`_add_days("2026-02-30", 0) == "2026-03-02"`,
  `_add_days("2026-13-01", 0) == "2027-01-01"`). Run **GATE** `tests/test_scan_rules.py`: fails (no module).
- [ ] **Step 4: Implement `app/services/scan_rules.py`.** A line-by-line port. Rules the port must keep: `na()` keeps
  the value untrimmed; JS rounding is `math.floor(x + 0.5)`; numbers become strings like JS `String()` (`15.0` ->
  `"15"`); `Number("")` cases as in scan.ts; keys that are `undefined` in TS are left out of the dict, `null` stays
  `None`; `"stock" in med` decides absent vs null; `_add_days` rolls over like `new Date(y, m-1, d+days)`; date
  comparisons are string comparisons; `sameName` key is NFKC, upper case, then `re.sub(r"[\s.．·・]+", "", ...)`
  with `\s` widened to JS's set (add `\ufeff`); the early-dose minutes are the constant 120.
- [ ] **Step 5:** **GATE** `tests/test_scan_rules.py`: passes. Scan unit tests: 16 pass.
- [ ] **Step 6: Commit** `"Scan rules in Python: the Scan page's drafts and payloads, held equal by a golden file"`.

### Task 2: The draft table and LINE's loading and content calls

**Files:**
- Modify: `sql/init.sql` (append), `app/services/line_service.py`
- Test: `tests/test_line_photo_db.py`, `tests/test_line_service_content.py`

**Interfaces:**
- Produces: table `line_photo_draft`; `LineService.start_loading(self, chat_id: str, seconds: int = 60) -> bool`;
  `LineService.get_content(self, message_id: str, max_bytes: int) -> tuple[str, bytes | None]` with status
  `"ok" | "too_large" | "failed"`. Both never raise and never log ids.

- [ ] **Step 1: Failing tests.**
  - `test_draft_table_is_idempotent_and_cascades`: `init.sql` contains exactly once, after `CREATE TABLE IF NOT EXISTS
    "user"`:
    ```sql
    -- LINE medicine photo (8 Nov 2026): one row per photo a patient sends; the photo itself is never kept.
    CREATE TABLE IF NOT EXISTS line_photo_draft (
        draft_id UUID PRIMARY KEY,
        u_id INTEGER NOT NULL REFERENCES "user"(u_id) ON DELETE CASCADE,
        line_message_id VARCHAR(64) NOT NULL UNIQUE,
        status VARCHAR(10) NOT NULL DEFAULT 'reading'
            CHECK (status IN ('reading', 'pending', 'saved', 'failed', 'refused')),
        result JSONB,
        error_code VARCHAR(40),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        expires_at TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '24 hours',
        saved_at TIMESTAMPTZ
    );
    CREATE INDEX IF NOT EXISTS idx_line_photo_draft_user ON line_photo_draft(u_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_user_line_id ON "user"(line_id);
    ```
  - `test_start_loading_posts_the_chat_and_seconds` (URL, JSON body, bearer header; a 500 or an exception gives
    `False`); `test_get_content_streams_and_caps` (`max_bytes + 1` bytes gives `"too_large"`; exactly `max_bytes`
    gives `"ok"`; 404 and `requests.Timeout` give `"failed"`; the URL is the api-data host and the message id);
    `test_content_calls_log_no_ids` (caplog has neither the message id nor the chat id). Fake `requests.post/get` on
    `line_service` as `tests/test_line_reply.py` does. Run: fail.
- [ ] **Step 2: Implement** the table and the two methods (`requests.get(..., stream=True, timeout=30)`, read
  `iter_content` until `max_bytes + 1`). Not available (no token) gives `False` / `("failed", None)`.
- [ ] **Step 3:** **GATE** the two files: pass. **GATE** all: 3668 + new, 1 skipped.
- [ ] **Step 4: Commit** `"LINE photo: the draft table, the loading animation and the content download"`.

### Task 3: Receiving a photo, reading it, answering with the list

**Files:**
- Create: `app/services/line_photo.py`
- Modify: `app/routers/api_notify.py` (image branch before the `!= "text"` skip, at about line 134)
- Test: `tests/test_line_photo_receive.py`

**Interfaces:**
- Consumes: Task 1 `medicines_of`, `draft_from`, `mark_duplicates`, `draft_problem`; Task 2 table and LineService
  methods; `consent_service.get_state/is_current`; `outbox.enqueue/wake`; `memory.local_today()`.
- Produces: `KIND = "line_photo"`, `MAX_BYTES`, `PER_HOUR = 10`; the copy constants of Global Constraints;
  `async def receive(event: dict, received_at: datetime) -> str` (returns the outcome word it logged);
  `async def process(draft_id: str, u_id: int, sender: str, message_id: str, reply_token: str | None,
  received_at: datetime) -> str`; `def list_messages(result: dict, today: str, active_names: list[str], draft_id: str)
  -> list[dict]`; `def sign_save(draft_id: str) -> str`; `def verify_save(data: str) -> str | None` (the draft id);
  `_spawn(coro)` (the task-set pattern of `dose_emotion._spawn`, so tests can replace it).

`receive`, in order: (1) source not `"user"` -> `"ignored"`, nothing sent; (2) `SELECT u_id FROM "user" WHERE line_id=$1
AND user_active=TRUE`: one row is the patient; zero rows and a `family_contacts` row with `line_id=$1 AND relationship
IS DISTINCT FROM 'user'` -> family text; zero rows otherwise, or two or more rows -> not-linked text (both by
`reply_text`); (3) core consent not current -> consent text by outbox, nothing downloaded; (4) `INSERT ... (draft_id,
u_id, line_message_id) ON CONFLICT (line_message_id) DO NOTHING RETURNING draft_id`: no row -> `"duplicate"`, nothing
sent; (5) count rows of this `u_id` with `created_at > NOW() - INTERVAL '1 hour'`; over 10 -> status `refused`,
too-many text; (6) `contentProvider.type` not `"line"` -> status `failed`, unreadable text; (7) else `_spawn(process(...))`.

`process`: `start_loading` (result ignored); `get_content(message_id, MAX_BYTES)`: `too_large` -> failed + too-big
text, `failed` -> failed + busy text; OCR: `OCRServiceError` with code `invalid_image`, `ocr_no_prescription_text` or
`ocr_incomplete_schedule` -> unreadable text, any other error or the timeout -> busy text; success -> `UPDATE ... SET
status='pending', result=$2::jsonb`, then `list_messages` (today = `local_today().isoformat()`, active names =
`SELECT med_name FROM medication WHERE u_id=$1 AND is_active IS NOT FALSE`) by outbox with dedupe key
`f"line_photo:{draft_id}:read"`. Every answer to the patient carries the webhook's `reply_token` and `received_at`.

`list_messages`: one text message `"我讀到 {n} 種藥：/ I read {n} medicines:"` then one line per medicine
`"{i}. {name}：{amount}，{times}，{days}"` (amount = `amount_each_intake` unless `"N/A"`, else `"每次 {unitsPerDose}"`;
times = `"一天 {k} 次（早上、中午…）"` from the slots and custom times, or `"需要時服用"` when as needed; days =
`"{d} 天"` when set; parts that are empty are left out), with `"（已在清單中）"` after a duplicate and
`"（請在 App 檢查）"` after a draft with a problem or not included, then the footer. If at least one medicine is
savable (included, not duplicate, no problem), a second message: a buttons template, text and label from Global
Constraints, postback `data = sign_save(draft_id)`, `displayText` = the label; else the nothing-to-save text instead.
`sign_save` gives `f"action=line_photo_save&id={draft_id}&s={hmac16}"` (HMAC-SHA256 of
`f"line_photo_save|{draft_id}"` with `SECRET_KEY`, first 16 hex); `verify_save` checks length <= 300, exactly those
keys, the action, a canonical UUID and `compare_digest`.

- [ ] **Step 1: Failing tests** (fake pool with a draft table in a dict, recorded outbox rows, `_spawn` replaced by a
  list, LineService faked): `test_group_and_room_photos_are_ignored`, `test_unlinked_sender_is_told_to_link`,
  `test_two_patients_on_one_line_id_count_as_unlinked`, `test_family_sender_is_refused`,
  `test_patient_without_current_consent_is_refused_without_download`, `test_redelivered_photo_is_read_once`,
  `test_eleventh_photo_in_an_hour_is_refused` (and the refused row counts toward the next),
  `test_external_photo_is_not_downloaded`, `test_too_big_photo`, `test_download_failure_is_busy`,
  `test_unreadable_photo_codes` (the three codes), `test_reader_errors_and_timeout_are_busy`,
  `test_good_read_stores_the_result_and_answers_with_the_list_and_button` (outbox row: kind `line_photo`, the reply
  token, received_at), `test_late_read_goes_by_push`, `test_duplicates_and_problems_are_marked_in_the_list`,
  `test_nothing_savable_has_no_button`, `test_sign_and_verify_save` (tampered, long, wrong action, dose-confirm data
  all `None`), `test_dose_confirmation_ignores_save_data` (`dose_confirmation.verify_postback(sign_save(x)) is None`),
  `test_photo_logs_carry_no_ids_or_names` (caplog), `test_webhook_routes_images_before_the_text_skip` (a signed
  webhook with an image event calls `line_photo.receive`; a text event still reaches the verification-code path).
  Run: fail.
- [ ] **Step 2: Implement** `line_photo.py` and the webhook branch:
  `if msg.get("type") == "image": await line_photo.receive(event, received_at); continue` inside the existing
  per-event `try` style (an exception is logged by type and the next event still runs).
- [ ] **Step 3:** **GATE** `tests/test_line_photo_receive.py tests/test_line_reply.py tests/test_postback.py
  tests/test_webhook_signature.py tests/test_line_kinds.py`: pass. **GATE** all.
- [ ] **Step 4: Commit** `"LINE photo: who sent it, the hourly limit, reading it and answering with the list"`
  (semgrep skipped: not installed).

### Task 4: Save all

**Files:**
- Modify: `app/routers/api_medications.py` (extract the insert), `app/services/line_photo.py`,
  `app/routers/api_notify.py` (postback dispatch)
- Test: `tests/test_line_photo_save.py`

**Interfaces:**
- Produces: `async def insert_medication(conn, u_id: int, payload: MedicationPayload) -> dict` in
  `api_medications.py` (the INSERT and `_generate_intake_schedule` lifted out of `create_medication` unchanged;
  `create_medication` calls it; same SQL text, same return dict);
  `async def handle_save(conn, data: str, sender: str, reply_token: str | None, received_at: datetime) -> str | None`.

`handle_save`: `verify_save(data)` is `None` -> `None` (the caller passes it on to `dose_confirmation`). Then in one
`conn.transaction()`: `SELECT d.*, u.line_id, u.user_active FROM line_photo_draft d JOIN "user" u USING (u_id) WHERE
d.draft_id=$1 FOR UPDATE OF d`. No row, another sender, or an inactive user -> expired text by `reply_text`, nothing
saved. `saved` -> already text. Not `pending` or `expires_at <= now` -> expired text. `consent_service.fetch_state(conn,
u_id)` not current -> consent text. Else: active names (as Task 3), drafts = `mark_duplicates([draft_from(m, result,
today) ...], names)` with today = `local_today().isoformat()`; for each draft that is included, not a duplicate and has
no `draft_problem(d, today)`: `MedicationPayload(**medication_payload(d, result))` (a `ValidationError` makes it a
skipped problem) then `insert_medication`; `UPDATE ... SET status='saved', saved_at=NOW()`; the saved text plus one line
per skipped medicine `"{name}（已在清單中 / already saved）"` or `"{name}（請在 App 檢查 / check in the app）"`. An
exception inside the transaction rolls it back and answers the save-failed text. Answers to the owner go by outbox
(dedupe key `f"line_photo:{draft_id}:save:{received_at.isoformat()}"`), others by `reply_text`.

Webhook: in the postback branch, `if await line_photo.handle_save(conn, data, sender, token, received_at) is None:`
then the existing `dose_confirmation.handle_postback(...)` call.

- [ ] **Step 1: Failing tests:** `test_create_medication_still_inserts_the_same_row` (the existing
  `test_medication_fields.py` stays green unchanged), `test_save_all_by_the_owner_saves_every_savable_medicine` (the
  INSERT args equal the golden payloads of Task 1's receipt case), `test_save_all_by_someone_else_saves_nothing`,
  `test_second_press_says_already_saved`, `test_after_24_hours_says_expired`, `test_save_all_skips_duplicates_and_problems_and_names_them`,
  `test_save_all_rechecks_consent`, `test_save_all_uses_the_taipei_date` (clock at 2026-10-04 16:30 UTC = 10-05
  00:30 Taipei: an ended-course check uses 10-05), `test_a_failed_insert_rolls_back_and_says_so`,
  `test_dose_confirm_postbacks_still_reach_their_handler`, `test_concurrent_presses_save_once` (two `handle_save` on
  one fake row lock: one save, one "already"). Run: fail.
- [ ] **Step 2: Implement.**
- [ ] **Step 3:** **GATE** `tests/test_line_photo_save.py tests/test_medication_fields.py tests/test_postback.py
  tests/test_dose_confirmation.py`: pass. **GATE** all.
- [ ] **Step 4: Commit** `"LINE photo: Save all saves what the Scan page would, once, after the patient's press"`
  (semgrep skipped).

### Task 5: LINE lists on the Scan page

**Files:**
- Create: `app/routers/api_line_drafts.py`; Modify: `app/main.py` (include the router)
- Modify: `frontend_source/src/pages/Scan.tsx`, `frontend_source/src/lib/ai-api.ts`, `frontend_source/src/lib/i18n.ts`
- Test: `tests/test_line_drafts_api.py`, `frontend_source/tests/scan.spec.ts` (new tests)

**Interfaces:**
- Produces: router prefix `/api/line-drafts`, every route `Depends(get_consented_user)`, only the caller's rows,
  only `status='pending' AND expires_at > NOW()`:
  `GET ""` -> `[{"id", "created_at", "count"}]` (count = number of medicines, newest first);
  `GET "/{draft_id}"` -> `{"id", "created_at", "result"}` or 404 `{"detail": "not_found"}`;
  `POST "/{draft_id}/saved"` -> `{"saved": true}` (sets `saved`, `saved_at`) or 404.

Scan page: on open, if logged in, fetch the list; above the camera, one row per draft: 「來自 LINE：{count} 種藥，{HH:mm}」
/ "From LINE: {count} medicines, {HH:mm}" with an Open button. Open fetches the draft, then does what a fresh scan does
from `setParsed` on (`draftFrom` with `isoDate(new Date())`, `markDuplicates` with the active names) and remembers the
draft id. No photo preview. After the save loop, if a LINE draft is open and every included draft is `saved`, POST
`/saved` and drop it from the list.

- [ ] **Step 1: Failing tests:** API: `test_lists_only_my_pending_unexpired_drafts`, `test_open_someone_elses_draft_is_404`,
  `test_mark_saved`, `test_routes_need_consent` (the enforcement test also stays green). Playwright (mocked API, the
  existing helpers): `'a LINE list shows and opens into cards'`, `'saving every card marks the LINE list saved'`,
  `'a failed card keeps the LINE list'`. Run: fail.
- [ ] **Step 2: Implement.**
- [ ] **Step 3:** **GATE** the API tests and `tests/test_consent_enforcement.py`; frontend gate; Playwright: the new
  tests pass (`scan.spec.ts:148` is the known BUG-38 failure).
- [ ] **Step 4: Commit** `"Scan page: lists read from LINE photos open into the usual cards"`.

### Task 6: Export, clean-up and docs

**Files:**
- Modify: `app/routers/api_account.py` (`EXPORT_TABLES` gets `"line_photo_draft"`),
  `app/jobs/conversation_retention_job.py` (`run_retention` also runs `DELETE FROM line_photo_draft WHERE expires_at <
  NOW()`), `tests/test_replay_ledger.py` (allow that DELETE), `CLAUDE.md`, `HANDOFF.md`
- Test: `tests/test_account.py` (stays green), `tests/test_line_photo_cleanup.py`

- [ ] **Step 1: Failing tests:** `test_export_includes_line_drafts`, `test_retention_deletes_expired_drafts`,
  `test_restore_replay_deletes_expired_drafts`. Run: fail.
- [ ] **Step 2: Implement.** CLAUDE.md: a "LINE medicine photo (Nov 2026)" subsection under the LINE section (flow,
  kinds, limits, the golden file rule "change scan.ts and scan_rules.py together and regenerate
  tests/scan_parity.json", the release gate: before real patients send photos, a new notice version must name medicine
  photos and Gemini; the user writes the text). HANDOFF: section 16 with the same facts and how to test with a real
  LINE account.
- [ ] **Step 3:** **GATE** all; frontend gate.
- [ ] **Step 4: Commit** `"LINE photo: export, nightly clean-up, docs and the release gate"`.
