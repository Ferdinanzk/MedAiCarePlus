# LINE Medication-Reminder Switch Implementation Plan (Part A)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An admin-only, system-wide switch that stops LINE medication reminders, a banner on every dashboard while it
is off, free replies to family's answers and verification codes, and the month's LINE usage on the admin card.

**Architecture:**
- A small module (`line_pause`) holds the switch in memory, loaded at startup and written through by the admin route.
  The app is one process (`app.serve`), so no cross-process sync is needed.
- `LineService` takes a required `kind` on every send and refuses `med_reminder` while paused. Only the missed-dose
  job and `reminders._line` send that kind.
- Reply tokens ride on outbox rows and on the webhook's direct answers. A reply that fails falls back to push.

**Tech Stack:** FastAPI, asyncpg (stubbed in tests), `requests`, React + Vite + Tailwind, Playwright.

**Spec:** `docs/superpowers/specs/2026-11-02-voice-id-and-line-admin-design.md`, Part A, and its decisions table.
Parts B to E get their own plans after Part B's checks.

## Global Constraints

- **Paused:** the dose-time reminder (「🔔 用藥提醒」, `missed_dose_job.py:197`), the missed-dose "take it now" texts
  (「⚠️ 逾時用藥提醒」, `missed_dose_job.py:224`), and a `set_reminder` medicine reminder sent on LINE
  (`reminders._line`).
- **Never paused:**
  - missed-dose alerts (`send_missed_dose_alert`, the 「❌ 用藥未完成」 text);
  - family confirmation requests and replies, and dose events and videos;
  - safety alerts, robot-offline notices;
  - verification replies, the weekly summary, emotion and refill alerts, the skip notice;
  - a non-medicine `set_reminder`.
- **A paused message is never sent later.** An outbox row becomes `status='cancelled'`, `last_error='reminders_paused'`.
- **Admin:** `ADMIN_EMAILS` in `.env`, comma-separated, compared lower-cased with `"user".email`. A non-admin gets
  403 `{"detail": "admin_only"}`. The switch has no other gate.
- **Consent:** every new route depends on `get_consented_user` (directly or through `require_admin`), and
  `tests/test_consent_enforcement.py` must keep passing.
- **Reply window:** `REPLY_WINDOW_SECONDS = 50`. LINE says to use a reply token within one minute and only once; a
  reply carries at most 5 messages; replies don't count toward the monthly quota.
- **Quota endpoints:**
  - `GET https://api.line.me/v2/bot/message/quota` gives `{"type": "limited", "value": n}` or `{"type": "none"}`;
  - `GET https://api.line.me/v2/bot/message/quota/consumption` gives `{"totalUsage": n}`;
  - both are cached `QUOTA_CACHE_SECONDS = 300`.
- **Banner copy:**
  - zh-TW: 「管理員已暫停 LINE 用藥提醒（自 {time} 起）。Reachy 仍會在機器人上提醒，其他 LINE 訊息照常發送。」
  - en: "LINE medication reminders paused by the admin since {time}. Reachy still reminds on the robot, and other LINE
    messages still go out."
- **Logs:** never print LINE user ids, reply tokens, message text or the channel token.
- **Backend gate:**

  ```bash
  MSYS_NO_PATHCONV=1 docker run --rm -v "C:/Reachy_Mini/repo/MedAiCarePlus:/src" -w /src --entrypoint sh medcareai2-app \
    -c "pip install -q pytest httpx; python -m pytest tests -q -p no:cacheprovider"
  ```

  Shorthand below: **GATE** `<path or -k>`. Baseline on `983a6ba`, 2 Nov 2026: **3565 passed, 1 skipped** (84 s).
- **Frontend gate:** `cd frontend_source && npx tsc -b --noEmit && npm run build && npx playwright test <spec>`.
- **Git:** branch `line-reminder-switch` from `quickfix-intake-voice` (`983a6ba`). Commit locally only, and never
  push without the user's word.
  - Before any commit that touches the webhook or the LINE token code, run semgrep
    (`semgrep scan --config auto app/services/line_service.py app/routers/api_notify.py app/routers/api_line_admin.py`),
    or state plainly that it was skipped (not installed on this PC).

