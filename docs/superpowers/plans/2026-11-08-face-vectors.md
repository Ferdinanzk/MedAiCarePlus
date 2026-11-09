# Face Vectors Instead of Face Photos: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Face registration stores the 256 numbers of each registration photo's main face in the database and keeps no
photo; face login matches against those stored vectors; existing photos are converted once, and the folder stays until
the user says it may go.

**Architecture:** A small module (`app/services/face_vectors.py`) owns the table: encode/decode, load rows, and build a
gallery object with the exact interface of today's `FacesDatabase` (`.database` list of `Identity(label,
descriptors)`, `match_faces`, `__len__`, `__getitem__`), so matching, `match_enrollment_face`, the monitor and the
tests keep working unchanged. The service starts with an empty gallery and is filled from the database in the
lifespan and after every registration. A one-time command converts the folder's photos with today's exact gallery
computation, so existing users match as they do now.

**Tech Stack:** FastAPI, asyncpg (no codecs: BYTEA), numpy float32, OpenVINO (existing models), React (Onboarding).

**Spec:** `docs/superpowers/specs/2026-11-08-face-login-and-line-photo-design.md`, Part 3 (lines 201-229) and the
release gate (231-238). Code map with exact lines: the session scratchpad `part3map/` (summarised in the tasks).

## Global Constraints

- Branch `version-4-competition`. Commit locally only; never push. New commits, no amend, no trailers.
- A vector is the `face-reidentification-retail-0095` descriptor: 256 float32 values, stored as BYTEA
  `desc.astype("<f4").tobytes()` (1024 bytes) and read back with `np.frombuffer(b, "<f4")`.
- Matching is unchanged: `FaceIdentifier(match_threshold=FACE_MATCH_THRESHOLD (0.3), match_algo="MIN_DIST")`,
  `FacesDatabase.Identity.cosine_dist` (scipy cosine × 0.5), each label's closest vector, then the closest label.
- Labels are lower case (`"user".face_label` is stored lower case); one `Identity` per label.
- New registrations compute the vector the way login does: the main face box (`main_face`) on the full frame,
  `_match_identities`' landmarks and identifier path.
- Vectors are never logged, never sent to the browser, never in the export (only how many and when).
- The face photo folder (`models/face_recognition/face_gallery`) is not deleted or emptied by any code in this plan;
  the backup keeps tarring it; deleting it is the user's decision after testing (spec).
- `sql/init.sql` runs whole at every start: only `IF NOT EXISTS` forms.
- **Backend gate (GATE):** Docker Desktop running, then
  ```bash
  MSYS_NO_PATHCONV=1 docker run --rm -v "C:/Reachy_Mini/repo/MedAiCarePlus:/src" -w /src --entrypoint sh medcareai2-app \
    -c "pip install -q pytest httpx; python -m pytest tests -q -p no:cacheprovider"
  ```
  Shorthand **GATE** `<path or -k>`. Baseline: the HEAD this plan starts from (record it in the ledger).
- **Frontend gate:** `cd frontend_source && npx tsc -b --noEmit && npm run build`.
- semgrep is not installed: commits touching registration or account routes say plainly that it was skipped.

## Rulings in this plan (Claude's calls, for the user to check)

1. BYTEA float32, not REAL[]: exact round trip, smallest rows.
2. The older registration page's photo route (`POST /auth/register-photos`) is retired: it saves photos before an
   account exists, so it has no account to store vectors under. It answers 410 with "Please register in the app.";
   the older page shows that message. Face registration happens in the app's onboarding.
3. Converted photos use today's gallery computation (the whole stored crop), so existing users match exactly as now;
   new registrations use the login computation (the spec). A measurement after Task 4 compares the two on a real
   face; if the new way is worse, switch new registrations to the crop computation.
4. The conversion is a command run once with the user's OK, not automatic at startup. Until it runs, users
   registered before it cannot log in by face (the app logs how many folder photos have no vectors).
5. Account export keeps the photo files while they still exist (they are still data held) and adds a
   `face_vectors` summary: count, first and last date.
6. The "registered without account" refusal stays; with vectors it only means an inactive account's rows.

## Review Focus

1. **Nobody has vectors yet** (fresh install, or before the conversion). Login answers `unknown`, registration
   works, nothing raises. Task 1: `test_empty_table_gives_an_empty_gallery`; Task 2: `test_first_ever_registration`.
2. **Re-registering** replaces that user's 3 vectors, never adds to them. Task 2:
   `test_reenrolling_replaces_your_vectors`.
3. **Two people registering the same face at the same time.** One wins, the other gets `face_already_registered`.
   Task 2: `test_two_accounts_enrolling_one_face_at_once_cannot_both_pass` (kept, rewritten for rows).
4. **The conversion run twice, or after an old backup put deleted users' photos back.** Second run converts
   nothing; a label with no active account is skipped and counted. Task 4:
   `test_conversion_is_once_and_skips_unknown_labels`.
5. **A corrupt row** (wrong length). It is skipped with a count-only warning; the rest of the gallery loads. Task 1:
   `test_a_bad_row_is_skipped`.

---

### Task 1: The vector table and a gallery built from it

**Files:**
- Create: `app/services/face_vectors.py`
- Modify: `sql/init.sql` (append), `app/services/face_recognition_service.py`, `app/main.py`
- Test: `tests/test_face_vectors.py`

**Interfaces:**
- Produces: table `face_vector`; in `face_vectors.py`: `to_bytes(desc) -> bytes`, `from_bytes(b: bytes) -> np.ndarray`,
  `def build_gallery(rows: list[tuple[str, bytes]]) -> FacesDatabase` (an instance made with `object.__new__` whose
  `.database` is a list of `FacesDatabase.Identity(label, [vectors])` in row order, labels lower-cased),
  `async def fetch_rows(conn) -> list[tuple[str, bytes]]` (`SELECT face_label, vector FROM face_vector f JOIN "user" u
  USING (u_id) WHERE u.user_active = TRUE ORDER BY vector_id`), `async def reload(pool=None) -> int` (fetches and
  calls the service's `load_gallery`, returns the label count);
  `FaceRecognitionService.load_gallery(self, gallery) -> None` (`@synchronized`, `self.face_id.set_faces_database`).

- [ ] **Step 1: Failing tests.**
  - `test_vector_table_is_pinned` (style of `tests/test_line_photo_db.py`):
    ```sql
    -- Face vectors (8 Nov 2026): face login keeps the 256 numbers of each registration photo, never the photo.
    CREATE TABLE IF NOT EXISTS face_vector (
        vector_id BIGSERIAL PRIMARY KEY,
        u_id INTEGER NOT NULL REFERENCES "user"(u_id) ON DELETE CASCADE,
        face_label VARCHAR(100) NOT NULL,
        vector BYTEA NOT NULL CHECK (octet_length(vector) = 1024),
        source VARCHAR(10) NOT NULL CHECK (source IN ('enroll', 'photo')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_face_vector_user ON face_vector(u_id, created_at DESC);
    ```
  - `test_round_trip_is_exact` (float32 in, identical array out); `test_build_gallery_groups_labels_like_faces_database`
    (two rows of `pearl`, one of `jane` give two identities, lower case, `len == 2`, `gallery[0].label == "pearl"`);
    `test_gallery_matches_like_the_folder_gallery` (same vectors in a folder-style `FacesDatabase` made with
    `object.__new__` and in `build_gallery` give identical `match_faces(..., "MIN_DIST")` results);
    `test_empty_table_gives_an_empty_gallery` (`match_box` answers `(None, None)`);
    `test_a_bad_row_is_skipped` (a 1000-byte row is left out, the log has a count and no label);
    `test_service_starts_empty_and_lifespan_loads_rows` (the constructor no longer reads the folder; the lifespan
    calls `face_vectors.reload` after `FaceRecognitionService.get_instance()`; `tests/test_ai_startup.py` stubs it).
  Run: fail.
- [ ] **Step 2: Implement.** The constructor sets `build_gallery([])`; `reload_gallery` is removed and its callers
  move to `await face_vectors.reload()` (Task 2 and 3 update their callers; in this task keep a thin sync
  `reload_gallery` that logs and does nothing, so other code still runs until then). The startup print becomes a
  logged count. A lifespan WARNING counts folder photos whose label has no vector rows ("run the conversion",
  counts only).
- [ ] **Step 3:** **GATE** `tests/test_face_vectors.py tests/test_face_login_service.py tests/test_ai_startup.py`; GATE all.
- [ ] **Step 4: Commit** `"Face vectors: the table and a gallery built from it"`.

### Task 2: Registration stores vectors, not photos

**Files:**
- Modify: `app/services/face_recognition_service.py` (`match_enrollment_face`), `app/routers/api_face.py`
  (`save_enrollment`, `enroll_face`, `enrollment_status`, remove `_gallery_labels`/`_label_in_use`),
  `app/routers/auth.py` (`register_photos` retired), `app/templates/register.html` and/or `static/js/photo_capture.js`
  (show the 410 message), `frontend_source/src/pages/Onboarding.tsx` (only if `saved` changes shape — it must not)
- Test: `tests/test_face_enroll.py` (rewritten for rows), `tests/test_consent_enforcement.py` (stays green)

**Interfaces:**
- Consumes: Task 1 `to_bytes`, `reload`.
- Produces: `match_enrollment_face(...)` adds `"descriptor": np.ndarray | None` — computed for the main face with
  `_match_identities`' path **always** (also when no other label exists), `None` when refused; never in an HTTP answer.
  `save_enrollment(photos, label, u_id, *, caller) -> list[int] | JSONResponse` (the 3 new `vector_id`s).

`save_enrollment`: same checks and messages as today (decode, `no_face`/`two_faces`/`come_closer`, known face →
`_known_face_refusal`), all 3 photos checked before anything is stored; then in one transaction `DELETE FROM
face_vector WHERE u_id=$1` and 3 `INSERT ... (u_id, face_label, vector, source) VALUES ($1,$2,$3,'enroll') RETURNING
vector_id`; then `await face_vectors.reload()` while still holding `_enroll_lock`. `enroll_face` answers
`{"saved": [str(id) ...], "face_label": label}` (3 entries, so Onboarding's `saved.length === 3` holds; no paths, no
numbers) and sets `face_enrolled = TRUE` as today. `enrollment_status`: `count = SELECT count(*) FROM face_vector
WHERE u_id=$1`, `enrolled = bool(face_enrolled) or count > 0`. `register_photos`: 410
`{"error": "use_app_registration", "detail": "Please register in the app."}`.

- [ ] **Step 1: Failing tests** (rewrite the folder assertions as row assertions; keep every refusal test):
  `test_enrolling_stores_three_vectors_and_no_files` (no file in the gallery folder; 3 rows of 1024 bytes;
  `source='enroll'`), `test_the_answer_carries_no_paths_or_numbers`, `test_reenrolling_replaces_your_vectors`,
  `test_first_ever_registration` (empty gallery: a descriptor is still computed and stored),
  `test_two_accounts_enrolling_one_face_at_once_cannot_both_pass`, `test_any_refused_photo_stores_nothing`,
  `test_enrollment_status_counts_rows`, `test_legacy_register_photos_is_retired`,
  `test_descriptor_never_logged` (caplog). Run: fail.
- [ ] **Step 2: Implement.**
- [ ] **Step 3:** **GATE** `tests/test_face_enroll.py tests/test_consent_enforcement.py`; GATE all; frontend gate.
- [ ] **Step 4: Commit** `"Face registration: three vectors in the database, no photos"` (semgrep skipped).

### Task 3: Deletion, export and the in-memory gallery

**Files:**
- Modify: `app/services/deletion_ledger.py` (`refresh_gallery`), `app/routers/api_account.py` (export summary)
- Test: `tests/test_account.py`, `tests/test_replay_ledger.py` (both stay green, assertions updated)

**Interfaces:**
- Produces: `refresh_gallery(face_label)` drops that label's identity from the in-memory gallery under the service
  lock (no folder reload); the export's `account["face_vectors"] = {"count": n, "first": iso | None, "last": iso |
  None}` from `SELECT count(*), min(created_at), max(created_at) FROM face_vector WHERE u_id=$1`; `face_vector` is
  NOT in `EXPORT_TABLES`.

Deletion needs no new SQL: `face_vector.u_id ... ON DELETE CASCADE` goes with `DELETE FROM "user"` in the request path
and in the restore replay. `delete_gallery_files` stays (the folder still holds old photos; old backups too).

- [ ] **Step 1: Failing tests:** `test_export_summarises_vectors_without_numbers` (no `vector` key, no bytes anywhere
  in account.json, the summary present), `test_export_still_includes_remaining_photo_files`,
  `test_refresh_drops_the_identity_without_reading_the_folder`, `test_account_delete_cascades_vectors` (the delete
  SQL is the one `DELETE FROM "user"`; the replay test stays green). Run: fail.
- [ ] **Step 2: Implement.**
- [ ] **Step 3:** **GATE** `tests/test_account.py tests/test_replay_ledger.py tests/test_face_vectors.py`; GATE all.
- [ ] **Step 4: Commit** `"Face vectors: deletion by cascade, export says how many and when"`.

### Task 4: The one-time conversion, and docs

**Files:**
- Create: `app/ops/convert_face_gallery.py`
- Modify: `CLAUDE.md`, `HANDOFF.md` (section 17), `README.md` (the gallery line)
- Test: `tests/test_convert_face_gallery.py`

**Interfaces:**
- Produces: `async def convert(conn, service, gallery_dir: Path) -> dict` returning
  `{"converted": n_rows, "labels": n, "skipped_labels": n, "already": bool}`; `python -m app.ops.convert_face_gallery`
  prints only those counts.

`convert`: if any row has `source = 'photo'` → `{"already": True, ...}`, nothing written. Build today's folder gallery
exactly as today (`FacesDatabase(str(gallery_dir), service.face_id, service.lm_det)` — the whole stored crop, no
detector). For each identity: find the active `"user"` with `face_label = identity.label`; none → skipped. Insert
one row per descriptor (`source='photo'`) in one transaction. Then verify: `build_gallery(await fetch_rows(conn))`
has, for every converted label, the same number of vectors and each within cosine distance 1e-6 of the folder's;
any mismatch rolls back and raises. The folder is never changed.

- [ ] **Step 1: Failing tests** (fake service whose `FacesDatabase` is stubbed to fixed vectors, fake conn):
  `test_conversion_writes_one_row_per_photo`, `test_conversion_is_once_and_skips_unknown_labels`,
  `test_a_mismatch_rolls_back`, `test_folder_is_untouched`, `test_cli_prints_counts_only`. Run: fail.
- [ ] **Step 2: Implement.** Docs: CLAUDE.md "Face vectors (Nov 2026)" under "Auth — face tokens only" (table,
  BYTEA float32, gallery from rows, registration path, retired legacy route, conversion command, folder kept until the
  user's OK, a future recognition-model change means everyone registers again (old vectors cannot be recomputed
  without photos), the release gate line: the notice still says "face enrollment photos" and must say vectors in the next
  notice version). HANDOFF §17: deploy order (rebuild → run the conversion with the user's OK:
  `docker compose -p medcareai2 run --rm --no-deps app python -m app.ops.convert_face_gallery` → restart the app →
  test login → only then the user decides on the folder and on dropping `gallery.tar` from `backup.sh:18,28` and
  `restore.ps1:3`).
- [ ] **Step 3:** **GATE** all; frontend gate.
- [ ] **Step 4: Commit** `"Face vectors: one-time conversion of the old photos, docs"`.
- [ ] **Step 5 (controller, after review):** in a throwaway container with the real models and a throwaway database,
  register the 8 Nov selfie's main face both ways (converted crop vs login path) and log in with a third frame;
  record both distances in HANDOFF §17 (ruling 3).
