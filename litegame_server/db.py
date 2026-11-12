import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL,
    username_key TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    revision INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX sessions_user ON sessions(user_id);
CREATE TABLE records (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('local', 'nes')),
    key TEXT NOT NULL,
    value TEXT,
    value_bytes INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL,
    deleted INTEGER NOT NULL CHECK(deleted IN (0, 1)),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, kind, key)
);
CREATE INDEX records_changes ON records(user_id, version);
CREATE TABLE login_attempts (
    scope TEXT PRIMARY KEY,
    started_at INTEGER NOT NULL,
    attempts INTEGER NOT NULL
);
"""


@contextmanager
def connect(path: Path):
    conn = sqlite3.connect(path, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    try:
        yield conn
    finally:
        if conn.in_transaction:
            conn.rollback()
        conn.close()


def initialize(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    with connect(path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("BEGIN EXCLUSIVE")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError("Database is newer than this application; do not downgrade")
        if version == 0:
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        # Add future migrations here transactionally before increasing user_version.
        conn.commit()
