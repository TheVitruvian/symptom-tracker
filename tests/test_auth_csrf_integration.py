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

    def test_medication_timestamp_round_trips_via_utc_storage(self):
        self._signup()
        # Simulate client timezone offset cookie from browser.
        # +120 means local time is UTC-02:00.
        self.client.cookies.set("tz_offset", "120")

        created = self.client.post(
            "/medications",
            headers={"origin": "http://testserver"},
            data={
                "name": "UTC Regression Med",
                "dose": "1 tab",
                "notes": "",
                "med_date": "2026-02-26T10:30",
            },
            follow_redirects=False,
        )
        self.assertEqual(created.status_code, 303)

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT timestamp FROM medications WHERE name = ? ORDER BY id DESC LIMIT 1",
                ("UTC Regression Med",),
            ).fetchone()
        self.assertIsNotNone(row)
        # 10:30 local +120 min offset => 12:30 UTC in storage.
        self.assertEqual(row[0], "2026-02-26 12:30:00")

        api = self.client.get("/api/medications")
        self.assertEqual(api.status_code, 200)
        meds = api.json()["medications"]
        target = next((m for m in meds if m["name"] == "UTC Regression Med"), None)
        self.assertIsNotNone(target)
        # API should convert stored UTC back to request-local wall time.
        self.assertEqual(target["timestamp"], "2026-02-26 10:30:00")

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
