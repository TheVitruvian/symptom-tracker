"""
Seed script: populates demo data for the Jamie Rivera account.

- Safe to run on a server where the account already exists.
- Clears any existing symptoms/medications/schedules/doses for Jamie,
  then inserts fresh demo data (Hypertension + Type 2 Diabetes).
- Does NOT touch other user accounts.

Usage:
    python3 seed.py
"""

import hashlib
import random
import secrets
import sqlite3
from datetime import date, datetime, timedelta

DB_PATH = "symptoms.db"
USERNAME = "jamie"
PASSWORD = "demo1234"
TODAY = date.today()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def hash_pw(plaintext: str) -> str:
    salt = secrets.token_bytes(32)
    dk = hashlib.pbkdf2_hmac("sha256", plaintext.encode(), salt, 480_000)
    return salt.hex() + ":" + dk.hex()


def ts(d: date, hour: int = 8, minute: int = 0) -> str:
    return f"{d.isoformat()} {hour:02d}:{minute:02d}:00"


def day(offset: int) -> date:
    return TODAY - timedelta(days=offset)


# ---------------------------------------------------------------------------
# Connect and ensure schema
# ---------------------------------------------------------------------------

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

# Ensure user exists; create if missing
row = conn.execute("SELECT id FROM user_profile WHERE username=?", (USERNAME,)).fetchone()
if row:
    uid = row["id"]
    print(f"Found existing account: jamie (id={uid})")
else:
    share_code = secrets.token_hex(4).upper()
    conn.execute(
        "INSERT INTO user_profile (username, password_hash, name, dob, conditions, share_code)"
        " VALUES (?,?,?,?,?,?)",
        (USERNAME, hash_pw(PASSWORD), "Jamie Rivera", "1968-03-14",
         "Hypertension, Type 2 Diabetes", share_code),
    )
    conn.commit()
    uid = conn.execute("SELECT id FROM user_profile WHERE username=?", (USERNAME,)).fetchone()["id"]
    print(f"Created account: jamie (id={uid})")

# Clear existing demo data for this user
for tbl in ["medication_doses", "medication_schedules", "medications", "symptoms"]:
    conn.execute(f"DELETE FROM {tbl} WHERE user_id=?", (uid,))
conn.commit()
print("Cleared existing data for jamie.")

# ---------------------------------------------------------------------------
# Medication schedules
# ---------------------------------------------------------------------------

rng = random.Random(42)  # fixed seed for reproducibility

START = day(59)  # 60 days of history

schedules = [
    # (name, dose, frequency, adherence_rate)
    ("Lisinopril",   "10mg",   "once_daily",  0.85),
    ("Amlodipine",   "5mg",    "once_daily",  0.90),
    ("Metformin",    "500mg",  "twice_daily", 0.75),
    ("Atorvastatin", "20mg",   "once_daily",  0.80),
    ("Aspirin",      "81mg",   "prn",         None),
]

sched_ids = {}
for name, dose, freq, _ in schedules:
    conn.execute(
        "INSERT INTO medication_schedules (user_id, name, dose, frequency, start_date, active)"
        " VALUES (?,?,?,?,?,1)",
        (uid, name, dose, freq, START.isoformat()),
    )
    sched_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    sched_ids[name] = sched_id

conn.commit()
print(f"Inserted {len(schedules)} medication schedules.")

# ---------------------------------------------------------------------------
# Medication doses (44 days: days 59 down to 16)
# ---------------------------------------------------------------------------

doses_inserted = 0
for offset in range(59, 15, -1):  # days 59..16
    d = day(offset)
    for name, dose, freq, rate in schedules:
        if freq == "prn":
            continue
        dpd = {"once_daily": 1, "twice_daily": 2}[freq]
        for dose_num in range(1, dpd + 1):
            taken = rng.random() < rate
            if taken:
                hour = 8 if dose_num == 1 else 20
                taken_at = ts(d, hour, rng.randint(0, 30))
                conn.execute(
                    "INSERT INTO medication_doses"
                    " (schedule_id, user_id, scheduled_date, dose_num, taken_at, status)"
                    " VALUES (?,?,?,?,?,'taken')",
                    (sched_ids[name], uid, d.isoformat(), dose_num, taken_at),
                )
            else:
                conn.execute(
                    "INSERT INTO medication_doses"
                    " (schedule_id, user_id, scheduled_date, dose_num, status)"
                    " VALUES (?,?,?,?,'missed')",
                    (sched_ids[name], uid, d.isoformat(), dose_num),
                )
            doses_inserted += 1

conn.commit()
print(f"Inserted {doses_inserted} scheduled dose records.")

# ---------------------------------------------------------------------------
# PRN Aspirin — 8 ad-hoc uses spread over history
# ---------------------------------------------------------------------------

prn_days = sorted(rng.sample(range(5, 59), 8))
for offset in prn_days:
    d = day(offset)
    taken_at = ts(d, rng.randint(9, 18), rng.randint(0, 59))
    conn.execute(
        "INSERT INTO medication_doses"
        " (schedule_id, user_id, scheduled_date, dose_num, taken_at, status)"
        " VALUES (?,?,?,?,?,'taken')",
        (sched_ids["Aspirin"], uid, d.isoformat(), 1, taken_at),
    )

