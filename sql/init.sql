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
    type VARCHAR(50) NOT NULL, -- 'upcoming_reminder', 'missed_reminder', 'missed_alert', 'emotion_alert'
    message TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