## Review Focus

1. **A paused reminder row in the outbox when the switch goes back on.** It must stay cancelled, not go out late.
   Test in Task 2: `test_paused_row_stays_cancelled_after_resume`.
2. **A medicine `set_reminder` with no medicine name** ("remind me to take my medicine at 8") is still a medicine
   reminder, and "remind me to call my daughter" is not. Test in Task 3: `test_line_kind_by_text`.
3. **A family button pressed long after it arrived.** The token is too old, so the answer must still arrive by push.
   Test in Task 4: `test_old_reply_token_goes_by_push`.
4. **`ADMIN_EMAILS` empty, or an admin account with no email.** Nobody is admin, and the card stays hidden with no
   error. Test in Task 5: `test_no_admin_configured_is_403`.
5. **The quota endpoints fail** (LINE down, or a token without the right). The card still shows and works the switch,
   with "usage unavailable". Test in Task 5: `test_quota_failure_keeps_card`.

---

### Task 1: The switch in memory and in the database

**Files:**
- Create: `app/services/line_pause.py`
- Modify: `sql/init.sql` (after `ops_state`, about line 221), `app/config.py` (after `LINE_WEBHOOK_ENDPOINT_URL`,
  about line 73), `app/main.py` (lifespan, near `ai_settings.set_current`, about line 61), `.env.example` (if
  present)
- Test: `tests/test_line_pause.py`

**Interfaces:**
- Produces:
  - `line_pause.MED_REMINDER = "med_reminder"`, `line_pause.OTHER = "other"`, `line_pause.PAUSABLE_KINDS = frozenset({MED_REMINDER})`
  - `line_pause.blocks(kind: str) -> bool`
  - `line_pause.state() -> dict`: `{"paused": bool, "since": datetime | None, "by": int | None}`
  - `async line_pause.load(conn) -> None`
  - `async line_pause.set_paused(conn, paused: bool, by_u_id: int, now: datetime) -> dict` (returns `state()`)
  - `config.ADMIN_EMAILS: frozenset[str]`
  - Table `line_switch_log(log_id BIGSERIAL PK, paused BOOLEAN NOT NULL, changed_by INTEGER REFERENCES "user"(u_id) ON DELETE SET NULL, changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW())`
  - The `ops_state` key `line_reminders_paused`, whose value is JSON `{"since": iso, "by": u_id}`. The row is present
    only while paused.

- [ ] **Step 1: Write the failing tests** in `tests/test_line_pause.py`, with a fake conn that records
  `ops_state` / `line_switch_log` statements (pattern: `tests/test_outbox_dispatcher.py` `OutboxDB`):
  - `test_blocks_only_med_reminder_while_paused`: after `set_paused(conn, True, 7, NOW)`,
    `blocks("med_reminder") is True` and `blocks("other") is False`. After `set_paused(conn, False, 7, NOW)`,
    `blocks("med_reminder") is False`.
  - `test_set_paused_writes_log_and_state`: one `line_switch_log` insert per call with `(paused, 7)`. While paused,
    the `ops_state` row holds `{"since": NOW.isoformat(), "by": 7}`; when resumed, the row is deleted.
  - `test_load_restores_after_restart`: a conn whose `ops_state` returns that JSON gives `state() == {"paused": True, "since": NOW, "by": 7}`
    after `load(conn)`; no row gives `paused False`.
  - `test_pausing_twice_keeps_first_since`: pausing again while paused keeps the earlier `since` and still logs.
