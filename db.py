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
        # Migrate symptoms: add user_id column
        symp_cols = [row[1] for row in conn.execute("PRAGMA table_info(symptoms)")]
        if "user_id" not in symp_cols:
            conn.execute("ALTER TABLE symptoms ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
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
