from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(tempfile.mkdtemp(prefix=".test_qsr_", dir=PROJECT_ROOT))
os.environ["QSR_DATA_DIR"] = str(TEST_ROOT / "data")
os.environ["QSR_BACKUP_DIR"] = str(TEST_ROOT / "backups")
os.environ["QSR_LOG_DIR"] = str(TEST_ROOT / "logs")
os.environ["QSR_EXPORT_DIR"] = str(TEST_ROOT / "exports")
os.environ["DATABASE_URL"] = ""
os.environ["DIRECT_DATABASE_URL"] = ""
sys.path.insert(0, str(PROJECT_ROOT))

import app  # noqa: E402
import quality  # noqa: E402


class QualityCommandCenterTests(unittest.TestCase):
    def test_session_expiry_accepts_postgres_and_sqlite_timestamps(self):
        now = datetime.now(timezone.utc)
        for as_text in (False, True):
            for expired in (False, True):
                with self.subTest(as_text=as_text, expired=expired):
                    expiry = now + timedelta(hours=-1 if expired else 1)
                    con = MagicMock()
                    con.execute.return_value.fetchone.return_value = {
                        "username": "admin",
                        "active": 1,
                        "expires_at": expiry.isoformat() if as_text else expiry,
                    }
                    with patch.object(app, "db") as database, patch.object(app, "utcnow", return_value=now):
                        database.return_value.__enter__.return_value = con
                        if expired:
                            with self.assertRaises(app.HTTPException) as error:
                                app.current_user("test-token")
                            self.assertEqual(error.exception.status_code, 401)
                            self.assertEqual(error.exception.detail, "Session expired")
                        else:
                            self.assertEqual(app.current_user("test-token"), "admin")

    def setUp(self):
        with app.db() as con:
            for table in [
                "capa_events", "capas", "audit_defects", "audit_responses", "audit_cases",
                "result_import_rows", "result_imports", "scorecard_items", "scorecard_versions",
                "process_settings", "processes", "sample_records", "sampling_runs", "uploads",
                "account_config", "accounts", "app_settings", "audit_events", "sessions", "user_roles",
                "user_profiles", "users",
            ]:
                con.execute(f"DELETE FROM {table}")
            salt, digest = app.password_hash("administrator")
            con.execute("INSERT INTO users(username,password_salt,password_hash,created_at) VALUES(?,?,?,?)", ("admin", salt, digest, app.iso_now()))
            con.execute("INSERT INTO user_profiles(username,display_name,active,must_change_password,updated_at) VALUES('admin','Administrator',1,0,?)", (app.iso_now(),))
            con.execute("INSERT INTO user_roles(username,role_name) VALUES('admin','Administrator')")

    def seed_process(self, process_type="back_office"):
        now = app.iso_now()
        with app.db() as con:
            aid = con.execute("INSERT INTO accounts(name,active,created_at,updated_at) VALUES('Acme Services',1,?,?)", (now, now)).lastrowid
            con.execute("INSERT INTO account_config(account_id,updated_at) VALUES(?,?)", (aid, now))
            pid = con.execute("INSERT INTO processes(account_id,name,process_type,created_at,updated_at) VALUES(?,?,?,?,?)", (aid, "Order Validation", process_type, now, now)).lastrowid
            con.execute("INSERT INTO process_settings(process_id,updated_at) VALUES(?,?)", (pid, now))
        return aid, pid

    def create_scorecard(self, pid, process_type="back_office"):
        card = quality.create_scorecard(quality.ScorecardIn(process_id=pid, name="Quality Review", passing_score=85, opportunities_per_unit=10), user="admin")
        if process_type == "front_office":
            quality.add_scorecard_item(card["id"], quality.ScorecardItemIn(item_type="question", category="Resolution", name="Resolved correctly", weight=60), user="admin")
            quality.add_scorecard_item(card["id"], quality.ScorecardItemIn(item_type="sla", category="SLA", name="Callback time", weight=40, target=30, lsl=0, usl=30, unit="minutes"), user="admin")
        quality.add_scorecard_item(card["id"], quality.ScorecardItemIn(item_type="defect", category="Accuracy", name="Incorrect data", critical=True, severity="Critical"), user="admin")
        quality.publish_scorecard(card["id"], user="admin")
        return card["id"]

    def test_schema_and_roles_are_initialized(self):
        with app.db() as con:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"processes", "scorecard_versions", "audit_cases", "capas", "result_imports"}.issubset(tables))
        self.assertEqual(quality.roles_for("admin"), ["Administrator"])

    def test_six_sigma_metrics_and_pareto(self):
        aid, pid = self.seed_process()
        now = datetime.now(timezone.utc)
        defect_counts = [0, 1, 2, 0, 1]
        with app.db() as con:
            for index, count in enumerate(defect_counts):
                stamp = (now - timedelta(days=4-index)).replace(microsecond=0).isoformat()
                audit_id = f"AUD-{index}"
                con.execute(
                    """INSERT INTO audit_cases(audit_id,process_id,status,weighted_score,passed,critical_fail,
                       opportunities,defect_count,created_at,created_by,submitted_at,reviewed_at,reviewed_by)
                       VALUES(?,?,'REVIEWED',?,?,?,?,?,?,?, ?,?, 'admin')""",
                    (audit_id, pid, 100-count*10, int(count == 0), int(count == 2), 10, count, stamp, "admin", stamp, stamp),
                )
                for d in range(count):
                    category = "Incorrect Data" if d == 0 else "Missing Information"
                    con.execute("INSERT INTO audit_defects(audit_id,category,name,severity,critical,opportunities,created_at) VALUES(?,?,?,?,?,?,?)", (audit_id, category, category, "Major", 0, 1, stamp))
        data = quality.analytics_payload(aid, pid, None, None)
        self.assertEqual(data["metrics"]["units"], 5)
        self.assertEqual(data["metrics"]["defects"], 4)
        self.assertEqual(data["metrics"]["dpmo"], 80000)
        self.assertEqual(data["metrics"]["yield"], 40.0)
        self.assertEqual(data["stability"], "provisional")
        self.assertEqual(data["pareto"][0]["category"], "Incorrect Data")

    def test_date_range_is_validated_and_includes_the_end_date(self):
        aid, pid = self.seed_process()
        now = datetime.now(timezone.utc)
        with app.db() as con:
            for index, count in enumerate([0, 1, 2, 0, 1]):
                stamp = (now - timedelta(days=4-index)).replace(microsecond=0).isoformat()
                con.execute(
                    """INSERT INTO audit_cases(audit_id,process_id,status,passed,critical_fail,opportunities,
                       defect_count,created_at,created_by,submitted_at,reviewed_at,reviewed_by)
                       VALUES(?,?,'REVIEWED',?,0,10,?,?,'admin',?,?,'admin')""",
                    (f"AUD-RANGE-{index}", pid, int(count == 0), count, stamp, stamp, stamp),
                )
        date_from = (now - timedelta(days=2)).date().isoformat()
        date_to = now.date().isoformat()
        data = quality.analytics_payload(aid, pid, date_from, date_to)
        self.assertEqual(data["metrics"]["units"], 3)
        self.assertEqual(data["metrics"]["defects"], 3)
        end_day = quality.analytics_payload(aid, pid, date_to, date_to)
        self.assertEqual(end_day["metrics"]["units"], 1)
        listed = quality.list_audits(account_id=aid, process_id=pid, date_from=date_to, date_to=date_to, user="admin")
        self.assertEqual([row["audit_id"] for row in listed], ["AUD-RANGE-4"])
        with self.assertRaises(quality.HTTPException) as invalid_order:
            quality.analytics_payload(aid, pid, date_to, date_from)
        self.assertEqual(invalid_order.exception.status_code, 400)
        with self.assertRaises(quality.HTTPException) as invalid_date:
            quality.analytics_payload(aid, pid, "2026-02-30", date_to)
        self.assertEqual(invalid_date.exception.status_code, 400)

    def test_published_scorecard_is_immutable_and_weights_validate(self):
        _, pid = self.seed_process("front_office")
        card_id = self.create_scorecard(pid, "front_office")
        with self.assertRaises(Exception):
            quality.add_scorecard_item(card_id, quality.ScorecardItemIn(item_type="question", name="Late addition", weight=0), user="admin")

    def test_critical_audit_opens_capa_after_review(self):
        _, pid = self.seed_process()
        card_id = self.create_scorecard(pid)
        with app.db() as con:
            item_id = con.execute("SELECT id FROM scorecard_items WHERE scorecard_version_id=? AND item_type='defect'", (card_id,)).fetchone()[0]
            con.execute("""INSERT INTO audit_cases(audit_id,process_id,scorecard_version_id,status,opportunities,created_at,created_by)
                           VALUES('AUD-CRITICAL',?,?,'UNASSIGNED',10,?,'admin')""", (pid, card_id, app.iso_now()))
        score = quality.save_audit("AUD-CRITICAL", quality.AuditSaveIn(defect_item_ids=[item_id]), user="admin")
        self.assertFalse(score["passed"])
        self.assertTrue(score["critical"])
        quality.submit_audit("AUD-CRITICAL", user="admin")
        reviewed = quality.review_audit("AUD-CRITICAL", quality.ReviewIn(decision="APPROVE"), user="admin")
        self.assertIsNotNone(reviewed["capa_id"])
        with app.db() as con:
            self.assertEqual(con.execute("SELECT stage FROM capas WHERE capa_id=?", (reviewed["capa_id"],)).fetchone()[0], "DRAFT")

    def test_sampling_run_creates_audit_cases(self):
        aid, pid = self.seed_process()
        card_id = self.create_scorecard(pid)
        source = TEST_ROOT / "source.csv"
        source.write_text("ID,Associate,Type\nA-1,Alex,Review\nA-2,Blair,Review\n", encoding="utf-8")
        upload_id = "upload-test"
        with app.db() as con:
            con.execute("""INSERT INTO uploads(upload_id,original_name,stored_path,file_type,sha256,size_bytes,created_at,status)
                           VALUES(?,?,?,?,?,?,?,'READY')""", (upload_id, source.name, str(source), ".csv", hashlib.sha256(source.read_bytes()).hexdigest(), source.stat().st_size, app.iso_now()))
        payload = app.RunIn(account_id=aid, process_id=pid, sheet_name="CSV", header_row=1, identifier_column="ID", associate_column="Associate", requested_count=2)
        result = app.generate_run(upload_id, payload, user="admin")
        self.assertEqual(result["selected_count"], 2)
        with app.db() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM audit_cases WHERE process_id=?", (pid,)).fetchone()[0], 2)
            self.assertEqual(con.execute("SELECT process_id FROM sampling_runs WHERE run_id=?", (result["run_id"],)).fetchone()[0], pid)
        self.assertFalse(source.exists())

    def test_historical_result_import_is_atomic(self):
        _, pid = self.seed_process()
        self.create_scorecard(pid)
        mapping = {"case_id": "Case ID", "audit_date": "Audit Date", "associate": "Associate",
                   "defects": "Defects", "opportunities": "Opportunities", "weighted_score": "Score",
                   "passed": "Passed", "critical": "Critical"}
        with app.db() as con:
            con.execute("""INSERT INTO result_imports(import_id,filename,sha256,columns_json,row_count,created_by,created_at)
                           VALUES('IMP-ATOMIC','results.csv','hash','[]',2,'admin',?)""", (app.iso_now(),))
            con.execute("INSERT INTO result_import_rows(import_id,row_number,row_json) VALUES('IMP-ATOMIC',2,?)",
                        (json.dumps({"Case ID": "EXT-1", "Audit Date": app.iso_now(), "Associate": "Alex", "Defects": "",
                                     "Opportunities": "10", "Score": "100", "Passed": "yes", "Critical": "no"}),))
            con.execute("INSERT INTO result_import_rows(import_id,row_number,row_json) VALUES('IMP-ATOMIC',3,?)",
                        (json.dumps({"Case ID": "EXT-2", "Audit Date": app.iso_now(), "Associate": "Blair", "Defects": "Accuracy",
                                     "Opportunities": "0", "Score": "80", "Passed": "no", "Critical": "no"}),))
        result = quality.commit_result_import("IMP-ATOMIC", quality.ImportCommitIn(process_id=pid, mapping=mapping), user="admin")
        self.assertEqual(result["committed"], 0)
        self.assertEqual(result["error_count"], 1)
        with app.db() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM audit_cases WHERE process_id=?", (pid,)).fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT status FROM result_imports WHERE import_id='IMP-ATOMIC'").fetchone()[0], "ERROR")

    def test_capability_and_stable_control_limits(self):
        aid, pid = self.seed_process("front_office")
        card_id = self.create_scorecard(pid, "front_office")
        with app.db() as con:
            item_id = con.execute("SELECT id FROM scorecard_items WHERE scorecard_version_id=? AND item_type='sla'", (card_id,)).fetchone()[0]
            for index in range(20):
                stamp = (datetime.now(timezone.utc) - timedelta(days=19-index)).replace(microsecond=0).isoformat()
                audit_id = f"AUD-SLA-{index}"
                con.execute("""INSERT INTO audit_cases(audit_id,process_id,scorecard_version_id,status,weighted_score,passed,
                               critical_fail,opportunities,defect_count,created_at,created_by,submitted_at,reviewed_at,reviewed_by)
                               VALUES(?,?,?,'REVIEWED',100,1,0,10,0,?,'admin',?,?,'admin')""",
                            (audit_id, pid, card_id, stamp, stamp, stamp))
                con.execute("INSERT INTO audit_responses(audit_id,item_id,numeric_value) VALUES(?,?,?)",
                            (audit_id, item_id, 10 + (index % 5)))
        capability = quality.analytics_capability(pid, item_id, user="admin")
        self.assertEqual(capability["status"], "available")
        self.assertIsNotNone(capability["cp"])
        self.assertIsNotNone(capability["cpk"])
        self.assertEqual(quality.analytics_payload(aid, pid, None, None)["stability"], "stable")

    def test_showcase_toggle_is_idempotent_and_preserves_real_data(self):
        real_account_id, real_process_id = self.seed_process()
        enabled = quality.toggle_showcase_data(quality.ShowcaseToggleIn(enabled=True), user="admin")
        self.assertTrue(enabled["enabled"])
        self.assertEqual(enabled["counts"], {"processes": 2, "scorecards": 2, "audits": 181, "capas": 5})
        with app.db() as con:
            invalid_reviewers = con.execute(
                """SELECT COUNT(*) FROM audit_cases a LEFT JOIN users u ON u.username=a.reviewed_by
                   WHERE a.audit_id LIKE 'DEMO-%' AND a.reviewed_by IS NOT NULL AND u.username IS NULL"""
            ).fetchone()[0]
            self.assertEqual(invalid_reviewers, 0)
        again = quality.toggle_showcase_data(quality.ShowcaseToggleIn(enabled=True), user="admin")
        self.assertEqual(again["account_id"], enabled["account_id"])
        self.assertEqual(quality.analytics_payload(enabled["account_id"], None, None, None)["stability"], "stable")
        disabled = quality.toggle_showcase_data(quality.ShowcaseToggleIn(enabled=False), user="admin")
        self.assertFalse(disabled["enabled"])
        with app.db() as con:
            self.assertIsNotNone(con.execute("SELECT 1 FROM accounts WHERE id=?", (real_account_id,)).fetchone())
            self.assertIsNotNone(con.execute("SELECT 1 FROM processes WHERE id=?", (real_process_id,)).fetchone())
            self.assertIsNone(con.execute("SELECT 1 FROM accounts WHERE id=?", (enabled["account_id"],)).fetchone())
            self.assertEqual(con.execute("SELECT COUNT(*) FROM audit_cases WHERE audit_id LIKE 'DEMO-%'").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