- [ ] **Step 2: Run** GATE `tests/test_line_pause.py`. Expected: FAIL, `ModuleNotFoundError: app.services.line_pause`.
- [ ] **Step 3: Implement the module.**
  - The state is a module-level dict.
  - `set_paused` runs its two writes in `conn.transaction()` and updates memory only after they succeed.
  - `config.ADMIN_EMAILS = frozenset(e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip())`.
  - Add the `line_switch_log` DDL (`CREATE TABLE IF NOT EXISTS`) to `sql/init.sql`.
  - In the lifespan, call `await line_pause.load(conn)` on the same connection as `ai_store.load`.
  - Add `ADMIN_EMAILS=` with a comment to `.env.example` if the file exists.
- [ ] **Step 4: Run** GATE `tests/test_line_pause.py`. Expected: PASS.
- [ ] **Step 5: Commit**: `git add app/services/line_pause.py app/config.py app/main.py sql/init.sql tests/test_line_pause.py .env.example` then
  `git commit -m "LINE: a system-wide switch for medication reminders, kept in ops_state with a change log"`

### Task 2: `LineService` takes a kind; the outbox honours the switch

**Files:**
- Modify: `app/services/line_service.py` (`send_text` line 26, `push_messages` line 47, and every `send_*` helper),
  `app/services/outbox_dispatcher.py` (`_send` line 49, `dispatch_once` line 62)
- Modify, passing `kind=line_pause.OTHER`:
  - `app/jobs/emotion_alert_job.py:70`, `app/jobs/refill_reminder_job.py:70`;
  - `app/routers/api_intake.py:107`, `app/routers/api_notify.py:276`;
  - and the webhook's sends at `api_notify.py:159-176`, which Task 4 replaces.
- Modify, passing `kind=line_pause.MED_REMINDER`: `app/jobs/missed_dose_job.py:197` and `:224` only. Lines 265
  and 290 send `OTHER`.
- Test: `tests/test_outbox_dispatcher.py` (extend; update `FakeLine.push_messages` to accept `kind`),
  `tests/test_line_kinds.py` (new)

**Interfaces:**
- Consumes: `line_pause.blocks`, `line_pause.MED_REMINDER`, `line_pause.OTHER`
- Produces:
  - `LineService.send_text(to: str, message: str, *, kind: str) -> dict`. While blocked it returns
    `{"sent": False, "paused": True, "error": "reminders_paused"}`.
  - `LineService.push_messages(to: str, messages: list[dict], retry_key: str, *, kind: str) -> dict`. While blocked
    it returns `{"status": "paused", "http_status": None, "request_id": None, "error": "reminders_paused"}`.
  - `dispatch_once` reads `kind` from the row and turns `paused` into `status='cancelled', last_error='reminders_paused', attempts=attempts+1`.

- [ ] **Step 1: Write the failing tests.**
  - `tests/test_outbox_dispatcher.py`:
    - `test_paused_kind_is_cancelled_not_retried`: a `med_reminder` row while paused ends `cancelled` with
      `last_error == "reminders_paused"` and makes no LINE request (`FakeLine` returns `paused` for that kind).
    - `test_paused_row_stays_cancelled_after_resume`: then resume, then `dispatch_once` again, and the row is still
      `cancelled` with no new call.
    - `test_other_kinds_send_while_paused`: a `dose_confirm` row is accepted while paused.
    - `OutboxDB.add` gains `kind="other"`, and its `fetch` returns `kind`.
  - `tests/test_line_kinds.py`:
    - `test_every_send_text_call_names_a_kind`: AST-walk every `.py` under `app/` and assert each call to an
      attribute `send_text` or `push_messages` has a `kind` keyword. Skip `websocket.send_text` by requiring that the
      receiver name is not `websocket`.
    - `test_only_reminder_sends_use_med_reminder`: the AST finds `MED_REMINDER` passed only in
      `app/jobs/missed_dose_job.py` (exactly 2 calls) and `app/services/reminders.py` (from Task 3; until then, assert
      `<= 1` file).
    - `test_send_text_refuses_med_reminder_while_paused`: patch `requests.post` to fail the test if called. Check
      that `send_text(..., kind="med_reminder")` returns `paused` while paused and that
      `send_text(..., kind="other")` calls `requests.post`.
