import importlib
import sqlite3
import sys
import tempfile
import unittest

from fastapi.testclient import TestClient


class AuthCsrfIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = f"{self.tmp.name}/test.db"

        import config
        import db

        self._config = config
        self._db = db
        self._old_config_db_path = config.DB_PATH
        self._old_db_db_path = db.DB_PATH

        config.DB_PATH = self.db_path
        db.DB_PATH = self.db_path

        sys.modules.pop("main", None)
        main = importlib.import_module("main")
        self.client = TestClient(main.app)

    def tearDown(self):
        self.client.close()
        self._config.DB_PATH = self._old_config_db_path
        self._db.DB_PATH = self._old_db_db_path
        sys.modules.pop("main", None)
        self.tmp.cleanup()

    def _signup(self):
        resp = self.client.post(
            "/signup",
            headers={"origin": "http://testserver"},
            data={
                "username": "alice",
                "email": "alice@example.com",
                "new_password": "password123",
                "confirm_password": "password123",
            },
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)

    def test_api_requires_auth(self):
        # Without any patients in DB, middleware redirects to /signup.
        # Create one account first, then clear auth and verify API returns 401.
        self._signup()
        self.client.cookies.clear()
        resp = self.client.get("/api/medications")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json(), {"error": "unauthorized"})

    def test_api_post_requires_csrf_header(self):
        self._signup()

        without_csrf = self.client.post(
            "/api/medications/schedules",
            headers={"origin": "http://testserver"},
            data={
                "name": "Ibuprofen",
                "dose": "400mg",
                "frequency": "once_daily",
                "start_date": "2000-01-01",
                "notes": "",
            },
        )
        self.assertEqual(without_csrf.status_code, 403)
        self.assertEqual(without_csrf.json(), {"error": "forbidden"})

        csrf = self.client.cookies.get("csrf_token")
        self.assertTrue(csrf)

        with_csrf = self.client.post(
            "/api/medications/schedules",
            headers={"origin": "http://testserver", "x-csrf-token": csrf},
            data={
                "name": "Ibuprofen",
                "dose": "400mg",
                "frequency": "once_daily",
                "start_date": "2000-01-01",
                "notes": "",
            },
        )
        self.assertEqual(with_csrf.status_code, 200)
        payload = with_csrf.json()
        self.assertTrue(payload.get("ok"))
        self.assertEqual(payload["schedule"]["name"], "Ibuprofen")

    def test_dose_timestamp_round_trips_via_utc_storage(self):
        """Dose taken_at is stored as UTC and the API returns it in client-local time.

        With tz_offset=120, client local = UTC - 120 min. A dose logged at the
        current moment should be stored with its UTC equivalent, and the API
        should return it shifted back by 120 minutes (to local time).
        """
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz

        # Simulate what the server middleware does: local = UTC - 120 min.
        OFFSET = 120
        utc_before = _dt.now(_tz.utc).replace(tzinfo=None)
        client_local_before = utc_before - _td(minutes=OFFSET)
        client_local_date = client_local_before.date().isoformat()

        self._signup()
        # +120 means the client's local time is UTC-02:00 (offset = -(−2h) = +120 min).
        self.client.cookies.set("tz_offset", str(OFFSET))

        csrf = self.client.cookies.get("csrf_token")
        self.assertTrue(csrf)

        # Create a schedule using the client-local date so start_date is valid.
        sched_resp = self.client.post(
            "/api/medications/schedules",
            headers={"origin": "http://testserver", "x-csrf-token": csrf},
            data={
                "name": "UTC Regression Med",
                "dose": "1 tab",
                "frequency": "once_daily",
                "start_date": client_local_date,
                "notes": "",
            },
        )
        self.assertEqual(sched_resp.status_code, 200)
        schedule_id = sched_resp.json()["schedule"]["id"]

        # Log a dose at the current moment (taken_time="") so it is always
        # after the schedule creation time and not in the future.
        take_resp = self.client.post(
            "/api/medications/doses/take",
            headers={"origin": "http://testserver", "x-csrf-token": csrf},
            json={
                "schedule_id": schedule_id,
                "scheduled_date": client_local_date,
                "dose_num": 1,
                "taken_time": "",
            },
        )
        self.assertEqual(take_resp.status_code, 200)
        self.assertTrue(take_resp.json().get("ok"))

        # The stored taken_at should be in UTC (≈ utc_before).
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT taken_at FROM medication_doses WHERE schedule_id = ? ORDER BY id DESC LIMIT 1",
                (schedule_id,),
            ).fetchone()
        self.assertIsNotNone(row)
        stored_dt = _dt.strptime(row[0], "%Y-%m-%d %H:%M:%S")
        utc_after = _dt.now(_tz.utc).replace(tzinfo=None)
        # Stored value must be between the UTC snapshots (i.e. stored as UTC).
        self.assertGreaterEqual(stored_dt, utc_before.replace(microsecond=0))
        self.assertLessEqual(stored_dt, utc_after.replace(microsecond=0) + _td(seconds=1))

        # The API must return the timestamp shifted back to client-local time
        # (stored_utc - 120 min).
        api = self.client.get("/api/medications")
        self.assertEqual(api.status_code, 200)
        meds = api.json()["medications"]
        target = next((m for m in meds if m["name"] == "UTC Regression Med"), None)
        self.assertIsNotNone(target)
        api_dt = _dt.strptime(target["timestamp"], "%Y-%m-%d %H:%M:%S")
        # API local = stored UTC - 120 min; difference must be exactly OFFSET minutes.
        diff_minutes = round((stored_dt - api_dt).total_seconds() / 60)
        self.assertEqual(diff_minutes, OFFSET)

    def test_symptom_soft_delete_and_restore(self):
        self._signup()
        csrf = self.client.cookies.get("csrf_token")
        self.assertTrue(csrf)

        created = self.client.post(
            "/api/symptoms",
            headers={
                "origin": "http://testserver",
                "x-csrf-token": csrf,
                "content-type": "application/json",
            },
            json={
                "name": "Headache",
                "severity": 6,
                "notes": "Afternoon",
                "symptom_date": "2026-02-26T14:00",
                "end_date": "",
            },
        )
        self.assertEqual(created.status_code, 200)
        symptom_id = created.json()["symptom"]["id"]

        deleted = self.client.post(
            f"/api/symptoms/{symptom_id}/soft-delete",
            headers={"origin": "http://testserver", "x-csrf-token": csrf},
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertTrue(deleted.json().get("ok"))

        after_delete = self.client.get("/api/symptoms")
        self.assertEqual(after_delete.status_code, 200)
        self.assertFalse(any(s["id"] == symptom_id for s in after_delete.json()["symptoms"]))

        restored = self.client.post(
            f"/api/symptoms/{symptom_id}/restore",
            headers={"origin": "http://testserver", "x-csrf-token": csrf},
        )
        self.assertEqual(restored.status_code, 200)
        self.assertTrue(restored.json().get("ok"))

        after_restore = self.client.get("/api/symptoms")
        self.assertEqual(after_restore.status_code, 200)
        self.assertTrue(any(s["id"] == symptom_id for s in after_restore.json()["symptoms"]))


if __name__ == "__main__":
    unittest.main()
