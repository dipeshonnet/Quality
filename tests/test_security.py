from __future__ import annotations

import asyncio
import io
import os
import subprocess
import sys
import unittest
from dataclasses import replace
from unittest.mock import patch

from fastapi.testclient import TestClient
from openpyxl import load_workbook

import test_quality as baseline
from test_quality import app, quality


class SecurityRegressionTests(unittest.TestCase):
    setUp = baseline.QualityCommandCenterTests.setUp

    def configure_user(self, *, active=True, password=None):
        return quality.save_admin_user(quality.UserIn(
            username="worker", display_name="Worker", password=password,
            roles=["Administrator"], active=active, must_change_password=False), user="admin")

    def test_disabled_user_cannot_login_or_use_existing_session(self):
        self.configure_user(password="old-password-123")
        client = TestClient(app.app)
        self.assertEqual(client.post("/api/login", json={
            "username": "worker", "password": "old-password-123"}).status_code, 200)
        self.assertEqual(client.get("/api/me").status_code, 200)
        self.assertEqual(client.get("/api/admin/users").status_code, 200)
        self.configure_user(active=False)
        with app.db() as con:
            self.assertTrue(con.execute("SELECT 1 FROM user_roles WHERE username='worker' AND role_name='Administrator'").fetchone())
        for route in ("/api/me", "/api/quality/me", "/api/accounts", "/api/admin/users", "/api/results/imports/missing/errors"):
            with self.subTest(route=route):
                self.assertEqual(client.get(route).status_code, 401)
        self.assertEqual(client.post("/api/login", json={
            "username": "worker", "password": "old-password-123"}).status_code, 401)

    def test_password_reset_revokes_sessions_but_profile_edit_preserves_them(self):
        self.configure_user(password="old-password-123")
        client = TestClient(app.app)
        client.post("/api/login", json={"username": "worker", "password": "old-password-123"})
        self.configure_user()
        self.assertEqual(client.get("/api/me").status_code, 200)
        self.configure_user(password="new-password-456")
        self.assertEqual(client.get("/api/me").status_code, 401)
        self.assertEqual(client.post("/api/login", json={
            "username": "worker", "password": "old-password-123"}).status_code, 401)
        self.assertEqual(client.post("/api/login", json={
            "username": "worker", "password": "new-password-456"}).status_code, 200)
        self.assertEqual(client.get("/api/me").status_code, 200)

    def test_reactivation_does_not_restore_old_sessions(self):
        self.configure_user(password="old-password-123")
        token = app.create_session("worker")
        self.configure_user(active=False)
        self.configure_user(active=True)
        with self.assertRaises(app.HTTPException) as error:
            app.current_user(token)
        self.assertEqual(error.exception.status_code, 401)

    def test_login_cannot_create_session_after_concurrent_security_change(self):
        original_verify = app.verify_password
        for change in ("reset", "disable"):
            with self.subTest(change=change):
                self.configure_user(password="old-password-123")

                def change_before_verification(password, salt, digest):
                    self.configure_user(active=change != "disable",
                                        password="new-password-456" if change == "reset" else None)
                    return original_verify(password, salt, digest)

                client = TestClient(app.app)
                with patch.object(app, "verify_password", side_effect=change_before_verification):
                    response = client.post("/api/login", json={
                        "username": "worker", "password": "old-password-123"})
                self.assertEqual(response.status_code, 401)
                self.assertEqual(client.get("/api/me").status_code, 401)
                with app.db() as con:
                    self.assertEqual(con.execute("SELECT COUNT(*) FROM sessions WHERE username='worker'").fetchone()[0], 0)

    def test_session_checks_profile_even_if_not_revoked(self):
        self.configure_user(password="old-password-123")
        token = app.create_session("worker")
        for missing in (False, True):
            with self.subTest(missing=missing):
                with app.db() as con:
                    if missing:
                        con.execute("DELETE FROM user_profiles WHERE username='worker'")
                    else:
                        con.execute("UPDATE user_profiles SET active=0 WHERE username='worker'")
                with self.assertRaises(app.HTTPException) as error:
                    app.current_user(token)
                self.assertEqual(error.exception.status_code, 401)

    def test_auth_cookies_secure_in_hosted_environments(self):
        for environment, origin, secure in (
            ("production", "https://example.com", True),
            ("staging", "https://example.com", True),
            ("development", "https://localhost", True),
            ("development", "http://127.0.0.1:8765", False),
        ):
            with self.subTest(environment=environment, origin=origin), patch.object(
                app, "settings", replace(app.settings, app_env=environment, public_origin=origin)
            ):
                response = TestClient(app.app).post("/api/login", json={
                    "username": "admin", "password": "administrator"})
                self.assertEqual(response.status_code, 200)
                cookies = response.headers.get_list("set-cookie")
                self.assertEqual(len(cookies), 2)
                for cookie in cookies:
                    self.assertEqual("; Secure" in cookie, secure)
                    self.assertIn("SameSite=strict", cookie)
                self.assertIn("HttpOnly", cookies[0])

    def test_first_admin_setup_creates_active_profile_and_roles(self):
        with app.db() as con:
            con.execute("DELETE FROM user_roles")
            con.execute("DELETE FROM user_profiles")
            con.execute("DELETE FROM users")
        client = TestClient(app.app)
        response = client.post("/api/setup", json={"password": "administrator"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(client.get("/api/me").status_code, 200)
        self.assertEqual(client.get("/api/admin/users").status_code, 200)
        self.assertEqual(client.post("/api/setup", json={"password": "administrator"}).status_code, 409)

    def test_private_config_file_and_environment_precedence(self):
        private_config = baseline.TEST_ROOT / "private.env"
        private_config.write_text("PUBLIC_ORIGIN=https://config.example\n", encoding="utf-8")
        environment = dict(os.environ, QCC_ENV_FILE=str(private_config), APP_ENV="development")
        environment.pop("PUBLIC_ORIGIN", None)
        code = "from qcc.config import settings; print(settings.public_origin)"
        for override, expected in ((None, "https://config.example"), ("https://env.example", "https://env.example")):
            with self.subTest(override=override):
                if override:
                    environment["PUBLIC_ORIGIN"] = override
                result = subprocess.run([sys.executable, "-c", code], cwd=baseline.PROJECT_ROOT,
                                        env=environment, capture_output=True, text=True, check=True)
                self.assertEqual(result.stdout.strip(), expected)

    def test_sampling_export_neutralizes_all_untrusted_cells(self):
        detail = {
            "records": [{"row": {"=1+1": "=2+2", "Normal": "plain text"},
                         "selection_sequence": 1, "selected_at": app.iso_now()}],
            "run_id": "RUN-TEST", "status": "COMPLETED", "created_at": app.iso_now(),
            "created_by": "admin", "account_name": "=3+3", "source_filename": "=4+4",
            "source_hash": "hash", "sheet_name": "CSV", "header_row": 1,
            "identifier_column": "=1+1", "associate_column": None,
            "sampling_method": "random", "requested_count": 1, "eligible_count": 1,
            "selected_count": 1, "algorithm_version": "test", "seed": "123",
            "coverage_enabled": False, "coverage_period": None, "coverage_quota": None,
            "period_key": None, "filters": [], "void_reason": None,
        }
        with patch.object(app, "get_run", return_value=detail):
            response = app.export_run("RUN-TEST", user="admin")

        async def consume():
            return b"".join([chunk async for chunk in response.body_iterator])

        workbook = load_workbook(io.BytesIO(asyncio.run(consume())), data_only=False)
        for sheet in workbook:
            for row in sheet:
                for cell in row:
                    self.assertNotEqual(cell.data_type, "f", cell.coordinate)
        results = workbook["Sample Results"]
        self.assertEqual(results["A1"].value, "'=1+1")
        self.assertEqual(results["A2"].value, "'=2+2")
        self.assertEqual(results["D2"].value, "'=3+3")
        self.assertEqual(results["B2"].value, "plain text")
        self.assertEqual(results["F2"].value, 1)


if __name__ == "__main__":
    unittest.main()