- [ ] **Step 2: Run** GATE `tests/test_outbox_dispatcher.py tests/test_line_kinds.py`. Expected: FAIL (`TypeError` on the
  `kind` keyword, and the AST test lists the callers).
- [ ] **Step 3: Implement.**
  - Make `kind` keyword-only and required on both methods. The pause check comes before the `_available` check.
  - Every `send_*` helper passes `kind=line_pause.OTHER`.
  - The dispatcher's SELECT adds `kind`, `_send` passes `kind=row["kind"]`, and a new
    `if result.get("status") == "paused"` branch issues the cancelled UPDATE.
  - Update all the callers listed above.
- [ ] **Step 4: Run** the full GATE. Expected: everything that passed before still passes, plus the new tests.
- [ ] **Step 5: Commit**: `git commit -am "LINE: every send names its kind; medication reminders stop while the switch is off"`
  (add `tests/test_line_kinds.py`).

### Task 3: `set_reminder` medicine reminders on LINE get their own kind

**Files:**
- Modify: `app/services/reminders.py` (`_line`, line 340)
- Test: `tests/test_reminders.py` (extend)

**Interfaces:**
- Consumes: `line_pause.MED_REMINDER`, the existing `reminders._mentions_medicine(text: str | None) -> bool`
- Produces: `reminders.line_kind(text: str) -> str`, which gives `"med_reminder"` when `_mentions_medicine(text)` and
  `"reminder"` otherwise. `_line` enqueues with `kind=line_kind(text)`.

- [ ] **Step 1: Write the failing tests:**
  - `test_line_kind_by_text`:
    - `line_kind(sentence("medicine", "zh-TW")) == "med_reminder"`;
    - `line_kind(sentence("named", "en", name="Adapalene")) == "med_reminder"`;
    - `line_kind(sentence("named_position", "zh-TW", name="Adapalene", position=1)) == "med_reminder"`;
    - `line_kind(sentence("plain", "zh-TW", what="打電話給女兒")) == "reminder"`.
  - `test_line_fallback_enqueues_med_reminder_kind`: with the existing fake pool of `test_reminders.py`, `_line`
    for a medicine text enqueues `kind == "med_reminder"`.
- [ ] **Step 2: Run** GATE `tests/test_reminders.py`. Expected: FAIL (`AttributeError: line_kind`).
- [ ] **Step 3: Implement** `line_kind` and use it in `_line`. Update the `MED_REMINDER` file count in
  `test_only_reminder_sends_use_med_reminder` to exactly these two files.
- [ ] **Step 4: Run** GATE `tests/test_reminders.py tests/test_line_kinds.py`. Expected: PASS.
- [ ] **Step 5: Commit**: `git commit -am "Reminders: a medicine reminder sent on LINE is a medication reminder for the switch"`

### Task 4: Free replies for family's answers and verification codes

**Files:**
- Modify:
  - `app/services/line_service.py`: add `reply_messages`, `reply_text` and `verification_success_text`.
  - `app/services/outbox.py`: `enqueue` gains `reply_token`.
  - `app/services/outbox_dispatcher.py`: reply first.
  - `app/services/dose_confirmation.py`: `handle_postback` (line 393) and `_enqueue_replies` (line 433) thread
    `reply_token` to the two enqueues addressed to the answering `contact`. The `other` contacts stay push.
  - `app/routers/api_notify.py`: the webhook passes `event.get("replyToken")`. Lines 159, 166 and 174 answer by
    `reply_text`; line 169 (to the patient) stays push with `kind=OTHER`.
- Test: `tests/test_outbox_dispatcher.py`, `tests/test_line_reply.py` (new)

