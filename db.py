import secrets
import sqlite3
from contextlib import contextmanager

from config import DB_PATH


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS symptoms (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                name      TEXT    NOT NULL,
                severity  INTEGER NOT NULL CHECK (severity BETWEEN 1 AND 10),
                notes     TEXT    NOT NULL DEFAULT '',
                timestamp TEXT    NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS medications (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                name      TEXT    NOT NULL,
                dose      TEXT    NOT NULL DEFAULT '',
                notes     TEXT    NOT NULL DEFAULT '',
                timestamp TEXT    NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_profile (
                id          INTEGER PRIMARY KEY,
                name        TEXT    NOT NULL DEFAULT '',
                dob         TEXT    NOT NULL DEFAULT '',
                conditions  TEXT    NOT NULL DEFAULT '',
                medications TEXT    NOT NULL DEFAULT ''
            )
        """)
        # Migrate: add columns if not present
        cols = [row[1] for row in conn.execute("PRAGMA table_info(user_profile)")]
        if "password_hash" not in cols:
            conn.execute(
                "ALTER TABLE user_profile ADD COLUMN password_hash TEXT NOT NULL DEFAULT ''"
            )
        if "username" not in cols:
            conn.execute(
                "ALTER TABLE user_profile ADD COLUMN username TEXT NOT NULL DEFAULT ''"
            )
        if "photo_ext" not in cols:
            conn.execute(
                "ALTER TABLE user_profile ADD COLUMN photo_ext TEXT NOT NULL DEFAULT ''"
            )
        if "share_code" not in cols:
            conn.execute(
                "ALTER TABLE user_profile ADD COLUMN share_code TEXT NOT NULL DEFAULT ''"
            )
        if "email" not in cols:
            conn.execute(
                "ALTER TABLE user_profile ADD COLUMN email TEXT NOT NULL DEFAULT ''"
            )
        # Migrate symptoms: add user_id and end_time columns
        symp_cols = [row[1] for row in conn.execute("PRAGMA table_info(symptoms)")]
        if "user_id" not in symp_cols:
            conn.execute("ALTER TABLE symptoms ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
        if "end_time" not in symp_cols:
            conn.execute("ALTER TABLE symptoms ADD COLUMN end_time TEXT NOT NULL DEFAULT ''")
        # Migrate medications: add user_id column
        med_cols = [row[1] for row in conn.execute("PRAGMA table_info(medications)")]
        if "user_id" not in med_cols:
            conn.execute("ALTER TABLE medications ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
        # Physicians table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS physicians (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL
            )
        """)
        # Physician–patient junction table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS physician_patients (
                physician_id INTEGER NOT NULL REFERENCES physicians(id),
                patient_id   INTEGER NOT NULL REFERENCES user_profile(id),
                PRIMARY KEY (physician_id, patient_id)
            )
        """)
        # Medication schedules and dose tracking
        conn.execute("""
            CREATE TABLE IF NOT EXISTS medication_schedules (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                name       TEXT    NOT NULL,
                dose       TEXT    NOT NULL DEFAULT '',
                notes      TEXT    NOT NULL DEFAULT '',
                frequency  TEXT    NOT NULL,
                start_date TEXT    NOT NULL,
                created_at TEXT    NOT NULL DEFAULT '',
                end_date   TEXT    NOT NULL DEFAULT '',
                active     INTEGER NOT NULL DEFAULT 1
            )
        """)
        sched_cols = [row[1] for row in conn.execute("PRAGMA table_info(medication_schedules)")]
        if "created_at" not in sched_cols:
            conn.execute("ALTER TABLE medication_schedules ADD COLUMN created_at TEXT NOT NULL DEFAULT ''")
        # Backfill historical rows with a best-effort value when created_at is missing.
        conn.execute(
            "UPDATE medication_schedules SET created_at = start_date || ' 00:00:00'"
            " WHERE created_at = ''"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS medication_doses (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                schedule_id    INTEGER NOT NULL REFERENCES medication_schedules(id),
                user_id        INTEGER NOT NULL,
                scheduled_date TEXT    NOT NULL,
                dose_num       INTEGER NOT NULL DEFAULT 1,
                taken_at       TEXT    NOT NULL DEFAULT '',
                status         TEXT    NOT NULL DEFAULT 'pending',
                notes          TEXT    NOT NULL DEFAULT ''
            )
        """)
        # Password reset tokens
        conn.execute("""
            CREATE TABLE IF NOT EXISTS password_reset_tokens (
                token      TEXT    PRIMARY KEY,
                user_id    INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            )
        """)
        # Generate share codes for any patient rows missing one
        for row in conn.execute("SELECT id FROM user_profile WHERE share_code = ''"):
            conn.execute(
                "UPDATE user_profile SET share_code = ? WHERE id = ?",
                (secrets.token_hex(4).upper(), row[0]),
            )
        conn.commit()


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()
