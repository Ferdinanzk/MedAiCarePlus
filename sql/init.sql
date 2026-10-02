-- MedAiCarePlus — PostgreSQL schema
-- Field names match the AIOT CAREBOX ERD diagram exactly.

CREATE TABLE IF NOT EXISTS "user" (
    u_id                SERIAL PRIMARY KEY,
    name                VARCHAR(100) NOT NULL,
    line_id             VARCHAR(100),
    face_label          VARCHAR(100) UNIQUE NOT NULL,
    "3_sided_photo_url" TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    user_active         BOOLEAN DEFAULT TRUE
);
ALTER TABLE "user" ADD COLUMN IF NOT EXISTS supabase_id VARCHAR(100) UNIQUE;
ALTER TABLE "user" ADD COLUMN IF NOT EXISTS email VARCHAR(200) UNIQUE;
ALTER TABLE "user" ADD COLUMN IF NOT EXISTS password_hash VARCHAR(200);
-- Onboarding state — backend source of truth so the setup wizard never repeats
-- already-completed steps after logout / new device / cleared localStorage.
ALTER TABLE "user" ADD COLUMN IF NOT EXISTS face_enrolled BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE "user" ADD COLUMN IF NOT EXISTS onboarding_complete BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS detail (
    detail_id  SERIAL PRIMARY KEY,
    u_id       INTEGER NOT NULL UNIQUE REFERENCES "user"(u_id) ON DELETE CASCADE,
    age        INTEGER,
    gender     VARCHAR(10),
    addres     TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    update_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS emotion (
    emot_id       SERIAL PRIMARY KEY,
    u_id          INTEGER NOT NULL REFERENCES "user"(u_id) ON DELETE CASCADE,
    emotion_type  VARCHAR(20) NOT NULL
        CHECK (emotion_type IN ('Angry','Happy','Neutral','Sad')),
    emotion_score REAL NOT NULL,
    note          TEXT,
    context       VARCHAR(50),
    time_stamp    TIMESTAMPTZ DEFAULT NOW()
);
-- Seed 43 predicts seven expressions; retain existing emotion rows.
ALTER TABLE emotion DROP CONSTRAINT IF EXISTS emotion_emotion_type_check;
ALTER TABLE emotion ADD CONSTRAINT emotion_emotion_type_check
    CHECK (emotion_type IN ('Angry','Disgust','Fear','Happy','Sad','Surprise','Neutral'));

CREATE TABLE IF NOT EXISTS medication (
    med_id             SERIAL PRIMARY KEY,
    u_id               INTEGER NOT NULL REFERENCES "user"(u_id) ON DELETE CASCADE,
    med_name           TEXT NOT NULL,
    schedule_time      JSONB,
    pill_prescribed    INTEGER NOT NULL DEFAULT 0,
    total_intake       INTEGER NOT NULL DEFAULT 0,
    actual_intake_time TIMESTAMPTZ,
    is_active          BOOLEAN DEFAULT TRUE,
    created_at         TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE medication ADD COLUMN IF NOT EXISTS dosage           VARCHAR(100);
ALTER TABLE medication ADD COLUMN IF NOT EXISTS pills_remaining  INTEGER NOT NULL DEFAULT 0;
ALTER TABLE medication ADD COLUMN IF NOT EXISTS instructions     TEXT;
ALTER TABLE medication ADD COLUMN IF NOT EXISTS warning          TEXT;
ALTER TABLE medication ADD COLUMN IF NOT EXISTS pill_description TEXT;
ALTER TABLE medication ADD COLUMN IF NOT EXISTS use_before       VARCHAR(50);
ALTER TABLE medication ADD COLUMN IF NOT EXISTS prescription_meta  JSONB;

CREATE TABLE IF NOT EXISTS intake (
    intk_id           SERIAL PRIMARY KEY,
    u_id              INTEGER NOT NULL REFERENCES "user"(u_id) ON DELETE CASCADE,
    med_id            INTEGER NOT NULL REFERENCES medication(med_id) ON DELETE CASCADE,
    emot_id           INTEGER REFERENCES emotion(emot_id) ON DELETE SET NULL,
    intake_stats      VARCHAR(10) NOT NULL DEFAULT 'pending'
        CHECK (intake_stats IN ('taken','skipped','pending','missed')),
    intake_time_stamp TIMESTAMPTZ DEFAULT NOW(),
    notify_stats      VARCHAR(10) DEFAULT 'pending'
        CHECK (notify_stats IN ('sent','pending','failed')),
    notify_time       TIMESTAMPTZ
);

ALTER TABLE intake ADD COLUMN IF NOT EXISTS detection_confidence FLOAT;
ALTER TABLE intake ADD COLUMN IF NOT EXISTS detection_method VARCHAR(50);
ALTER TABLE intake ADD COLUMN IF NOT EXISTS actual_intake_time TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS monitor_event (
    event_id UUID PRIMARY KEY,
    session_id UUID NOT NULL,
    u_id INTEGER NOT NULL REFERENCES "user"(u_id) ON DELETE CASCADE,
    intk_id INTEGER NOT NULL REFERENCES intake(intk_id) ON DELETE CASCADE,
    previous_status VARCHAR(10) NOT NULL,
    identity_distance REAL,
    detector_score REAL NOT NULL,
    detector_band VARCHAR(20) NOT NULL,
    emotion_probabilities JSONB,
    outcome VARCHAR(20) NOT NULL CHECK (outcome IN ('taken','rejected')),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    corrected_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_monitor_event_user ON monitor_event(u_id, recorded_at DESC);

-- ─────────────────────────────────────────────
-- LOGIN_LOG  (audit trail)
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS login_log (
    log_id        SERIAL PRIMARY KEY,
    u_id          INTEGER NOT NULL REFERENCES "user"(u_id) ON DELETE CASCADE,
    login_session VARCHAR(100),
    login_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS family_contacts (
    id                SERIAL PRIMARY KEY,
    u_id              INTEGER NOT NULL REFERENCES "user"(u_id) ON DELETE CASCADE,
    name              VARCHAR(100) NOT NULL,
    relationship      VARCHAR(50),
    phone             VARCHAR(30),
    line_id           VARCHAR(100),
    verification_code VARCHAR(20),
    verified          BOOLEAN DEFAULT FALSE,
    verified_at       TIMESTAMPTZ,
    notify_missed     BOOLEAN DEFAULT TRUE,
    notify_emotion    BOOLEAN DEFAULT TRUE,
    notify_weekly     BOOLEAN DEFAULT FALSE,
    notify_skipped    BOOLEAN DEFAULT TRUE,
    created_at        TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE family_contacts ADD COLUMN IF NOT EXISTS notify_skipped BOOLEAN DEFAULT TRUE;

CREATE INDEX IF NOT EXISTS idx_login_log_user ON login_log(u_id, login_at DESC);
CREATE INDEX IF NOT EXISTS idx_family_contacts_u_id  ON family_contacts(u_id);
CREATE INDEX IF NOT EXISTS idx_family_contacts_code  ON family_contacts(verification_code);
CREATE INDEX IF NOT EXISTS idx_emotion_user           ON emotion(u_id, time_stamp DESC);
CREATE INDEX IF NOT EXISTS idx_intake_user     ON intake(u_id, intake_time_stamp DESC);
CREATE INDEX IF NOT EXISTS idx_intake_med      ON intake(med_id);
CREATE INDEX IF NOT EXISTS idx_medication_user ON medication(u_id, is_active);

-- ─────────────────────────────────────────────
-- Notification Settings & Logs
-- ─────────────────────────────────────────────
ALTER TABLE intake ADD COLUMN IF NOT EXISTS reminder_sent BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE intake ADD COLUMN IF NOT EXISTS missed_reminders_sent INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS notification_settings (
    u_id INTEGER PRIMARY KEY REFERENCES "user"(u_id) ON DELETE CASCADE,
    remind_before_minutes INTEGER NOT NULL DEFAULT 5,
    remind_after_minutes INTEGER NOT NULL DEFAULT 10,
    remind_after_retries INTEGER NOT NULL DEFAULT 3,
    notify_family_on_missed BOOLEAN NOT NULL DEFAULT TRUE,
    notify_family_on_bad_mood BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS notification (
    id SERIAL PRIMARY KEY,
    u_id INTEGER NOT NULL REFERENCES "user"(u_id) ON DELETE CASCADE,
    category VARCHAR(20) NOT NULL CHECK (category IN ('user', 'family')),
    type VARCHAR(50) NOT NULL, -- 'upcoming_reminder', 'missed_reminder', 'missed_alert', 'emotion_alert', 'taken_confirmation'
    message TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ─────────────────────────────────────────────
-- Taken-confirmation (batched success notifications to family)
-- ─────────────────────────────────────────────
-- Per-intake flag so a "taken" confirmation is pushed to family only once.
ALTER TABLE intake ADD COLUMN IF NOT EXISTS taken_notified BOOLEAN NOT NULL DEFAULT FALSE;
-- Per-user master toggle + per-contact toggle (mirrors notify_family_on_missed / notify_skipped).
ALTER TABLE notification_settings ADD COLUMN IF NOT EXISTS notify_family_on_taken BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE family_contacts ADD COLUMN IF NOT EXISTS notify_taken BOOLEAN DEFAULT TRUE;

-- ─────────────────────────────────────────────
-- Legal notice versions, consent, deletion ledger (Phase 1)
-- ─────────────────────────────────────────────
-- One row per exact rendered text: an operator changing a fill-in changes
-- the hash without changing the version, and consent must reference what
-- the person actually saw.
CREATE TABLE IF NOT EXISTS legal_document (
    kind          VARCHAR(10) NOT NULL CHECK (kind IN ('core','robot')),
    terms_version VARCHAR(20) NOT NULL,
    language      VARCHAR(10) NOT NULL,
    sha256        CHAR(64) NOT NULL,
    published_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (kind, terms_version, language, sha256)
);

-- Append-only; current state is the highest consent_id per (u_id, scope).
CREATE TABLE IF NOT EXISTS consent (
    consent_id      BIGSERIAL PRIMARY KEY,
    u_id            INTEGER NOT NULL REFERENCES "user"(u_id) ON DELETE CASCADE,
    kind            VARCHAR(10) NOT NULL,
    terms_version   VARCHAR(20) NOT NULL,
    language        VARCHAR(10) NOT NULL,
    document_sha256 CHAR(64) NOT NULL,
    scope           VARCHAR(40) NOT NULL,
    granted         BOOLEAN NOT NULL,
    source          VARCHAR(20) NOT NULL CHECK (source IN ('register','reconsent','settings','pairing')),
    user_agent      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (kind, terms_version, language, document_sha256)
        REFERENCES legal_document(kind, terms_version, language, sha256)
);
CREATE INDEX IF NOT EXISTS idx_consent_latest ON consent(u_id, scope, consent_id DESC);

-- No FK to "user": entries must survive the account they describe so a
-- restored backup can re-apply the deletion.
CREATE TABLE IF NOT EXISTS deletion_ledger (
    ledger_id  BIGSERIAL PRIMARY KEY,
    kind       VARCHAR(30) NOT NULL,
    u_id       INTEGER NOT NULL,
    object_id  TEXT,
    deleted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ops_state (
    key        VARCHAR(40) PRIMARY KEY,
    value      TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─────────────────────────────────────────────
-- Reachy robot: devices, tasks, confirmations, outbox (Phase 2)
-- ─────────────────────────────────────────────
ALTER TABLE medication ADD COLUMN IF NOT EXISTS dose_form VARCHAR(20) NOT NULL DEFAULT 'solid_oral';
ALTER TABLE medication ADD COLUMN IF NOT EXISTS units_per_dose NUMERIC(4,2) NOT NULL DEFAULT 1;
ALTER TABLE medication DROP CONSTRAINT IF EXISTS medication_dose_form_check;
ALTER TABLE medication ADD CONSTRAINT medication_dose_form_check
    CHECK (dose_form IN ('solid_oral','liquid','inhaler','injection','topical','other'));

ALTER TABLE intake DROP CONSTRAINT IF EXISTS intake_intake_stats_check;
ALTER TABLE intake ALTER COLUMN intake_stats TYPE VARCHAR(20);
ALTER TABLE intake ADD CONSTRAINT intake_intake_stats_check
    CHECK (intake_stats IN ('taken','skipped','pending','missed','pending_confirmation'));

-- ─────────────────────────────────────────────
-- Stock by dose size, supply records, archived courses
-- ─────────────────────────────────────────────
-- A dose can be half a tablet (units_per_dose 0.5), so stock is fractional.
ALTER TABLE medication ALTER COLUMN pills_remaining TYPE NUMERIC(8,2);
-- Set when a course is stopped; archived medications keep their history.
ALTER TABLE medication ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;
-- Units a taken dose removed from stock, so an undo restores exactly that amount
-- even if units_per_dose was edited in between (NULL on rows taken before this column: 1).
ALTER TABLE intake ADD COLUMN IF NOT EXISTS units_taken NUMERIC(4,2);

CREATE TABLE IF NOT EXISTS medication_supply (
    supply_id   SERIAL PRIMARY KEY,
    u_id        INTEGER NOT NULL REFERENCES "user"(u_id) ON DELETE CASCADE,
    med_id      INTEGER NOT NULL REFERENCES medication(med_id) ON DELETE CASCADE,
    quantity    NUMERIC(8,2) NOT NULL CHECK (quantity > 0),
    note        VARCHAR(200),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_medication_supply_med ON medication_supply(med_id, created_at DESC);

-- auto_record defaults to FALSE while decision D2 (recording without pill
-- identification) is open: every observed event goes to caregiver confirmation.
CREATE TABLE IF NOT EXISTS reachy_device (
    device_id    UUID PRIMARY KEY,
    u_id         INTEGER NOT NULL REFERENCES "user"(u_id) ON DELETE CASCADE,
    token_hash   CHAR(64) NOT NULL UNIQUE,
    label        VARCHAR(50) NOT NULL DEFAULT 'Reachy Mini',
    auto_record  BOOLEAN NOT NULL DEFAULT FALSE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at   TIMESTAMPTZ,
    last_seen_at TIMESTAMPTZ,
    robot_reachable BOOLEAN,
    landmark_fps REAL,
    status_detail JSONB
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_device_per_user ON reachy_device(u_id) WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS reachy_task (
    task_id      UUID PRIMARY KEY,
    u_id         INTEGER NOT NULL REFERENCES "user"(u_id) ON DELETE CASCADE,
    slot_time    TIMESTAMPTZ NOT NULL,
    intk_ids     INTEGER[] NOT NULL,
    reason       VARCHAR(20) NOT NULL CHECK (reason IN ('upcoming','missed_retry','manual')),
    attempt      INTEGER NOT NULL DEFAULT 1,
    status       VARCHAR(20) NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued','leased','searching','in_progress','completed','not_found','aborted','expired')),
    lease_owner  UUID,
    lease_until  TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at  TIMESTAMPTZ,
    expires_at   TIMESTAMPTZ NOT NULL,
    detail       JSONB
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_task_per_slot ON reachy_task(u_id, slot_time)
    WHERE status IN ('queued','leased','searching','in_progress');
-- 'checkin': a conversation-only task (no doses), started from the web app.
ALTER TABLE reachy_task DROP CONSTRAINT IF EXISTS reachy_task_reason_check;
ALTER TABLE reachy_task ADD CONSTRAINT reachy_task_reason_check
    CHECK (reason IN ('upcoming','missed_retry','manual','checkin'));

-- Check-in conversations. Speech is turned into text on the robot; only text is stored.
-- Retention (robot notice §5): turns 30 days, turns quoted in a safety alert 180 days,
-- summaries and mood until the patient deletes them or the account.
CREATE TABLE IF NOT EXISTS conversation (
    conversation_id UUID PRIMARY KEY,
    u_id        INTEGER NOT NULL REFERENCES "user"(u_id) ON DELETE CASCADE,
    task_id     UUID REFERENCES reachy_task(task_id) ON DELETE SET NULL,
    language    VARCHAR(10) NOT NULL DEFAULT 'zh-TW',
    started_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at    TIMESTAMPTZ,
    end_reason  VARCHAR(30),
    summary     TEXT,
    mood        VARCHAR(20),
    risk_flag   BOOLEAN NOT NULL DEFAULT FALSE,
    model       VARCHAR(100)
);
CREATE INDEX IF NOT EXISTS idx_conversation_user ON conversation(u_id, started_at DESC);

CREATE TABLE IF NOT EXISTS conversation_turn (
    turn_id         SERIAL PRIMARY KEY,
    conversation_id UUID NOT NULL REFERENCES conversation(conversation_id) ON DELETE CASCADE,
    u_id            INTEGER NOT NULL REFERENCES "user"(u_id) ON DELETE CASCADE,
    role            VARCHAR(10) NOT NULL CHECK (role IN ('patient','reachy')),
    text            TEXT NOT NULL,
    flagged         BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_conversation_turn_conv ON conversation_turn(conversation_id, turn_id);

CREATE TABLE IF NOT EXISTS dose_confirmation (
    confirmation_id UUID PRIMARY KEY,
    u_id        INTEGER NOT NULL REFERENCES "user"(u_id) ON DELETE CASCADE,
    task_id     UUID REFERENCES reachy_task(task_id) ON DELETE SET NULL,
    intk_ids    INTEGER[] NOT NULL,
    previous_status JSONB NOT NULL,
    source      VARCHAR(20) NOT NULL
        CHECK (source IN ('uncertain_detection','unsupported_dose','degraded','auto_record_off','patient_claim')),
    evidence    JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reminded_at TIMESTAMPTZ,
    resolved_at TIMESTAMPTZ,
    resolution  VARCHAR(10) CHECK (resolution IN ('confirmed','denied','expired')),
    resolved_by INTEGER REFERENCES family_contacts(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS monitor_extra_event (
    extra_id       UUID PRIMARY KEY,
    u_id           INTEGER NOT NULL REFERENCES "user"(u_id) ON DELETE CASCADE,
    task_id        UUID REFERENCES reachy_task(task_id) ON DELETE SET NULL,
    detector_band  VARCHAR(20) NOT NULL,
    detector_score REAL NOT NULL,
    observed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Every LINE push added from Phase 2 on goes through this table so a crash
-- between commit and send never loses a message.
CREATE TABLE IF NOT EXISTS notification_outbox (
    outbox_id    BIGSERIAL PRIMARY KEY,
    dedupe_key   VARCHAR(160) NOT NULL UNIQUE,
    u_id         INTEGER NOT NULL REFERENCES "user"(u_id) ON DELETE CASCADE,
    recipient_contact_id INTEGER REFERENCES family_contacts(id) ON DELETE CASCADE,
    recipient_line_id VARCHAR(100) NOT NULL,
    kind         VARCHAR(30) NOT NULL,
    priority     SMALLINT NOT NULL,
    payload      JSONB NOT NULL,
    status       VARCHAR(12) NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued','sending','accepted','failed','cancelled')),
    attempts     INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    line_request_id VARCHAR(64),
    last_error   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    accepted_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_outbox_due ON notification_outbox(priority, next_attempt_at)
    WHERE status IN ('queued','failed');