**Interfaces:**
- Consumes: `LineService.send_text(..., kind=)`, `push_messages(..., kind=)` from Task 2
- Produces:
  - `LineService.reply_messages(reply_token: str, messages: list[dict]) -> dict`: POST to
    `https://api.line.me/v2/bot/message/reply` with `{"replyToken", "messages"}`, at most 5 messages. Status is
    `"accepted"` on 2xx, `"invalid_token"` on 400, otherwise `"failed"`.
  - `LineService.reply_text(reply_token: str | None, to: str, message: str) -> dict`: reply first, then on anything
    but `accepted` (or no token) `send_text(to, message, kind=line_pause.OTHER)`.
  - `line_service.verification_success_text(patient_name: str) -> str`: the existing text of
    `send_verification_success`, which now uses it.
  - `outbox.enqueue(..., reply_token: str | None = None, received_at: datetime | None = None)`. With a token, the
    payload gains `"reply_token"` and `"reply_until": (received_at + REPLY_WINDOW_SECONDS).isoformat()`.
  - `dose_confirmation.handle_postback(conn, data: str, sender_line_id: str, reply_token: str | None = None) -> dict | None`
  - `outbox_dispatcher.REPLY_WINDOW_SECONDS = 50`. While `now < reply_until`, `_send` tries `reply_messages`; on
    anything but `accepted` it pushes in the same attempt. A row whose window has passed goes straight to push.

- [ ] **Step 1: Write the failing tests.**
  - `test_outbox_dispatcher.py`:
    - `test_fresh_reply_token_uses_reply`: the row is accepted, and `FakeLine` saw one `reply_messages` call and no
      push.
    - `test_invalid_reply_token_falls_back_to_push`: the reply returns `invalid_token`, then one push, then accepted.
    - `test_old_reply_token_goes_by_push`: `reply_until` is in the past, so a push is made and no reply is tried.
  - `tests/test_line_reply.py`:
    - `test_reply_messages_posts_to_reply_endpoint`: patch `requests.post`; check the URL, body keys and a 400 giving
      `invalid_token`.
    - `test_reply_text_falls_back_to_push_without_token`.
    - `test_postback_ack_carries_reply_token_others_do_not`: drive `handle_postback` with the fixtures of
      `tests/test_demo_dose.py` (CONTACT, `env.db`). The ack row's payload has `reply_token`, and the other contacts'
      `dose_confirm_answered` rows have none.
- [ ] **Step 2: Run** GATE `tests/test_outbox_dispatcher.py tests/test_line_reply.py`. Expected: FAIL.
- [ ] **Step 3: Implement** to the interfaces above. Reply tokens are never logged; the dispatcher's
  `last_error` holds the status only.
- [ ] **Step 4: Run** the full GATE. Expected: PASS (`tests/test_demo_dose.py` unchanged in behaviour).
- [ ] **Step 5:** Run semgrep, or record that it was skipped. Then commit:
  `git commit -am "LINE: answers to family buttons and verification codes go as free replies, push as the fallback"`
  (add `tests/test_line_reply.py`).

### Task 5: Admin routes, usage, and the banner's state

**Files:**
- Create: `app/routers/api_line_admin.py`
- Modify:
  - `app/dependencies.py`: add `require_admin`;
  - `app/main.py`: include the router;
  - `app/services/line_service.py`: add `quota()`.
- Test: `tests/test_line_admin_api.py` (new); `tests/test_consent_enforcement.py` must pass unchanged

**Interfaces:**
- Consumes: `line_pause.state`, `line_pause.set_paused`, `config.ADMIN_EMAILS`
- Produces:
  - `async dependencies.require_admin(user: dict = Depends(get_consented_user)) -> dict`: it reads
    `SELECT email FROM "user" WHERE u_id = $1`. When the email is NULL, empty, or not in `ADMIN_EMAILS` after
    `.lower()`, it raises 403 `admin_only`.
  - `LineService.quota() -> dict`: `{"limit": int | None, "used": int | None, "error": str | None}`, where
    `limit=None` with `error=None` means unlimited (`type == "none"`). It is cached 300 s in-process, and a failure is
    not cached.
  - `GET /api/line/reminders` (`get_consented_user`) gives `{"paused": bool, "since": iso | null}`.
  - `GET /api/admin/line` (`require_admin`) gives `{"paused", "since", "changed_by": name | null, "quota": {"limit", "used", "error"}}`.
  - `PUT /api/admin/line/reminders` (`require_admin`) takes `{"paused": StrictBool}` (anything else is 422) and gives
    the same body as `GET /api/admin/line`. The server logs one INFO line: `LINE medication reminders paused|resumed by user <u_id>`.

