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

CREATE TABLE IF NOT EXISTS intake (
    intk_id           SERIAL PRIMARY KEY,
    u_id              INTEGER NOT NULL REFERENCES "user"(u_id) ON DELETE CASCADE,
    med_id            INTEGER NOT NULL REFERENCES medication(med_id) ON DELETE CASCADE,
    emot_id           INTEGER REFERENCES emotion(emot_id) ON DELETE SET NULL,
    intake_stats      VARCHAR(10) NOT NULL DEFAULT 'pending'
        CHECK (intake_stats IN ('taken','skipped','pending')),
    intake_time_stamp TIMESTAMPTZ DEFAULT NOW(),
    notify_stats      VARCHAR(10) DEFAULT 'pending'
        CHECK (notify_stats IN ('sent','pending','failed')),
    notify_time       TIMESTAMPTZ
);

-- ─────────────────────────────────────────────
-- LOGIN_LOG  (audit trail)
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS login_log (
    log_id        SERIAL PRIMARY KEY,
    u_id          INTEGER NOT NULL REFERENCES "user"(u_id) ON DELETE CASCADE,
    login_session VARCHAR(100),
    login_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_login_log_user ON login_log(u_id, login_at DESC);
CREATE INDEX IF NOT EXISTS idx_emotion_user    ON emotion(u_id, time_stamp DESC);
CREATE INDEX IF NOT EXISTS idx_intake_user     ON intake(u_id, intake_time_stamp DESC);
CREATE INDEX IF NOT EXISTS idx_intake_med      ON intake(med_id);
CREATE INDEX IF NOT EXISTS idx_medication_user ON medication(u_id, is_active);
