-- F01 local schema. PostgreSQL/Alembic infrastructure belongs to F02.
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO schema_version VALUES (1);
CREATE TABLE IF NOT EXISTS journal (
    revision INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    entity_key TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    payload TEXT NOT NULL,
    sha256 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS journal_lookup ON journal(kind, entity_key, revision);
CREATE TRIGGER IF NOT EXISTS journal_no_update BEFORE UPDATE ON journal
BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END;
CREATE TRIGGER IF NOT EXISTS journal_no_delete BEFORE DELETE ON journal
BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END;
CREATE TABLE IF NOT EXISTS document_bytes (
    revision INTEGER PRIMARY KEY REFERENCES journal(revision),
    content BLOB NOT NULL
);
CREATE TRIGGER IF NOT EXISTS documents_no_update BEFORE UPDATE ON document_bytes
BEGIN SELECT RAISE(ABORT, 'documents are immutable'); END;
CREATE TRIGGER IF NOT EXISTS documents_no_delete BEFORE DELETE ON document_bytes
BEGIN SELECT RAISE(ABORT, 'documents require a separate licensed retention procedure'); END;
CREATE TABLE IF NOT EXISTS raw_bytes (
    object_id TEXT PRIMARY KEY,
    content BLOB NOT NULL
);
CREATE TRIGGER IF NOT EXISTS raw_no_update BEFORE UPDATE ON raw_bytes
BEGIN SELECT RAISE(ABORT, 'raw objects are immutable'); END;
CREATE TRIGGER IF NOT EXISTS raw_delete_requires_tombstone BEFORE DELETE ON raw_bytes
WHEN NOT EXISTS (SELECT 1 FROM journal WHERE kind='tombstone' AND entity_key=OLD.object_id)
BEGIN SELECT RAISE(ABORT, 'deletion requires an audit tombstone'); END;