- [ ] **Step 1: Write the failing tests.** Use FastAPI `TestClient` with `dependency_overrides`, following an existing
  router test such as the Reachy settings tests.
  - `test_banner_state_for_any_user`
  - `test_admin_can_pause_and_resume`: two `line_switch_log` rows; `GET /api/line/reminders` follows.
  - `test_non_admin_gets_403`
  - `test_no_admin_configured_is_403`: `ADMIN_EMAILS` empty, or the admin's email NULL.
  - `test_paused_must_be_boolean`: `"true"` and `1` both give 422.
  - `test_quota_failure_keeps_card`: `quota()` returns an error, and the GET still answers 200 with
    `quota.error` set.
  - `test_quota_is_cached`: two GETs within 300 s make two `requests.get` calls in total (quota and consumption),
    not four.
- [ ] **Step 2: Run** GATE `tests/test_line_admin_api.py tests/test_consent_enforcement.py`. Expected: FAIL (404s).
- [ ] **Step 3: Implement** to the interfaces. `quota()` uses `requests.get` with `timeout=10` and the channel token.
- [ ] **Step 4: Run** the full GATE. Expected: PASS.
- [ ] **Step 5: Commit**: `git add app/routers/api_line_admin.py tests/test_line_admin_api.py` then
  `git commit -am "LINE admin: the reminder switch, its state for the banner, and the month's usage"`

### Task 6: Dashboard banner and the admin card

**Files:**
- Create:
  - `frontend_source/src/lib/line-admin-api.ts`, following `ai-connections-api.ts` and its `call`/`ApiError` from
    `./reachy-api`;
  - `frontend_source/src/components/LinePausedBanner.tsx`;
  - `frontend_source/src/components/LineAdminCard.tsx`;
  - `frontend_source/tests/line-admin.spec.ts`.
- Modify:
  - `frontend_source/src/components/Layout.tsx`: the banner directly above `<Outlet />` inside `<main>`, line 70;
  - `frontend_source/src/pages/Settings.tsx`: `<LineAdminCard />` after `<AiConnectionsCard />`, line 184;
  - `frontend_source/src/i18n.ts`: zh-TW and en keys `lineAdmin.*`.

**Interfaces:**
- Consumes: the three routes of Task 5
- Produces:
  - `getLineReminders(): Promise<{paused: boolean; since: string | null}>`
  - `getLineAdmin(): Promise<LineAdmin>`
  - `setLineReminders(paused: boolean): Promise<LineAdmin>`
  - `interface LineAdmin { paused: boolean; since: string | null; changed_by: string | null; quota: { limit: number | null; used: number | null; error: string | null } }`

**Copy (zh-TW / en):**

| Key | zh-TW | en |
|---|---|---|
| `lineAdmin.banner` | the Global Constraints text | the Global Constraints text |
| `lineAdmin.title` | 「LINE 用藥提醒」 | "LINE medication reminders" |
| `lineAdmin.on` | 「目前：開啟」 | "Now: on" |
| `lineAdmin.off` | 「目前：已暫停（自 {time} 起，{name}）」 | "Now: paused (since {time}, by {name})" |
| `lineAdmin.pause` | 「暫停用藥提醒」 | "Pause medication reminders" |
| `lineAdmin.resume` | 「恢復用藥提醒」 | "Resume medication reminders" |
| `lineAdmin.help` | 「只停止 LINE 用藥提醒。錯過用藥通知、家人確認、安全警示和機器人離線通知照常發送。暫停期間的提醒不會補發。」 | "Stops only LINE medication reminders. Missed-dose alerts, family confirmations, safety alerts and robot-offline notices still go out. Reminders skipped while paused are not sent later." |
| `lineAdmin.usage` | 「本月已用 {used} / {limit} 則」 | "This month: {used} of {limit} messages" |
| `lineAdmin.usageUnlimited` | 「本月已用 {used} 則（無上限）」 | "This month: {used} messages (no limit)" |
| `lineAdmin.usageUnavailable` | 「無法取得用量」 | "Usage unavailable" |