conn.commit()
print(f"Inserted 8 PRN Aspirin doses.")

# ---------------------------------------------------------------------------
# Ad-hoc medication log entries (visible in the Log tab)
# ---------------------------------------------------------------------------

adhoc_meds = [
    ("Lisinopril",   "10mg"),
    ("Amlodipine",   "5mg"),
    ("Metformin",    "500mg"),
    ("Atorvastatin", "20mg"),
    ("Aspirin",      "81mg"),
]
adhoc_inserted = 0
for offset in range(59, 28, -4):
    d = day(offset)
    name, dose = rng.choice(adhoc_meds)
    conn.execute(
        "INSERT INTO medications (user_id, name, dose, timestamp) VALUES (?,?,?,?)",
        (uid, name, dose, ts(d, 8, 0)),
    )
    adhoc_inserted += 1

conn.commit()
print(f"Inserted {adhoc_inserted} ad-hoc medication log entries.")

# ---------------------------------------------------------------------------
# Symptoms — 60 days, clinically correlated
# ---------------------------------------------------------------------------
#
# BP symptoms (Headache, Dizziness, Palpitations, Ankle swelling):
#   driven by a shared bp_pressure signal
# DM symptoms (Fatigue, Increased thirst, Nocturia, Blurry vision):
#   driven by a shared glucose signal
#
# Both signals oscillate with some noise.

symptoms_inserted = 0

def clamp(v, lo=1, hi=10):
    return max(lo, min(hi, int(round(v))))

for offset in range(59, -1, -1):
    d = day(offset)

    # Underlying disease activity signals (1-10 scale)
    bp_signal    = 5 + 3 * (0.5 - rng.random()) + 1.5 * (offset % 14 > 7)
    glucose_signal = 5 + 2.5 * (0.5 - rng.random()) + 1.2 * (offset % 10 > 5)

    # --- BP-related symptoms ---
    if rng.random() < 0.70:
        sev = clamp(bp_signal + rng.gauss(0, 1))
        conn.execute(
            "INSERT INTO symptoms (user_id, name, severity, timestamp) VALUES (?,?,?,?)",
            (uid, "Headache", sev, ts(d, rng.randint(7, 14), rng.randint(0, 59))),
        )
        symptoms_inserted += 1

    if rng.random() < 0.55:
        sev = clamp(bp_signal * 0.85 + rng.gauss(0, 1.2))
        conn.execute(
            "INSERT INTO symptoms (user_id, name, severity, timestamp) VALUES (?,?,?,?)",
            (uid, "Dizziness", sev, ts(d, rng.randint(8, 16), rng.randint(0, 59))),
        )
        symptoms_inserted += 1

    if rng.random() < 0.40:
        sev = clamp(bp_signal * 0.75 + rng.gauss(0, 1.5))
        conn.execute(
            "INSERT INTO symptoms (user_id, name, severity, timestamp) VALUES (?,?,?,?)",
            (uid, "Palpitations", sev, ts(d, rng.randint(10, 20), rng.randint(0, 59))),
        )
        symptoms_inserted += 1

    if rng.random() < 0.35:
        sev = clamp(bp_signal * 0.70 + rng.gauss(0, 1.5))
        conn.execute(
            "INSERT INTO symptoms (user_id, name, severity, timestamp) VALUES (?,?,?,?)",
            (uid, "Ankle swelling", sev, ts(d, rng.randint(16, 21), rng.randint(0, 59))),
        )
        symptoms_inserted += 1

    # --- DM-related symptoms ---
    if rng.random() < 0.75:
        sev = clamp(glucose_signal + rng.gauss(0, 1))
        conn.execute(
            "INSERT INTO symptoms (user_id, name, severity, timestamp) VALUES (?,?,?,?)",
            (uid, "Fatigue", sev, ts(d, rng.randint(9, 18), rng.randint(0, 59))),
        )
        symptoms_inserted += 1

    if rng.random() < 0.60:
        sev = clamp(glucose_signal * 0.90 + rng.gauss(0, 1.2))
        conn.execute(
            "INSERT INTO symptoms (user_id, name, severity, timestamp) VALUES (?,?,?,?)",
            (uid, "Increased thirst", sev, ts(d, rng.randint(10, 17), rng.randint(0, 59))),
        )
        symptoms_inserted += 1

    if rng.random() < 0.50:
        sev = clamp(glucose_signal * 0.80 + rng.gauss(0, 1.3))
        conn.execute(
            "INSERT INTO symptoms (user_id, name, severity, timestamp) VALUES (?,?,?,?)",
            (uid, "Nocturia", sev, ts(d, rng.randint(0, 4), rng.randint(0, 59))),
        )
        symptoms_inserted += 1

    if rng.random() < 0.30:
        sev = clamp(glucose_signal * 0.70 + rng.gauss(0, 1.5))
        conn.execute(
            "INSERT INTO symptoms (user_id, name, severity, timestamp) VALUES (?,?,?,?)",
            (uid, "Blurry vision", sev, ts(d, rng.randint(8, 19), rng.randint(0, 59))),
        )
        symptoms_inserted += 1

conn.commit()
print(f"Inserted {symptoms_inserted} symptom records.")

conn.close()
print("\nDone. Log in with username=jamie password=demo1234")
