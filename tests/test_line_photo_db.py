"""The draft table for photos patients send over LINE (8 Nov 2026)."""

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

STATEMENT = """-- LINE medicine photo (8 Nov 2026): one row per photo a patient sends; the photo itself is never kept.
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
CREATE INDEX IF NOT EXISTS idx_user_line_id ON "user"(line_id);"""


def test_draft_table_is_idempotent_and_cascades():
    sql = (REPO / "sql" / "init.sql").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert sql.count(STATEMENT) == 1
    assert sql.index(STATEMENT) > sql.index('CREATE TABLE IF NOT EXISTS "user"')
    assert sql.count("CREATE TABLE IF NOT EXISTS line_photo_draft") == 1
    assert sql.count("idx_user_line_id") == 1