**Behaviour:**
- **Banner:**
  - It reads `getLineReminders()` on mount and every 60 s, and shows only while `paused`.
  - Errors show nothing.
  - It is an amber strip with `role="status"`.
- **Card:**
  - It calls `getLineAdmin()` on mount and renders nothing on 403 or any error, so it is never shown to a non-admin.
  - It has one button (at least 48 px tall) that is disabled while a request runs. The state flips only after the PUT
    succeeds, as the Reachy card's switches do.

- [ ] **Step 1: Write the failing Playwright tests** in `tests/line-admin.spec.ts`, with `page.route` mocks as in
  `tests/ai-connections.spec.ts`:
  - `banner shows while paused and hides when on`
  - `card hidden for a non-admin (403)`
  - `admin pauses: state flips after the PUT succeeds and the button re-enables`
  - `failed PUT keeps the old state`
  - `usage line: limited, unlimited, unavailable`
  - `no horizontal scroll at 390 px`
- [ ] **Step 2: Run** `cd frontend_source && npx playwright test tests/line-admin.spec.ts`. Expected: FAIL.
- [ ] **Step 3: Implement** the API file, the two components, the i18n keys and the two mounts.
- [ ] **Step 4: Run** the frontend gate: `npx tsc -b --noEmit && npm run build && npx playwright test tests/line-admin.spec.ts`. Expected: PASS.
- [ ] **Step 5: Screenshot check** at 1280×900 and 390×844 (banner on Dashboard; card on Settings, as admin), saved
  to `C:\Reachy_Mini\.playwright-mcp\line-admin-*.png`.
- [ ] **Step 6: Commit**: `git add frontend_source/src/lib/line-admin-api.ts frontend_source/src/components/LinePausedBanner.tsx frontend_source/src/components/LineAdminCard.tsx frontend_source/tests/line-admin.spec.ts` then
  `git commit -am "Dashboard: a banner while LINE reminders are paused, and the admin's LINE card"`

### Task 7: Docs and an end-to-end check on the real stack

**Files:**
- Modify: `CLAUDE.md` (a short "LINE reminder switch" subsection under the consent and Phase 2 rules: the kinds,
  `ADMIN_EMAILS`, replies, never sent later), `HANDOFF.md` (new section)

- [ ] **Step 1:** Write the CLAUDE.md subsection and the HANDOFF section from the Global Constraints. Keep it
  factual, with file:line references.
- [ ] **Step 2: Rebuild only while no dose session runs:** `.\scripts\rebuild-app.ps1`. Then confirm that
  `curl -s localhost:8080/health` answers and that the `[STARTUP]` lines show no FAILED.
- [ ] **Step 3: Real check, with the user's OK for LINE sends.**
  - Set `ADMIN_EMAILS` to the admin account's email, then run `docker compose up -d app`.
  - Pause from the card and give the patient a demo dose with a reminder due.
  - Check that `notification_outbox` and the job made no medication-reminder request:

    ```sql
    SELECT kind, status, last_error FROM notification_outbox ORDER BY outbox_id DESC LIMIT 5;
    ```

  - Check that a family confirmation still arrives on LINE and that its answer comes back as a reply.
  - Resume from the card.
  - Record the card's usage number before and after.
- [ ] **Step 4: Commit**: `git commit -am "Docs: the LINE medication-reminder switch"`
