from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from openpyxl import Workbook, load_workbook
from pydantic import BaseModel, Field

from app import (
    APP_VERSION,
    ConfigIn,
    DB_PATH,
    audit,
    current_user,
    db,
    formula_safe,
    iso_now,
    password_hash,
)
from qcc.analytics import sigma_level as _sigma_level, summarize_quality
from qcc.database import uses_postgres
from qcc import access
from qcc.account_schema import statements, initialize_process, sampling_config

router = APIRouter()

ROLES = {
    "Administrator": "Full configuration and system access",
    "QA Auditor": "Create samples and complete assigned audits",
    "QA Reviewer": "Review audits and manage CAPA evidence",
    "Operations Manager": "View analytics, reports, and approve CAPA",
}
CAPA_STAGES = [
    "DRAFT",
    "CONTAINMENT",
    "ROOT_CAUSE",
    "ACTION_PLAN",
    "IMPLEMENTATION",
    "EFFECTIVENESS_REVIEW",
    "CLOSED",
]


def _columns(con, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}


def init_quality_db() -> None:
    """Idempotent v2 schema migration; legacy rows remain untouched."""
    if uses_postgres():
        return
    with db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS roles (
                name TEXT PRIMARY KEY,
                description TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_roles (
                username TEXT NOT NULL,
                role_name TEXT NOT NULL,
                PRIMARY KEY(username, role_name),
                FOREIGN KEY(role_name) REFERENCES roles(name)
            );
            CREATE TABLE IF NOT EXISTS user_profiles (
                username TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                must_change_password INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS processes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                process_type TEXT NOT NULL CHECK(process_type IN ('front_office','back_office')),
                timezone TEXT NOT NULL DEFAULT 'local',
                target_yield REAL NOT NULL DEFAULT 95,
                target_sigma REAL NOT NULL DEFAULT 3.0,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(account_id, name COLLATE NOCASE),
                FOREIGN KEY(account_id) REFERENCES accounts(id)
            );
            CREATE TABLE IF NOT EXISTS process_settings (
                process_id INTEGER PRIMARY KEY,
                sampling_frequency TEXT NOT NULL DEFAULT 'weekly',
                sampling_count INTEGER NOT NULL DEFAULT 10,
                assignment_mode TEXT NOT NULL DEFAULT 'unassigned',
                subgroup_mode TEXT NOT NULL DEFAULT 'day',
                baseline_subgroups INTEGER NOT NULL DEFAULT 20,
                critical_capa_enabled INTEGER NOT NULL DEFAULT 1,
                capa_due_days INTEGER NOT NULL DEFAULT 14,
                import_mapping_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL,
                FOREIGN KEY(process_id) REFERENCES processes(id)
            );
            CREATE TABLE IF NOT EXISTS scorecard_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                process_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                version INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'DRAFT' CHECK(status IN ('DRAFT','PUBLISHED','ARCHIVED')),
                passing_score REAL NOT NULL DEFAULT 85,
                opportunities_per_unit INTEGER NOT NULL DEFAULT 1,
                critical_fail_override INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL,
                published_at TEXT,
                UNIQUE(process_id, version),
                FOREIGN KEY(process_id) REFERENCES processes(id)
            );
            CREATE TABLE IF NOT EXISTS scorecard_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scorecard_version_id INTEGER NOT NULL,
                item_type TEXT NOT NULL CHECK(item_type IN ('question','defect','sla')),
                category TEXT NOT NULL DEFAULT 'General',
                name TEXT NOT NULL,
                weight REAL NOT NULL DEFAULT 0,
                severity TEXT NOT NULL DEFAULT 'Minor',
                critical INTEGER NOT NULL DEFAULT 0,
                opportunity_count INTEGER NOT NULL DEFAULT 1,
                target REAL,
                lsl REAL,
                usl REAL,
                unit TEXT,
                sort_order INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY(scorecard_version_id) REFERENCES scorecard_versions(id)
            );
            CREATE TABLE IF NOT EXISTS audit_cases (
                audit_id TEXT PRIMARY KEY,
                process_id INTEGER NOT NULL,
                sample_record_id INTEGER,
                external_case_id TEXT,
                scorecard_version_id INTEGER,
                assigned_to TEXT,
                status TEXT NOT NULL DEFAULT 'UNASSIGNED',
                associate TEXT,
                source_json TEXT NOT NULL DEFAULT '{}',
                started_at TEXT,
                submitted_at TEXT,
                reviewed_at TEXT,
                reviewed_by TEXT,
                rejection_reason TEXT,
                weighted_score REAL,
                passed INTEGER,
                critical_fail INTEGER NOT NULL DEFAULT 0,
                opportunities INTEGER NOT NULL DEFAULT 1,
                defect_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL,
                void_reason TEXT,
                FOREIGN KEY(process_id) REFERENCES processes(id),
                FOREIGN KEY(sample_record_id) REFERENCES sample_records(id),
                FOREIGN KEY(scorecard_version_id) REFERENCES scorecard_versions(id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS ux_audit_external_case
                ON audit_cases(process_id, external_case_id) WHERE external_case_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS ix_audits_process_status ON audit_cases(process_id, status, submitted_at);
            CREATE TABLE IF NOT EXISTS audit_responses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                audit_id TEXT NOT NULL,
                item_id INTEGER NOT NULL,
                passed INTEGER,
                score REAL,
                numeric_value REAL,
                notes TEXT,
                UNIQUE(audit_id, item_id),
                FOREIGN KEY(audit_id) REFERENCES audit_cases(audit_id),
                FOREIGN KEY(item_id) REFERENCES scorecard_items(id)
            );
            CREATE TABLE IF NOT EXISTS audit_defects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                audit_id TEXT NOT NULL,
                item_id INTEGER,
                category TEXT NOT NULL,
                name TEXT NOT NULL,
                severity TEXT NOT NULL,
                critical INTEGER NOT NULL DEFAULT 0,
                opportunities INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                FOREIGN KEY(audit_id) REFERENCES audit_cases(audit_id)
            );
            CREATE INDEX IF NOT EXISTS ix_defects_audit ON audit_defects(audit_id);
            CREATE TABLE IF NOT EXISTS result_imports (
                import_id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PREVIEW',
                columns_json TEXT NOT NULL,
                mapping_json TEXT NOT NULL DEFAULT '{}',
                row_count INTEGER NOT NULL,
                error_count INTEGER NOT NULL DEFAULT 0,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                committed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS result_import_rows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                import_id TEXT NOT NULL,
                row_number INTEGER NOT NULL,
                row_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'READY',
                error TEXT,
                audit_id TEXT,
                FOREIGN KEY(import_id) REFERENCES result_imports(import_id)
            );
            CREATE TABLE IF NOT EXISTS capas (
                capa_id TEXT PRIMARY KEY,
                process_id INTEGER NOT NULL,
                audit_id TEXT,
                title TEXT NOT NULL,
                priority TEXT NOT NULL DEFAULT 'MEDIUM',
                stage TEXT NOT NULL DEFAULT 'DRAFT',
                owner TEXT,
                due_date TEXT,
                containment TEXT NOT NULL DEFAULT '',
                root_cause TEXT NOT NULL DEFAULT '',
                action_plan TEXT NOT NULL DEFAULT '',
                implementation_notes TEXT NOT NULL DEFAULT '',
                effectiveness TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                closed_at TEXT,
                FOREIGN KEY(process_id) REFERENCES processes(id),
                FOREIGN KEY(audit_id) REFERENCES audit_cases(audit_id)
            );
            CREATE INDEX IF NOT EXISTS ix_capa_process_stage ON capas(process_id, stage, due_date);
            CREATE TABLE IF NOT EXISTS capa_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                capa_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                from_stage TEXT,
                to_stage TEXT,
                detail_json TEXT NOT NULL DEFAULT '{}',
                username TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(capa_id) REFERENCES capas(capa_id)
            );
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                updated_by TEXT NOT NULL
            );
            """
        )
        if "process_id" not in _columns(con, "sampling_runs"):
            con.execute("ALTER TABLE sampling_runs ADD COLUMN process_id INTEGER")
        for name, description in ROLES.items():
            con.execute("INSERT OR IGNORE INTO roles(name,description) VALUES(?,?)", (name, description))
        users = con.execute("SELECT username FROM users").fetchall()
        for row in users:
            username = row["username"]
            con.execute(
                "INSERT OR IGNORE INTO user_profiles(username,display_name,updated_at) VALUES(?,?,?)",
                (username, username.title(), iso_now()),
            )
        if users and not con.execute("SELECT 1 FROM schema_migrations WHERE version=2").fetchone():
            con.execute(
                "INSERT OR IGNORE INTO user_roles(username,role_name) VALUES(?,?)",
                (users[0]["username"], "Administrator"),
            )
        accounts = con.execute("SELECT id,name FROM accounts").fetchall()
        for account in accounts:
            existing = con.execute("SELECT id FROM processes WHERE account_id=? ORDER BY id LIMIT 1", (account["id"],)).fetchone()
            if not existing:
                now = iso_now()
                cur = con.execute(
                    "INSERT INTO processes(account_id,name,process_type,created_at,updated_at) VALUES(?,?,?,?,?)",
                    (account["id"], "General Service Process", "back_office", now, now),
                )
                process_id = cur.lastrowid
                con.execute("INSERT INTO process_settings(process_id,updated_at) VALUES(?,?)", (process_id, now))
            else:
                process_id = existing["id"]
            con.execute("UPDATE sampling_runs SET process_id=? WHERE account_id=? AND process_id IS NULL", (process_id, account["id"]))
        con.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(2,?)", (iso_now(),))
        if not con.execute("SELECT 1 FROM schema_migrations WHERE version=3").fetchone():
            for statement in statements():
                con.execute(statement)
            con.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(3,?)", (iso_now(),))



def roles_for(username: str) -> list[str]:
    with db() as con:
        return access.roles(con, username)


def require_roles(*allowed: str):
    def dependency(user: str = Depends(current_user)) -> str:
        if not set(roles_for(user)).intersection(allowed):
            raise HTTPException(403, "You do not have permission to perform this action")
        return user
    return dependency


def user_context(user: str) -> dict[str, Any]:
    with db() as con:
        p = con.execute("SELECT display_name,active FROM user_profiles WHERE username=?", (user,)).fetchone()
        global_roles = ["Administrator"] if access.is_admin(con, user) else []
        account_roles = access.assignments(con, user)
    return {"username": user, "display_name": p["display_name"] if p else user,
            "roles": global_roles, "account_roles": account_roles,
            "assignment_required": not global_roles and not account_roles}



def _id(prefix: str) -> str:
    return f"{prefix}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2).upper()}"


class ProcessIn(BaseModel):
    account_id: int
    name: str = Field(min_length=1, max_length=120)
    process_type: str
    timezone: str = "local"
    target_yield: float = Field(default=95, ge=0, le=100)
    target_sigma: float = Field(default=3, ge=0, le=6)
    active: bool = True


class ProcessSettingsIn(BaseModel):
    sampling_frequency: str = "weekly"
    sampling_count: int = Field(default=10, ge=1, le=100000)
    assignment_mode: str = "unassigned"
    subgroup_mode: str = "day"
    baseline_subgroups: int = Field(default=20, ge=5, le=100)
    critical_capa_enabled: bool = True
    capa_due_days: int = Field(default=14, ge=1, le=365)


class AccountRoleIn(BaseModel):
    account_id: int
    roles: list[str] = Field(min_length=1)


class UserIn(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    display_name: str = Field(min_length=1, max_length=120)
    password: str | None = Field(default=None, min_length=8, max_length=200)
    roles: list[str] = Field(default_factory=list)
    account_roles: list[AccountRoleIn] = Field(default_factory=list)
    active: bool = True
    must_change_password: bool = True


class ScorecardIn(BaseModel):
    process_id: int
    name: str = Field(min_length=1, max_length=160)
    passing_score: float = Field(default=85, ge=0, le=100)
    opportunities_per_unit: int = Field(default=1, ge=1, le=10000)
    critical_fail_override: bool = True


class ScorecardItemIn(BaseModel):
    item_type: str
    category: str = "General"
    name: str = Field(min_length=1, max_length=240)
    weight: float = Field(default=0, ge=0, le=100)
    severity: str = "Minor"
    critical: bool = False
    opportunity_count: int = Field(default=1, ge=1, le=10000)
    target: float | None = None
    lsl: float | None = None
    usl: float | None = None
    unit: str | None = None
    sort_order: int = 0


class AuditResponseIn(BaseModel):
    item_id: int
    passed: bool | None = None
    score: float | None = Field(default=None, ge=0, le=100)
    numeric_value: float | None = None
    notes: str | None = None


class AuditSaveIn(BaseModel):
    assigned_to: str | None = None
    responses: list[AuditResponseIn] = Field(default_factory=list)
    defect_item_ids: list[int] = Field(default_factory=list)


class ReviewIn(BaseModel):
    decision: str
    reason: str | None = None


class ImportCommitIn(BaseModel):
    process_id: int
    scorecard_version_id: int | None = None
    mapping: dict[str, str]


class CapaIn(BaseModel):
    process_id: int
    audit_id: str | None = None
    title: str = Field(min_length=3, max_length=240)
    priority: str = "MEDIUM"
    owner: str | None = None
    due_date: str | None = None


class CapaUpdateIn(BaseModel):
    owner: str | None = None
    due_date: str | None = None
    containment: str = ""
    root_cause: str = ""
    action_plan: str = ""
    implementation_notes: str = ""
    effectiveness: str = ""


class TransitionIn(BaseModel):
    to_stage: str
    comment: str = ""


class ShowcaseToggleIn(BaseModel):
    enabled: bool


@router.get("/api/quality/me")
def quality_me(user: str = Depends(current_user)):
    return user_context(user)


@router.get("/api/admin/overview")
def admin_overview(user: str = Depends(require_roles("Administrator"))):
    with db() as con:
        counts = {
            "users": con.execute("SELECT COUNT(*) FROM user_profiles WHERE active=1").fetchone()[0],
            "processes": con.execute("SELECT COUNT(*) FROM processes WHERE active=1").fetchone()[0],
            "scorecards": con.execute("SELECT COUNT(*) FROM scorecard_versions WHERE status='PUBLISHED'").fetchone()[0],
            "imports": con.execute("SELECT COUNT(*) FROM result_imports").fetchone()[0],
        }
        settings = {r["key"]: json.loads(r["value_json"]) for r in con.execute("SELECT * FROM app_settings").fetchall()}
    return {"counts": counts, "roles": ROLES, "settings": settings, "version": APP_VERSION}


SHOWCASE_SETTING_KEY = "showcase_demo_data"


def _showcase_state(con) -> dict[str, Any]:
    row = con.execute("SELECT value_json FROM app_settings WHERE key=?", (SHOWCASE_SETTING_KEY,)).fetchone()
    if not row:
        return {"enabled": False}
    try:
        return json.loads(row["value_json"])
    except (TypeError, json.JSONDecodeError):
        return {"enabled": False}


def _showcase_status(con) -> dict[str, Any]:
    state = _showcase_state(con)
    account_id = state.get("account_id")
    enabled = bool(state.get("enabled") and account_id and con.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone())
    return {
        "enabled": enabled,
        "account_id": account_id if enabled else None,
        "account_name": state.get("account_name") if enabled else None,
        "created_at": state.get("created_at") if enabled else None,
        "counts": {
            "processes": len(state.get("process_ids", [])) if enabled else 0,
            "scorecards": len(state.get("scorecard_ids", [])) if enabled else 0,
            "audits": len(state.get("audit_ids", [])) if enabled else 0,
            "capas": len(state.get("capa_ids", [])) if enabled else 0,
        },
    }


def _delete_showcase_data(con, state: dict[str, Any]) -> None:
    audit_ids = [str(x) for x in state.get("audit_ids", [])]
    capa_ids = [str(x) for x in state.get("capa_ids", [])]
    scorecard_ids = [int(x) for x in state.get("scorecard_ids", [])]
    process_ids = [int(x) for x in state.get("process_ids", [])]
    account_id = state.get("account_id")
    if capa_ids:
        marks = ",".join("?" for _ in capa_ids)
        con.execute(f"DELETE FROM capa_events WHERE capa_id IN ({marks})", capa_ids)
        con.execute(f"DELETE FROM capas WHERE capa_id IN ({marks})", capa_ids)
    if audit_ids:
        marks = ",".join("?" for _ in audit_ids)
        con.execute(f"DELETE FROM audit_defects WHERE audit_id IN ({marks})", audit_ids)
        con.execute(f"DELETE FROM audit_responses WHERE audit_id IN ({marks})", audit_ids)
        con.execute(f"DELETE FROM audit_cases WHERE audit_id IN ({marks})", audit_ids)
    if scorecard_ids:
        marks = ",".join("?" for _ in scorecard_ids)
        con.execute(f"DELETE FROM scorecard_items WHERE scorecard_version_id IN ({marks})", scorecard_ids)
        con.execute(f"DELETE FROM scorecard_versions WHERE id IN ({marks})", scorecard_ids)
    if process_ids:
        marks = ",".join("?" for _ in process_ids)
        con.execute(f"DELETE FROM process_sampling_config WHERE process_id IN ({marks})", process_ids)
        con.execute(f"DELETE FROM process_settings WHERE process_id IN ({marks})", process_ids)
        con.execute(f"DELETE FROM processes WHERE id IN ({marks})", process_ids)
    if account_id:
        account = con.execute("SELECT name FROM accounts WHERE id=?", (account_id,)).fetchone()
        if account and account["name"] == state.get("account_name"):
            con.execute("DELETE FROM account_user_roles WHERE account_id=?", (account_id,))
            con.execute("DELETE FROM account_config WHERE account_id=?", (account_id,))
            con.execute("DELETE FROM accounts WHERE id=?", (account_id,))


def _seed_showcase_data(con, user: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    now_text = now.isoformat()
    account_name = "Quality Showcase Services (Demo)"
    if con.execute("SELECT 1 FROM accounts WHERE name=?", (account_name,)).fetchone():
        raise HTTPException(409, "The showcase account name is already in use. Rename or archive it before enabling demo data.")
    account_id = con.execute("INSERT INTO accounts(name,active,created_at,updated_at) VALUES(?,1,?,?)",
                             (account_name, now_text, now_text)).lastrowid
    con.execute("""INSERT INTO account_config(account_id,coverage_enabled,coverage_period,audits_per_associate,
                   exclude_previously_sampled,case_insensitive_ids,updated_at) VALUES(?,1,'week',3,1,1,?)""",
                (account_id, now_text))

    process_ids: list[int] = []
    for name, process_type, target_yield in [("Order Validation", "back_office", 96), ("Customer Retention", "front_office", 92)]:
        pid = con.execute("""INSERT INTO processes(account_id,name,process_type,target_yield,target_sigma,active,created_at,updated_at)
                              VALUES(?,?,?,?,3.5,1,?,?)""", (account_id, name, process_type, target_yield, now_text, now_text)).lastrowid
        process_ids.append(pid)
        initialize_process(con, pid, now_text)
        con.execute("""INSERT INTO process_settings(process_id,sampling_frequency,sampling_count,assignment_mode,subgroup_mode,
                       baseline_subgroups,critical_capa_enabled,capa_due_days,updated_at) VALUES(?,'weekly',25,'round_robin','day',20,1,7,?)""",
                    (pid, now_text))
    back_pid, front_pid = process_ids

    def add_card(process_id: int, name: str, opportunities: int, items: list[dict[str, Any]]) -> tuple[int, dict[str, int]]:
        sid = con.execute("""INSERT INTO scorecard_versions(process_id,name,version,status,passing_score,opportunities_per_unit,
                           critical_fail_override,created_at,created_by,published_at) VALUES(?,?,1,'PUBLISHED',85,?,1,?,?,?)""",
                          (process_id, name, opportunities, now_text, user, now_text)).lastrowid
        item_ids: dict[str, int] = {}
        for order, item in enumerate(items, 1):
            iid = con.execute("""INSERT INTO scorecard_items(scorecard_version_id,item_type,category,name,weight,severity,
                               critical,opportunity_count,target,lsl,usl,unit,sort_order) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                              (sid, item["type"], item["category"], item["name"], item.get("weight", 0),
                               item.get("severity", "Minor"), int(item.get("critical", False)), item.get("opportunities", 1),
                               item.get("target"), item.get("lsl"), item.get("usl"), item.get("unit"), order)).lastrowid
            item_ids[item["name"]] = iid
        return sid, item_ids

    back_card, back_items = add_card(back_pid, "Back Office Quality Review", 10, [
        {"type": "defect", "category": "Accuracy", "name": "Incorrect Data Entry", "severity": "Major"},
        {"type": "defect", "category": "Completeness", "name": "Missing / Incomplete Information", "severity": "Critical", "critical": True},
        {"type": "defect", "category": "Documentation", "name": "Documentation Error", "severity": "Minor"},
        {"type": "defect", "category": "Duplication", "name": "Duplicate Order", "severity": "Major"},
        {"type": "defect", "category": "Service", "name": "Wrong Service / Product", "severity": "Critical", "critical": True},
    ])
    front_card, front_items = add_card(front_pid, "Front Office Service Quality", 12, [
        {"type": "question", "category": "Greeting", "name": "Professional greeting", "weight": 20},
        {"type": "question", "category": "Discovery", "name": "Confirmed customer need", "weight": 20},
        {"type": "question", "category": "Resolution", "name": "Resolved accurately", "weight": 20},
        {"type": "question", "category": "Closure", "name": "Clear next steps", "weight": 10},
        {"type": "sla", "category": "SLA", "name": "Average handle time", "weight": 15, "target": 12, "lsl": 5, "usl": 15, "unit": "minutes"},
        {"type": "sla", "category": "SLA", "name": "After-call work", "weight": 15, "target": 3, "lsl": 0, "usl": 5, "unit": "minutes"},
        {"type": "defect", "category": "Compliance", "name": "Required disclosure missed", "severity": "Critical", "critical": True},
    ])
    scorecard_ids = [back_card, front_card]
    audit_ids: list[str] = []
    associates = ["Avery Patel", "Jordan Lee", "Morgan Diaz", "Taylor Smith", "Casey Nguyen"]
    defect_names = ["Incorrect Data Entry", "Missing / Incomplete Information", "Documentation Error", "Duplicate Order", "Wrong Service / Product"]
    defect_days = {4, 10, 15, 21, 27}
    for day_index in range(30):
        stamp = (now - timedelta(days=29-day_index)).replace(hour=10, minute=30)
        for unit in range(5):
            aid = f"DEMO-AUD-B-{day_index:02d}-{unit:02d}"
            audit_ids.append(aid)
            is_defect = day_index in defect_days and unit == 0
            defect_name = defect_names[sorted(defect_days).index(day_index)] if is_defect else None
            critical = defect_name in {"Missing / Incomplete Information", "Wrong Service / Product"}
            con.execute("""INSERT INTO audit_cases(audit_id,process_id,external_case_id,scorecard_version_id,status,assigned_to,
                           associate,source_json,started_at,submitted_at,reviewed_at,reviewed_by,weighted_score,passed,critical_fail,
                           opportunities,defect_count,created_at,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (aid, back_pid, f"SHOW-B-{day_index:02d}-{unit:02d}", back_card, "REVIEWED", "demo.auditor",
                         associates[(day_index + unit) % len(associates)], json.dumps({"showcase_demo": True}), stamp.isoformat(),
                         stamp.isoformat(), stamp.isoformat(), user, 90 if is_defect else 100, int(not critical),
                         int(critical), 10, int(is_defect), stamp.isoformat(), user))
            if is_defect and defect_name:
                item_id = back_items[defect_name]
                item = con.execute("SELECT category,severity,critical FROM scorecard_items WHERE id=?", (item_id,)).fetchone()
                con.execute("""INSERT INTO audit_defects(audit_id,item_id,category,name,severity,critical,opportunities,created_at)
                               VALUES(?,?,?,?,?,?,1,?)""", (aid, item_id, item["category"], defect_name, item["severity"], item["critical"], stamp.isoformat()))

    front_scorable = [front_items[name] for name in ["Professional greeting", "Confirmed customer need", "Resolved accurately",
                                                     "Clear next steps", "Average handle time", "After-call work"]]
    for day_index in range(30):
        stamp = (now - timedelta(days=29-day_index)).replace(hour=14, minute=15)
        aid = f"DEMO-AUD-F-{day_index:02d}"
        audit_ids.append(aid)
        critical = day_index in {8, 24}
        con.execute("""INSERT INTO audit_cases(audit_id,process_id,external_case_id,scorecard_version_id,status,assigned_to,
                       associate,source_json,started_at,submitted_at,reviewed_at,reviewed_by,weighted_score,passed,critical_fail,
                       opportunities,defect_count,created_at,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (aid, front_pid, f"SHOW-F-{day_index:02d}", front_card, "REVIEWED", "demo.auditor",
                     associates[day_index % len(associates)], json.dumps({"showcase_demo": True}), stamp.isoformat(), stamp.isoformat(),
                     stamp.isoformat(), user, 82 if critical else 96, int(not critical), int(critical), 12, int(critical),
                     stamp.isoformat(), user))
        for item_id in front_scorable[:4]:
            con.execute("INSERT INTO audit_responses(audit_id,item_id,passed,score,notes) VALUES(?,?,1,100,'Synthetic showcase response')", (aid, item_id))
        con.execute("INSERT INTO audit_responses(audit_id,item_id,passed,score,numeric_value,notes) VALUES(?,?,1,100,?,'Synthetic showcase SLA')",
                    (aid, front_items["Average handle time"], 9 + (day_index % 6) * 0.55))
        con.execute("INSERT INTO audit_responses(audit_id,item_id,passed,score,numeric_value,notes) VALUES(?,?,1,100,?,'Synthetic showcase SLA')",
                    (aid, front_items["After-call work"], 1.5 + (day_index % 5) * 0.35))
        if critical:
            item_id = front_items["Required disclosure missed"]
            con.execute("""INSERT INTO audit_defects(audit_id,item_id,category,name,severity,critical,opportunities,created_at)
                           VALUES(?,?,'Compliance','Required disclosure missed','Critical',1,1,?)""", (aid, item_id, stamp.isoformat()))

    submitted_id = "DEMO-AUD-REVIEW-001"
    audit_ids.append(submitted_id)
    con.execute("""INSERT INTO audit_cases(audit_id,process_id,external_case_id,scorecard_version_id,status,assigned_to,associate,
                   source_json,started_at,submitted_at,weighted_score,passed,critical_fail,opportunities,defect_count,created_at,created_by)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (submitted_id, front_pid, "SHOW-REVIEW-001", front_card, "SUBMITTED", "demo.auditor", associates[0],
                 json.dumps({"showcase_demo": True}), now_text, now_text, 88, 1, 0, 12, 0, now_text, user))

    capa_specs = [
        ("High defects: Missing / Incomplete Information", "CRITICAL", "DRAFT", 1),
        ("SLA breaches exceeding threshold", "CRITICAL", "CONTAINMENT", 2),
        ("Incorrect Data Entry — root cause analysis", "HIGH", "ROOT_CAUSE", 3),
        ("Callback not within SLA target", "HIGH", "ACTION_PLAN", 4),
        ("Documentation errors in order notes", "MEDIUM", "IMPLEMENTATION", 5),
    ]
    capa_ids: list[str] = []
    for index, (title, priority, stage, due_days) in enumerate(capa_specs, 1):
        cid = f"DEMO-CAPA-{index:03d}"
        capa_ids.append(cid)
        pid = back_pid if index in {1, 3, 5} else front_pid
        con.execute("""INSERT INTO capas(capa_id,process_id,title,priority,stage,owner,due_date,containment,root_cause,
                       action_plan,implementation_notes,effectiveness,created_at,created_by,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (cid, pid, title, priority, stage, "Quality Improvement Team", (now.date()+timedelta(days=due_days)).isoformat(),
                     "Customer impact contained.", "Process variation under investigation.", "Targeted coaching and control update.",
                     "Implementation evidence pending.", "", now_text, user, now_text))
        con.execute("INSERT INTO capa_events(capa_id,event_type,to_stage,detail_json,username,created_at) VALUES(?,'SHOWCASE_CREATED',?,'{}',?,?)",
                    (cid, stage, user, now_text))

    state = {"enabled": True, "account_id": account_id, "account_name": account_name, "process_ids": process_ids,
             "scorecard_ids": scorecard_ids, "audit_ids": audit_ids, "capa_ids": capa_ids, "created_at": now_text}
    con.execute("""INSERT INTO app_settings(key,value_json,updated_at,updated_by) VALUES(?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at,updated_by=excluded.updated_by""",
                (SHOWCASE_SETTING_KEY, json.dumps(state), now_text, user))
    return state


@router.get("/api/admin/showcase-data")
def get_showcase_data(user: str = Depends(require_roles("Administrator"))):
    with db() as con:
        return _showcase_status(con)


@router.put("/api/admin/showcase-data")
def toggle_showcase_data(payload: ShowcaseToggleIn, user: str = Depends(require_roles("Administrator"))):
    with db() as con:
        state = _showcase_state(con)
        current = _showcase_status(con)["enabled"]
        if payload.enabled and not current:
            if state.get("account_id"):
                _delete_showcase_data(con, state)
            _seed_showcase_data(con, user)
        elif not payload.enabled and current:
            _delete_showcase_data(con, state)
            disabled = {"enabled": False, "removed_at": iso_now()}
            con.execute("UPDATE app_settings SET value_json=?,updated_at=?,updated_by=? WHERE key=?",
                        (json.dumps(disabled), iso_now(), user, SHOWCASE_SETTING_KEY))
        result = _showcase_status(con)
    audit("SHOWCASE_DATA_ENABLED" if result["enabled"] else "SHOWCASE_DATA_DISABLED", user, "setting", SHOWCASE_SETTING_KEY, result["counts"])
    return result


@router.get("/api/admin/users")
def admin_users(user: str = Depends(require_roles("Administrator"))):
    with db() as con:
        rows = con.execute(
            """SELECT u.username,p.display_name,p.active,p.must_change_password,u.created_at
               FROM users u JOIN user_profiles p ON p.username=u.username ORDER BY p.display_name COLLATE NOCASE"""
        ).fetchall()
        users = []
        for r in rows:
            grants = access.assignments(con, r["username"])
            global_roles = ["Administrator"] if access.is_admin(con, r["username"]) else []
            users.append(dict(r) | {"roles": global_roles, "account_roles": grants,
                "assignment_required": not global_roles and not grants,
                "legacy_roles": [x[0] for x in con.execute("SELECT role_name FROM legacy_user_roles WHERE username=?", (r["username"],)).fetchall()]})
        return users


@router.post("/api/admin/users")
def save_admin_user(payload: UserIn, user: str = Depends(require_roles("Administrator"))):
    invalid = set(payload.roles) - {"Administrator"}
    if invalid:
        raise HTTPException(400, f"Unknown roles: {', '.join(sorted(invalid))}")
    username = re.sub(r"\s+", "", payload.username.strip()).casefold()
    with db() as con:
        for grant in payload.account_roles:
            if set(grant.roles) - access.ACCOUNT_ROLES:
                raise HTTPException(400, "Account roles must be QA Auditor, QA Reviewer, or Operations Manager")
            if not con.execute("SELECT 1 FROM accounts WHERE id=?", (grant.account_id,)).fetchone():
                raise HTTPException(400, "Account not found")
        if access.is_admin(con, username) and (not payload.active or "Administrator" not in payload.roles):
            remaining = con.execute("SELECT COUNT(*) FROM user_roles r JOIN user_profiles p ON p.username=r.username WHERE r.role_name='Administrator' AND p.active=1 AND r.username<>?", (username,)).fetchone()[0]
            if not remaining:
                raise HTTPException(409, "Keep at least one active Administrator")
        exists = con.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone()
        if not exists:
            if not payload.password:
                raise HTTPException(400, "Password is required for a new user")
            salt, digest = password_hash(payload.password)
            con.execute("INSERT INTO users(username,password_salt,password_hash,created_at) VALUES(?,?,?,?)", (username, salt, digest, iso_now()))
        elif payload.password:
            salt, digest = password_hash(payload.password)
            con.execute("UPDATE users SET password_salt=?,password_hash=? WHERE username=?", (salt, digest, username))
        con.execute(
            """INSERT INTO user_profiles(username,display_name,active,must_change_password,updated_at) VALUES(?,?,?,?,?)
               ON CONFLICT(username) DO UPDATE SET display_name=excluded.display_name,active=excluded.active,
               must_change_password=excluded.must_change_password,updated_at=excluded.updated_at""",
            (username, payload.display_name.strip(), int(payload.active), int(payload.must_change_password), iso_now()),
        )
        if not payload.active or payload.password:
            con.execute("DELETE FROM sessions WHERE username=?", (username,))
        con.execute("DELETE FROM user_roles WHERE username=?", (username,))
        for role in set(payload.roles):
            con.execute("INSERT INTO user_roles(username,role_name) VALUES(?,?)", (username, role))
        con.execute("DELETE FROM account_user_roles WHERE username=?", (username,))
        for aid, role in {(g.account_id, r) for g in payload.account_roles for r in g.roles}:
            con.execute("INSERT INTO account_user_roles(username,account_id,role_name) VALUES(?,?,?)", (username, aid, role))

    audit("USER_CONFIGURED", user, "user", username, {"roles": payload.roles, "account_roles": [g.model_dump() for g in payload.account_roles], "active": payload.active})
    return {"ok": True, "username": username}


@router.get("/api/admin/processes")
def list_processes(account_id: int | None = None, user: str = Depends(current_user), include_archived: bool = False):
    params: list[Any] = []
    where = "WHERE 1=1" if include_archived else "WHERE p.active=1 AND a.active=1"
    if account_id:
        where += " AND p.account_id=?"
        params.append(account_id)
    with db() as con:
        clause, scope_params = access.scope(con, user, "p.account_id", account_id)
        where += " AND " + clause
        params += scope_params
        rows = con.execute(
            f"""SELECT p.*,a.active account_active,a.name account_name,ps.sampling_frequency,ps.sampling_count,ps.assignment_mode,
                       ps.subgroup_mode,ps.baseline_subgroups,ps.critical_capa_enabled,ps.capa_due_days,
                       ps.import_mapping_json
                FROM processes p JOIN accounts a ON a.id=p.account_id
                LEFT JOIN process_settings ps ON ps.process_id=p.id {where}
                ORDER BY a.name COLLATE NOCASE,p.name COLLATE NOCASE""",
            params,
        ).fetchall()
        return [dict(r) | {"sampling_config": sampling_config(con, r["id"])} for r in rows]


@router.post("/api/admin/processes")
def create_process(payload: ProcessIn, user: str = Depends(require_roles("Administrator"))):
    if payload.process_type not in {"front_office", "back_office"}:
        raise HTTPException(400, "Process type must be front_office or back_office")
    now = iso_now()
    try:
        with db() as con:
            if not con.execute("SELECT 1 FROM accounts WHERE id=? AND active=1", (payload.account_id,)).fetchone():
                raise HTTPException(404, "Account not found")
            cur = con.execute(
                """INSERT INTO processes(account_id,name,process_type,timezone,target_yield,target_sigma,active,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (payload.account_id, payload.name.strip(), payload.process_type, payload.timezone, payload.target_yield, payload.target_sigma, int(payload.active), now, now),
            )
            pid = cur.lastrowid
            con.execute("INSERT INTO process_settings(process_id,updated_at) VALUES(?,?)", (pid, now))
            initialize_process(con, pid, now)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(409, "A process with this name already exists for the account") from exc
    audit("PROCESS_CREATED", user, "process", str(pid), payload.model_dump())
    return {"id": pid, "ok": True}


@router.put("/api/admin/processes/{process_id}")
def update_process(process_id: int, payload: ProcessIn, user: str = Depends(require_roles("Administrator"))):
    if payload.process_type not in {"front_office", "back_office"}:
        raise HTTPException(400, "Invalid process type")
    with db() as con:
        access.process(con, user, process_id, active=True)
        if not con.execute("SELECT 1 FROM processes WHERE id=?", (process_id,)).fetchone():
            raise HTTPException(404, "Process not found")
        current = access.process(con, user, process_id)
        if current["account_id"] != payload.account_id:
            raise HTTPException(409, "A process cannot be moved to another account")
        if bool(current["active"]) != payload.active:
            raise HTTPException(409, "Use the archive or restore action")
        con.execute(
            """UPDATE processes SET account_id=?,name=?,process_type=?,timezone=?,target_yield=?,target_sigma=?,active=?,updated_at=? WHERE id=?""",
            (payload.account_id, payload.name.strip(), payload.process_type, payload.timezone, payload.target_yield, payload.target_sigma, int(payload.active), iso_now(), process_id),
        )
    audit("PROCESS_CHANGED", user, "process", str(process_id), payload.model_dump())
    return {"ok": True}


@router.put("/api/admin/processes/{process_id}/settings")
def update_process_settings(process_id: int, payload: ProcessSettingsIn, user: str = Depends(require_roles("Administrator"))):
    if payload.sampling_frequency not in {"daily", "weekly", "monthly", "manual"} or payload.subgroup_mode not in {"day", "week", "month"}:
        raise HTTPException(400, "Invalid frequency or subgroup mode")
    with db() as con:
        access.process(con, user, process_id, active=True)
        if not con.execute("SELECT 1 FROM processes WHERE id=?", (process_id,)).fetchone():
            raise HTTPException(404, "Process not found")
        con.execute(
            """INSERT INTO process_settings(process_id,sampling_frequency,sampling_count,assignment_mode,subgroup_mode,
                   baseline_subgroups,critical_capa_enabled,capa_due_days,updated_at) VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(process_id) DO UPDATE SET sampling_frequency=excluded.sampling_frequency,
                   sampling_count=excluded.sampling_count,assignment_mode=excluded.assignment_mode,
                   subgroup_mode=excluded.subgroup_mode,baseline_subgroups=excluded.baseline_subgroups,
                   critical_capa_enabled=excluded.critical_capa_enabled,capa_due_days=excluded.capa_due_days,updated_at=excluded.updated_at""",
            (process_id, payload.sampling_frequency, payload.sampling_count, payload.assignment_mode, payload.subgroup_mode,
             payload.baseline_subgroups, int(payload.critical_capa_enabled), payload.capa_due_days, iso_now()),
        )
    audit("PROCESS_SETTINGS_CHANGED", user, "process", str(process_id), payload.model_dump())
    return {"ok": True}


@router.get("/api/scorecards")
def list_scorecards(process_id: int | None = None, user: str = Depends(current_user), account_id: int | None = None):
    where, params = "", []
    if process_id:
        where, params = "WHERE s.process_id=?", [process_id]
    with db() as con:
        clause, scoped = access.scope(con, user, "p.account_id", account_id, process_id)
        where = (where + " AND " if where else "WHERE ") + clause
        params += scoped
        if account_id is not None:
            where += " AND p.account_id=?"
            params.append(account_id)
        cards = [dict(r) for r in con.execute(
            f"""SELECT s.*,p.account_id,p.active process_active,a.active account_active,p.name process_name,p.process_type,a.name account_name,
                        (SELECT COUNT(*) FROM scorecard_items i WHERE i.scorecard_version_id=s.id AND i.active=1) item_count
                 FROM scorecard_versions s JOIN processes p ON p.id=s.process_id JOIN accounts a ON a.id=p.account_id
                 {where} ORDER BY a.name,p.name,s.version DESC""", params).fetchall()]
        for card in cards:
            card["items"] = [dict(x) for x in con.execute("SELECT * FROM scorecard_items WHERE scorecard_version_id=? AND active=1 ORDER BY sort_order,id", (card["id"],)).fetchall()]
    return cards


@router.post("/api/admin/scorecards")
def create_scorecard(payload: ScorecardIn, user: str = Depends(require_roles("Administrator"))):
    with db() as con:
        lock_scorecard_process(con, payload.process_id)
        access.process(con, user, payload.process_id, active=True)
        if not con.execute("SELECT 1 FROM processes WHERE id=?", (payload.process_id,)).fetchone():
            raise HTTPException(404, "Process not found")
        version = con.execute("SELECT COALESCE(MAX(version),0)+1 FROM scorecard_versions WHERE process_id=?", (payload.process_id,)).fetchone()[0]
        cur = con.execute(
            """INSERT INTO scorecard_versions(process_id,name,version,passing_score,opportunities_per_unit,
               critical_fail_override,created_at,created_by) VALUES(?,?,?,?,?,?,?,?)""",
            (payload.process_id, payload.name.strip(), version, payload.passing_score, payload.opportunities_per_unit,
             int(payload.critical_fail_override), iso_now(), user),
        )
        sid = cur.lastrowid
    audit("SCORECARD_CREATED", user, "scorecard", str(sid), {"version": version})
    return {"id": sid, "version": version, "ok": True}


@router.post("/api/admin/scorecards/{scorecard_id}/items")
def add_scorecard_item(scorecard_id: int, payload: ScorecardItemIn, user: str = Depends(require_roles("Administrator"))):
    if payload.item_type not in {"question", "defect", "sla"}:
        raise HTTPException(400, "Invalid item type")
    with db() as con:
        editable_scorecard(con, user, scorecard_id)
        cur = con.execute(
            """INSERT INTO scorecard_items(scorecard_version_id,item_type,category,name,weight,severity,critical,
               opportunity_count,target,lsl,usl,unit,sort_order) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (scorecard_id, payload.item_type, payload.category.strip(), payload.name.strip(), payload.weight,
             payload.severity, int(payload.critical), payload.opportunity_count, payload.target, payload.lsl,
             payload.usl, payload.unit, payload.sort_order),
        )
    return {"id": cur.lastrowid, "ok": True}


@router.post("/api/admin/scorecards/{scorecard_id}/publish")
def publish_scorecard(scorecard_id: int, user: str = Depends(require_roles("Administrator"))):
    with db() as con:
        card = editable_scorecard(con, user, scorecard_id)
        items = con.execute("SELECT item_type,weight FROM scorecard_items WHERE scorecard_version_id=? AND active=1", (scorecard_id,)).fetchall()
        if not items:
            raise HTTPException(400, "Add at least one scorecard item before publishing")
        ptype = con.execute("SELECT process_type FROM processes WHERE id=?", (card["process_id"],)).fetchone()[0]
        if ptype == "front_office":
            weight = sum(float(r["weight"] or 0) for r in items if r["item_type"] in {"question", "sla"})
            if not math.isclose(weight, 100, abs_tol=0.01):
                raise HTTPException(400, f"Front-office question and SLA weights must total 100; current total is {weight:g}")
        con.execute("UPDATE scorecard_versions SET status='ARCHIVED' WHERE process_id=? AND status='PUBLISHED'", (card["process_id"],))
        con.execute("UPDATE scorecard_versions SET status='PUBLISHED',published_at=? WHERE id=?", (iso_now(), scorecard_id))
    audit("SCORECARD_PUBLISHED", user, "scorecard", str(scorecard_id))
    return {"ok": True}


def published_scorecard(con, process_id: int):
    return con.execute("SELECT * FROM scorecard_versions WHERE process_id=? AND status='PUBLISHED' ORDER BY version DESC LIMIT 1", (process_id,)).fetchone()


@router.get("/api/audits")
def list_audits(process_id: int | None = None, status: str | None = None, assigned_to: str | None = None,
                limit: int = 250, user: str = Depends(current_user), account_id: int | None = None,
                date_from: str | None = None, date_to: str | None = None):
    where, params = [], []
    if account_id:
        where.append("p.account_id=?"); params.append(account_id)
    if process_id:
        where.append("c.process_id=?"); params.append(process_id)
    if status:
        where.append("c.status=?"); params.append(status.upper())
    if assigned_to:
        where.append("c.assigned_to=?"); params.append(assigned_to)
    extra, date_params = _range_filters("COALESCE(c.reviewed_at,c.submitted_at,c.created_at)", date_from, date_to)
    sql_where = "WHERE " + " AND ".join(where) if where else "WHERE 1=1"
    sql_where += extra; params += date_params
    params.append(max(1, min(limit, 1000)))
    with db() as con:
        clause, scoped = access.scope(con, user, "p.account_id", account_id, process_id)
        sql_where += " AND " + clause
        params[-1:-1] = scoped
        rows = con.execute(
            f"""SELECT c.*,p.account_id,p.active process_active,p.name process_name,p.process_type,a.name account_name,s.name scorecard_name,s.version scorecard_version
                 FROM audit_cases c JOIN processes p ON p.id=c.process_id JOIN accounts a ON a.id=p.account_id
                 LEFT JOIN scorecard_versions s ON s.id=c.scorecard_version_id {sql_where}
                 ORDER BY CASE c.status WHEN 'SUBMITTED' THEN 0 WHEN 'IN_PROGRESS' THEN 1 WHEN 'ASSIGNED' THEN 2 ELSE 3 END,
                 c.created_at DESC LIMIT ?""", params).fetchall()
    return [dict(r) for r in rows]


@router.get("/api/audits/{audit_id}")
def get_audit(audit_id: str, user: str = Depends(current_user)):
    with db() as con:
        access.record(con, user, "audit_cases", "audit_id", audit_id, ())
        row = con.execute(
            """SELECT c.*,p.account_id,p.active process_active,p.name process_name,p.process_type,a.name account_name,s.name scorecard_name,s.version scorecard_version,
                      s.passing_score,s.opportunities_per_unit,s.critical_fail_override
               FROM audit_cases c JOIN processes p ON p.id=c.process_id JOIN accounts a ON a.id=p.account_id
               LEFT JOIN scorecard_versions s ON s.id=c.scorecard_version_id WHERE c.audit_id=?""", (audit_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Audit not found")
        out = dict(row)
        out["source"] = json.loads(out.pop("source_json") or "{}")
        out["items"] = [dict(x) for x in con.execute("SELECT * FROM scorecard_items WHERE scorecard_version_id=? AND active=1 ORDER BY sort_order,id", (row["scorecard_version_id"],)).fetchall()] if row["scorecard_version_id"] else []
        out["responses"] = [dict(x) for x in con.execute("SELECT * FROM audit_responses WHERE audit_id=?", (audit_id,)).fetchall()]
        out["defects"] = [dict(x) for x in con.execute("SELECT * FROM audit_defects WHERE audit_id=?", (audit_id,)).fetchall()]
    return out


def _score_audit(con, audit, responses: list[AuditResponseIn], defect_ids: list[int]) -> dict[str, Any]:
    items = [dict(x) for x in con.execute("SELECT * FROM scorecard_items WHERE scorecard_version_id=? AND active=1", (audit["scorecard_version_id"],)).fetchall()]
    by_id = {i["id"]: i for i in items}
    response_map = {r.item_id: r for r in responses}
    selected_defects = [by_id[i] for i in defect_ids if i in by_id and by_id[i]["item_type"] == "defect"]
    card = con.execute("SELECT * FROM scorecard_versions WHERE id=?", (audit["scorecard_version_id"],)).fetchone()
    process_type = con.execute("SELECT process_type FROM processes WHERE id=?", (audit["process_id"],)).fetchone()[0]
    opportunities = int(card["opportunities_per_unit"] or 1)
    critical = any(bool(i["critical"]) for i in selected_defects)
    if process_type == "front_office":
        weighted_score = 0.0
        for item in items:
            if item["item_type"] not in {"question", "sla"}:
                continue
            resp = response_map.get(item["id"])
            raw_score = 0.0
            if resp:
                if resp.score is not None:
                    raw_score = resp.score
                elif resp.passed is not None:
                    raw_score = 100.0 if resp.passed else 0.0
                elif resp.numeric_value is not None and item["target"] is not None:
                    raw_score = 100.0 if resp.numeric_value <= item["target"] else 0.0
            weighted_score += float(item["weight"] or 0) * raw_score / 100.0
    else:
        weighted_score = max(0.0, 100.0 * (1 - len(selected_defects) / max(1, opportunities)))
    passed = weighted_score >= float(card["passing_score"] or 0)
    if bool(card["critical_fail_override"]) and critical:
        passed = False
    return {"weighted_score": round(weighted_score, 2), "passed": passed, "critical": critical,
            "opportunities": opportunities, "defects": selected_defects}


@router.put("/api/audits/{audit_id}")
def save_audit(audit_id: str, payload: AuditSaveIn, user: str = Depends(require_roles("Administrator", "QA Auditor"))):
    with db() as con:
        access.record(con, user, "audit_cases", "audit_id", audit_id, ('QA Auditor',))
        case = con.execute("SELECT * FROM audit_cases WHERE audit_id=?", (audit_id,)).fetchone()
        if not case:
            raise HTTPException(404, "Audit not found")
        if case["status"] not in {"UNASSIGNED", "ASSIGNED", "IN_PROGRESS", "REJECTED"}:
            raise HTTPException(409, "This audit can no longer be edited")
        if not case["scorecard_version_id"]:
            raise HTTPException(400, "Assign a published scorecard before scoring")
        proc = access.process(con, user, case["process_id"], ("QA Auditor",))
        access.validate_assignee(con, payload.assigned_to, proc["account_id"], ("QA Auditor",))
        valid_items = {r[0] for r in con.execute("SELECT id FROM scorecard_items WHERE scorecard_version_id=? AND active=1", (case["scorecard_version_id"],)).fetchall()}
        if ({r.item_id for r in payload.responses} | set(payload.defect_item_ids)) - valid_items:
            raise HTTPException(400, "Items must belong to this audit's scorecard")
        score = _score_audit(con, case, payload.responses, payload.defect_item_ids)
        con.execute("DELETE FROM audit_responses WHERE audit_id=?", (audit_id,))
        for r in payload.responses:
            con.execute("INSERT INTO audit_responses(audit_id,item_id,passed,score,numeric_value,notes) VALUES(?,?,?,?,?,?)",
                        (audit_id, r.item_id, None if r.passed is None else int(r.passed), r.score, r.numeric_value, r.notes))
        con.execute("DELETE FROM audit_defects WHERE audit_id=?", (audit_id,))
        for item in score["defects"]:
            con.execute("""INSERT INTO audit_defects(audit_id,item_id,category,name,severity,critical,opportunities,created_at)
                           VALUES(?,?,?,?,?,?,?,?)""",
                        (audit_id, item["id"], item["category"], item["name"], item["severity"], item["critical"], item["opportunity_count"], iso_now()))
        status = "IN_PROGRESS"
        con.execute("""UPDATE audit_cases SET assigned_to=COALESCE(?,assigned_to),status=?,started_at=COALESCE(started_at,?),
                       weighted_score=?,passed=?,critical_fail=?,opportunities=?,defect_count=? WHERE audit_id=?""",
                    (payload.assigned_to or user, status, iso_now(), score["weighted_score"], int(score["passed"]),
                     int(score["critical"]), score["opportunities"], len(score["defects"]), audit_id))
    audit("AUDIT_SAVED", user, "audit", audit_id, {k: v for k, v in score.items() if k != "defects"})
    return {"ok": True, **{k: v for k, v in score.items() if k != "defects"}}


@router.post("/api/audits/{audit_id}/submit")
def submit_audit(audit_id: str, user: str = Depends(require_roles("Administrator", "QA Auditor"))):
    with db() as con:
        access.record(con, user, "audit_cases", "audit_id", audit_id, ('QA Auditor',))
        row = con.execute("SELECT status,weighted_score FROM audit_cases WHERE audit_id=?", (audit_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Audit not found")
        if row["status"] not in {"IN_PROGRESS", "REJECTED"} or row["weighted_score"] is None:
            raise HTTPException(409, "Save a completed scorecard before submitting")
        con.execute("UPDATE audit_cases SET status='SUBMITTED',submitted_at=? WHERE audit_id=?", (iso_now(), audit_id))
    audit("AUDIT_SUBMITTED", user, "audit", audit_id)
    return {"ok": True}


def _create_capa(con, process_id: int, audit_id: str | None, title: str, priority: str, owner: str | None, due_date: str | None, user: str) -> str:
    cid = _id("CAPA")
    now = iso_now()
    con.execute("""INSERT INTO capas(capa_id,process_id,audit_id,title,priority,owner,due_date,created_at,created_by,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""", (cid, process_id, audit_id, title, priority, owner, due_date, now, user, now))
    con.execute("INSERT INTO capa_events(capa_id,event_type,to_stage,username,created_at) VALUES(?,'CREATED','DRAFT',?,?)", (cid, user, now))
    return cid


@router.post("/api/audits/{audit_id}/review")
def review_audit(audit_id: str, payload: ReviewIn, user: str = Depends(require_roles("Administrator", "QA Reviewer", "Operations Manager"))):
    decision = payload.decision.upper()
    if decision not in {"APPROVE", "REJECT"}:
        raise HTTPException(400, "Decision must be APPROVE or REJECT")
    capa_id = None
    with db() as con:
        access.record(con, user, "audit_cases", "audit_id", audit_id, ('QA Reviewer', 'Operations Manager'))
        row = con.execute("SELECT * FROM audit_cases WHERE audit_id=?", (audit_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Audit not found")
        if row["status"] != "SUBMITTED":
            raise HTTPException(409, "Only submitted audits can be reviewed")
        if decision == "REJECT":
            if not (payload.reason or "").strip():
                raise HTTPException(400, "A rejection reason is required")
            con.execute("UPDATE audit_cases SET status='REJECTED',rejection_reason=?,reviewed_by=? WHERE audit_id=?", (payload.reason.strip(), user, audit_id))
        else:
            con.execute("UPDATE audit_cases SET status='REVIEWED',reviewed_at=?,reviewed_by=?,rejection_reason=NULL WHERE audit_id=?", (iso_now(), user, audit_id))
            settings = con.execute("SELECT * FROM process_settings WHERE process_id=?", (row["process_id"],)).fetchone()
            existing = con.execute("SELECT capa_id FROM capas WHERE audit_id=?", (audit_id,)).fetchone()
            if row["critical_fail"] and settings and settings["critical_capa_enabled"] and not existing:
                due = (datetime.now().date() + timedelta(days=int(settings["capa_due_days"]))).isoformat()
                capa_id = _create_capa(con, row["process_id"], audit_id, f"Critical finding in {audit_id}", "CRITICAL", None, due, user)
    audit(f"AUDIT_{'REVIEWED' if decision == 'APPROVE' else 'REJECTED'}", user, "audit", audit_id, {"reason": payload.reason, "capa_id": capa_id})
    return {"ok": True, "capa_id": capa_id}


def _read_result_file(file: UploadFile, content: bytes) -> tuple[list[str], list[dict[str, str]]]:
    ext = Path(file.filename or "").suffix.casefold()
    if ext == ".csv":
        text = content.decode("utf-8-sig", "replace")
        reader = csv.DictReader(io.StringIO(text))
        headers = list(reader.fieldnames or [])
        return headers, [{str(k): str(v or "").strip() for k, v in row.items()} for row in reader]
    if ext != ".xlsx":
        raise HTTPException(400, "Historical results must be XLSX or CSV")
    wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    try:
        rows = wb.active.iter_rows(values_only=True)
        first = next(rows, None)
        if first is None:
            return [], []
        headers = [str(v or f"Column {i+1}").strip() for i, v in enumerate(first)]
        records = [
            {h: str(row[i] if i < len(row) and row[i] is not None else "").strip() for i, h in enumerate(headers)}
            for row in rows
        ]
        return headers, records
    finally:
        wb.close()


@router.post("/api/results/imports")
async def preview_result_import(file: UploadFile = File(...), user: str = Depends(require_roles("Administrator", "QA Auditor", "QA Reviewer"))):
    content = await file.read(25 * 1024 * 1024 + 1)
    if len(content) > 25 * 1024 * 1024:
        raise HTTPException(400, "File exceeds the 25 MB limit")
    headers, rows = _read_result_file(file, content)
    if not headers or not rows:
        raise HTTPException(400, "File contains no result rows")
    iid = _id("IMP")
    lookup = {h.casefold(): h for h in headers}
    aliases = {
        "case_id": ["case id", "case_id", "audit id", "work order", "identifier"],
        "audit_date": ["audit date", "date", "reviewed at"],
        "associate": ["associate", "agent", "employee", "emp id"],
        "defects": ["defects", "defect", "defect category"],
        "opportunities": ["opportunities", "opportunity count"],
        "weighted_score": ["weighted score", "quality score", "score"],
        "passed": ["passed", "pass", "result"],
        "critical": ["critical", "critical fail"],
    }
    detected = {key: next((lookup[a] for a in names if a in lookup), "") for key, names in aliases.items()}
    with db() as con:
        con.execute("""INSERT INTO result_imports(import_id,filename,sha256,columns_json,row_count,created_by,created_at)
                       VALUES(?,?,?,?,?,?,?)""", (iid, Path(file.filename or "results").name, hashlib.sha256(content).hexdigest(), json.dumps(headers), len(rows), user, iso_now()))
        for n, row in enumerate(rows, 2):
            con.execute("INSERT INTO result_import_rows(import_id,row_number,row_json) VALUES(?,?,?)", (iid, n, json.dumps(row, ensure_ascii=False)))
    audit("RESULT_IMPORT_PREVIEWED", user, "import", iid, {"rows": len(rows)})
    return {"import_id": iid, "columns": headers, "detected_mapping": detected, "row_count": len(rows), "preview": rows[:8]}


def _bool_value(value: str) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "y", "pass", "passed", "critical"}


@router.post("/api/results/imports/{import_id}/commit")
def commit_result_import(import_id: str, payload: ImportCommitIn, user: str = Depends(require_roles("Administrator", "QA Reviewer"))):
    required = {"case_id", "audit_date", "opportunities"}
    if any(not payload.mapping.get(k) for k in required):
        raise HTTPException(400, "Map case ID, audit date, and opportunities")
    committed, errors = 0, []
    with db() as con:
        access.staging(con, user, "result_imports", "import_id", import_id)
        lock_scorecard_process(con, payload.process_id)
        access.process(con, user, payload.process_id, ("QA Reviewer",), active=True)
        if payload.scorecard_version_id is not None:
            card = con.execute("SELECT process_id FROM scorecard_versions WHERE id=?", (payload.scorecard_version_id,)).fetchone()
            if not card or card["process_id"] != payload.process_id:
                raise HTTPException(400, "Scorecard does not belong to this process")
        imp = con.execute("SELECT * FROM result_imports WHERE import_id=?", (import_id,)).fetchone()
        if not imp:
            raise HTTPException(404, "Import not found")
        if imp["status"] != "PREVIEW":
            raise HTTPException(409, "Import has already been committed")
        process = con.execute("SELECT * FROM processes WHERE id=?", (payload.process_id,)).fetchone()
        if not process:
            raise HTTPException(404, "Process not found")
        card_id = payload.scorecard_version_id
        if not card_id:
            card = published_scorecard(con, payload.process_id)
            card_id = card["id"] if card else None
        rows = con.execute("SELECT * FROM result_import_rows WHERE import_id=? ORDER BY row_number", (import_id,)).fetchall()
        validated: list[tuple[sqlite3.Row, dict[str, Any]]] = []
        seen_case_ids: set[str] = set()
        for staged in rows:
            raw = json.loads(staged["row_json"])
            try:
                get = lambda k: raw.get(payload.mapping.get(k, ""), "")
                case_id = str(get("case_id")).strip()
                if not case_id:
                    raise ValueError("Case ID is blank")
                normalized_case_id = case_id.casefold()
                if normalized_case_id in seen_case_ids:
                    raise ValueError("Duplicate case ID within this import")
                if con.execute("SELECT 1 FROM audit_cases WHERE process_id=? AND external_case_id=?", (payload.process_id, case_id)).fetchone():
                    raise ValueError("Duplicate case ID for this process")
                opportunities = int(float(str(get("opportunities") or "0").replace(",", "")))
                if opportunities < 1:
                    raise ValueError("Opportunities must be at least 1")
                defect_names = [x.strip() for x in re.split(r"[;|]", str(get("defects"))) if x.strip()]
                critical = _bool_value(get("critical"))
                score_text = str(get("weighted_score")).strip()
                score = float(score_text) if score_text else (100.0 if not defect_names else max(0, 100 * (1 - len(defect_names) / opportunities)))
                passed_text = str(get("passed")).strip()
                passed = _bool_value(passed_text) if passed_text else (not defect_names and not critical)
                submitted = str(get("audit_date") or iso_now()).strip()
                seen_case_ids.add(normalized_case_id)
                validated.append((staged, {"raw": raw, "case_id": case_id, "opportunities": opportunities,
                                          "defect_names": defect_names, "critical": critical, "score": score,
                                          "passed": passed, "submitted": submitted, "associate": str(get("associate")).strip()}))
            except Exception as exc:
                message = str(exc)
                con.execute("UPDATE result_import_rows SET status='ERROR',error=? WHERE id=?", (message, staged["id"]))
                errors.append({"row_number": staged["row_number"], "error": message})
        if errors:
            con.execute("UPDATE result_import_rows SET status='VALIDATED' WHERE import_id=? AND status='READY'", (import_id,))
            con.execute("UPDATE result_imports SET status='ERROR',mapping_json=?,error_count=? WHERE import_id=?",
                        (json.dumps(payload.mapping), len(errors), import_id))
        else:
            for staged, item in validated:
                aid = _id("AUD")
                con.execute("""INSERT INTO audit_cases(audit_id,process_id,external_case_id,scorecard_version_id,status,associate,
                               submitted_at,reviewed_at,reviewed_by,weighted_score,passed,critical_fail,opportunities,defect_count,
                               created_at,created_by,source_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (aid, payload.process_id, item["case_id"], card_id, "REVIEWED", item["associate"], item["submitted"],
                             item["submitted"], user, item["score"], int(item["passed"]), int(item["critical"]),
                             item["opportunities"], len(item["defect_names"]), iso_now(), user,
                             json.dumps(item["raw"], ensure_ascii=False)))
                for name in item["defect_names"]:
                    con.execute("""INSERT INTO audit_defects(audit_id,category,name,severity,critical,opportunities,created_at)
                                   VALUES(?,?,?,?,?,?,?)""", (aid, name, name, "Critical" if item["critical"] else "Major",
                                                              int(item["critical"]), 1, iso_now()))
                con.execute("UPDATE result_import_rows SET status='COMMITTED',audit_id=? WHERE id=?", (aid, staged["id"]))
                committed += 1
            con.execute("UPDATE result_imports SET status='COMMITTED',mapping_json=?,error_count=0,committed_at=? WHERE import_id=?",
                        (json.dumps(payload.mapping), iso_now(), import_id))
            settings = con.execute("SELECT * FROM process_settings WHERE process_id=?", (payload.process_id,)).fetchone()
            if settings:
                con.execute("UPDATE process_settings SET import_mapping_json=?,updated_at=? WHERE process_id=?", (json.dumps(payload.mapping), iso_now(), payload.process_id))
    audit("RESULT_IMPORT_COMMITTED", user, "import", import_id, {"committed": committed, "errors": len(errors)})
    return {"ok": True, "committed": committed, "error_count": len(errors), "errors": errors[:25]}


@router.get("/api/results/imports/{import_id}/errors")
def import_errors(import_id: str, user: str = Depends(current_user)):
    with db() as con:
        access.staging(con, user, "result_imports", "import_id", import_id)
        rows = con.execute("SELECT row_number,row_json,error FROM result_import_rows WHERE import_id=? AND status='ERROR' ORDER BY row_number", (import_id,)).fetchall()
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow(["Row", "Error", "Source Row"])
    for r in rows:
        writer.writerow([r["row_number"], r["error"], r["row_json"]])
    return StreamingResponse(iter([out.getvalue()]), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{import_id}_errors.csv"'})


def _range_filters(expression: str, date_from: str | None, date_to: str | None) -> tuple[str, list[Any]]:
    parsed: dict[str, date] = {}
    for name, value in (("date_from", date_from), ("date_to", date_to)):
        if not value:
            continue
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise HTTPException(400, f"{name} must use YYYY-MM-DD format")
        try:
            parsed[name] = date.fromisoformat(value)
        except ValueError as exc:
            raise HTTPException(400, f"{name} is not a valid date") from exc
    if parsed.get("date_from") and parsed.get("date_to") and parsed["date_from"] > parsed["date_to"]:
        raise HTTPException(400, "date_from must be on or before date_to")
    where, params = "", []
    if parsed.get("date_from"):
        where += f" AND {expression}>=?"; params.append(parsed["date_from"].isoformat())
    if parsed.get("date_to"):
        if parsed["date_to"] == date.max:
            where += f" AND substr({expression},1,10)<=?"; params.append(parsed["date_to"].isoformat())
        else:
            exclusive_to = parsed["date_to"] + timedelta(days=1)
            where += f" AND {expression}<?"; params.append(exclusive_to.isoformat())
    return where, params


def _date_filters(date_from: str | None, date_to: str | None) -> tuple[str, list[Any]]:
    return _range_filters("COALESCE(c.reviewed_at,c.submitted_at)", date_from, date_to)


def analytics_payload(account_id: int | None, process_id: int | None, date_from: str | None, date_to: str | None, user: str = "admin") -> dict[str, Any]:
    where = ["c.status='REVIEWED'"]
    params: list[Any] = []
    if account_id:
        where.append("p.account_id=?"); params.append(account_id)
    if process_id:
        where.append("c.process_id=?"); params.append(process_id)
    extra, date_params = _date_filters(date_from, date_to)
    sql_where = " AND ".join(where) + extra
    params += date_params
    with db() as con:
        clause, scoped = access.scope(con, user, "p.account_id", account_id, process_id)
        sql_where += " AND " + clause
        params += scoped
        # Aggregate before materializing rows: large histories stay in the database.
        days = [dict(r) for r in con.execute(
            f"""SELECT substr(CAST(COALESCE(c.reviewed_at,c.submitted_at,c.created_at) AS TEXT),1,10) label,
                       COUNT(*) n,
                       SUM(COALESCE(c.defect_count,0)) defects,
                       SUM(CASE WHEN c.defect_count>0 THEN 1 ELSE 0 END) defective,
                       SUM(CASE WHEN COALESCE(c.opportunities,0)<1 THEN 1 ELSE c.opportunities END) opportunities,
                       SUM(CASE WHEN c.critical_fail<>0 THEN 1 ELSE 0 END) critical
                FROM audit_cases c JOIN processes p ON p.id=c.process_id
                WHERE {sql_where} GROUP BY 1 ORDER BY 1""", params).fetchall()]
        categories = [dict(r) for r in con.execute(
            f"""SELECT COALESCE(NULLIF(d.category,''),d.name) category, COUNT(*) count
                FROM audit_defects d JOIN audit_cases c ON c.audit_id=d.audit_id
                JOIN processes p ON p.id=c.process_id
                WHERE {sql_where} GROUP BY 1""", params).fetchall()]
        capa_where, capa_params = [], []
        if process_id:
            capa_where.append("process_id=?"); capa_params.append(process_id)
        elif account_id:
            capa_where.append("process_id IN (SELECT id FROM processes WHERE account_id=?)"); capa_params.append(account_id)
        capa_scope, scoped = access.scope(con, user, "account_id")
        capa_where.append("process_id IN (SELECT id FROM processes WHERE " + capa_scope + ")")
        capa_params += scoped
        sql_capa = "WHERE " + " AND ".join(capa_where)
        capas = [dict(r) for r in con.execute(f"SELECT * FROM capas {sql_capa} ORDER BY CASE priority WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 ELSE 2 END,due_date LIMIT 12", capa_params).fetchall()]
    return {**summarize_quality(days, categories), "capas": capas}


@router.get("/api/analytics/summary")
def analytics_summary(account_id: int | None = None, process_id: int | None = None, date_from: str | None = None,
                      date_to: str | None = None, user: str = Depends(current_user)):
    return analytics_payload(account_id, process_id, date_from, date_to, user)


@router.get("/api/analytics/control-chart")
def analytics_control_chart(chart_type: str = "p", account_id: int | None = None, process_id: int | None = None,
                            date_from: str | None = None, date_to: str | None = None, user: str = Depends(current_user)):
    data = analytics_payload(account_id, process_id, date_from, date_to, user)
    if chart_type not in {"p", "u"}:
        raise HTTPException(400, "Chart type must be p or u")
    return {"chart_type": chart_type, "stability": data["stability"], "points": data["control_chart"]}


@router.get("/api/analytics/pareto")
def analytics_pareto(account_id: int | None = None, process_id: int | None = None, date_from: str | None = None,
                     date_to: str | None = None, user: str = Depends(current_user)):
    return analytics_payload(account_id, process_id, date_from, date_to, user)["pareto"]


@router.get("/api/analytics/capability")
def analytics_capability(process_id: int, item_id: int, user: str = Depends(current_user)):
    with db() as con:
        access.process(con, user, process_id)
        item = con.execute("SELECT * FROM scorecard_items WHERE id=? AND item_type='sla' AND scorecard_version_id IN (SELECT id FROM scorecard_versions WHERE process_id=?)", (item_id, process_id)).fetchone()
        if not item:
            raise HTTPException(404, "SLA item not found")
        values = [float(r[0]) for r in con.execute(
            """SELECT ar.numeric_value FROM audit_responses ar JOIN audit_cases c ON c.audit_id=ar.audit_id
               WHERE c.process_id=? AND c.status='REVIEWED' AND ar.item_id=? AND ar.numeric_value IS NOT NULL ORDER BY c.reviewed_at""",
            (process_id, item_id)).fetchall()]
    if len(values) < 20:
        return {"status": "insufficient", "count": len(values), "minimum": 20}
    avg = mean(values)
    variance = sum((x - avg) ** 2 for x in values) / (len(values) - 1)
    sigma = math.sqrt(variance)
    cp = ((item["usl"] - item["lsl"]) / (6 * sigma)) if sigma and item["usl"] is not None and item["lsl"] is not None else None
    candidates = []
    if sigma and item["usl"] is not None: candidates.append((item["usl"] - avg) / (3 * sigma))
    if sigma and item["lsl"] is not None: candidates.append((avg - item["lsl"]) / (3 * sigma))
    cpk = min(candidates) if candidates else None
    moving = [abs(values[i] - values[i-1]) for i in range(1, len(values))]
    mrbar = mean(moving) if moving else 0
    return {"status": "available", "count": len(values), "mean": round(avg, 4), "stdev": round(sigma, 4),
            "cp": None if cp is None else round(cp, 3), "cpk": None if cpk is None else round(cpk, 3),
            "imr": {"cl": avg, "ucl": avg + 2.66 * mrbar, "lcl": avg - 2.66 * mrbar,
                    "mr_cl": mrbar, "mr_ucl": 3.267 * mrbar}, "values": values}


@router.get("/api/capas")
def list_capas(process_id: int | None = None, stage: str | None = None, user: str = Depends(current_user),
               account_id: int | None = None, date_from: str | None = None, date_to: str | None = None):
    where, params = [], []
    if account_id: where.append("p.account_id=?"); params.append(account_id)
    if process_id: where.append("c.process_id=?"); params.append(process_id)
    if stage: where.append("c.stage=?"); params.append(stage.upper())
    extra, date_params = _range_filters("c.created_at", date_from, date_to)
    sql_where = "WHERE " + " AND ".join(where) if where else "WHERE 1=1"
    sql_where += extra; params += date_params
    with db() as con:
        clause, scoped = access.scope(con, user, "p.account_id", account_id, process_id)
        sql_where += " AND " + clause
        params += scoped
        rows = con.execute(f"""SELECT c.*,p.account_id,p.active process_active,p.name process_name,a.name account_name
            FROM capas c JOIN processes p ON p.id=c.process_id JOIN accounts a ON a.id=p.account_id
            {sql_where} ORDER BY CASE c.priority WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 WHEN 'MEDIUM' THEN 2 ELSE 3 END,c.due_date""", params).fetchall()
    return [dict(r) for r in rows]


@router.post("/api/capas")
def create_capa(payload: CapaIn, user: str = Depends(require_roles("Administrator", "QA Reviewer", "Operations Manager"))):
    with db() as con:
        access.process(con, user, payload.process_id, active=True)
        if not con.execute("SELECT 1 FROM processes WHERE id=?", (payload.process_id,)).fetchone():
            raise HTTPException(404, "Process not found")
        proc = access.process(con, user, payload.process_id, ("QA Reviewer", "Operations Manager"), active=True)
        access.validate_assignee(con, payload.owner, proc["account_id"], ("QA Reviewer", "Operations Manager"))
        if payload.audit_id:
            case = access.record(con, user, "audit_cases", "audit_id", payload.audit_id)
            if case["process_id"] != payload.process_id:
                raise HTTPException(400, "Audit does not belong to this process")
        cid = _create_capa(con, payload.process_id, payload.audit_id, payload.title.strip(), payload.priority.upper(), payload.owner, payload.due_date, user)
    audit("CAPA_CREATED", user, "capa", cid)
    return {"capa_id": cid, "ok": True}


@router.get("/api/capas/{capa_id}")
def get_capa(capa_id: str, user: str = Depends(current_user)):
    with db() as con:
        access.record(con, user, "capas", "capa_id", capa_id, ())
        row = con.execute("SELECT c.*,p.account_id,p.active process_active,p.name process_name,a.name account_name FROM capas c JOIN processes p ON p.id=c.process_id JOIN accounts a ON a.id=p.account_id WHERE capa_id=?", (capa_id,)).fetchone()
        if not row: raise HTTPException(404, "CAPA not found")
        out = dict(row)
        out["events"] = [dict(x) for x in con.execute("SELECT * FROM capa_events WHERE capa_id=? ORDER BY created_at", (capa_id,)).fetchall()]
    return out


@router.put("/api/capas/{capa_id}")
def update_capa(capa_id: str, payload: CapaUpdateIn, user: str = Depends(require_roles("Administrator", "QA Reviewer", "Operations Manager"))):
    with db() as con:
        access.record(con, user, "capas", "capa_id", capa_id, ('QA Reviewer', 'Operations Manager'))
        if not con.execute("SELECT 1 FROM capas WHERE capa_id=?", (capa_id,)).fetchone(): raise HTTPException(404, "CAPA not found")
        capa = access.record(con, user, "capas", "capa_id", capa_id)
        proc = access.process(con, user, capa["process_id"])
        if payload.owner != capa["owner"]:
            access.validate_assignee(con, payload.owner, proc["account_id"], ("QA Reviewer", "Operations Manager"))
        con.execute("""UPDATE capas SET owner=?,due_date=?,containment=?,root_cause=?,action_plan=?,implementation_notes=?,
                       effectiveness=?,updated_at=? WHERE capa_id=?""",
                    (payload.owner,payload.due_date,payload.containment,payload.root_cause,payload.action_plan,
                     payload.implementation_notes,payload.effectiveness,iso_now(),capa_id))
        con.execute("INSERT INTO capa_events(capa_id,event_type,detail_json,username,created_at) VALUES(?,'DETAILS_UPDATED',?,?,?)",
                    (capa_id, json.dumps(payload.model_dump(), ensure_ascii=False), user, iso_now()))
    audit("CAPA_UPDATED", user, "capa", capa_id)
    return {"ok": True}


@router.post("/api/capas/{capa_id}/transition")
def transition_capa(capa_id: str, payload: TransitionIn, user: str = Depends(require_roles("Administrator", "QA Reviewer", "Operations Manager"))):
    target = payload.to_stage.upper()
    if target not in CAPA_STAGES: raise HTTPException(400, "Invalid CAPA stage")
    with db() as con:
        access.record(con, user, "capas", "capa_id", capa_id, ('QA Reviewer', 'Operations Manager'))
        row = con.execute("SELECT stage,root_cause,action_plan,effectiveness FROM capas WHERE capa_id=?", (capa_id,)).fetchone()
        if not row: raise HTTPException(404, "CAPA not found")
        current = row["stage"]
        if CAPA_STAGES.index(target) != CAPA_STAGES.index(current) + 1:
            raise HTTPException(409, "CAPA stages must advance in sequence")
        if target == "ACTION_PLAN" and not row["root_cause"].strip(): raise HTTPException(400, "Root cause is required")
        if target == "IMPLEMENTATION" and not row["action_plan"].strip(): raise HTTPException(400, "Action plan is required")
        if target == "CLOSED" and not row["effectiveness"].strip(): raise HTTPException(400, "Effectiveness evidence is required")
        now = iso_now()
        con.execute("UPDATE capas SET stage=?,updated_at=?,closed_at=? WHERE capa_id=?", (target, now, now if target == "CLOSED" else None, capa_id))
        con.execute("INSERT INTO capa_events(capa_id,event_type,from_stage,to_stage,detail_json,username,created_at) VALUES(?,'TRANSITION',?,?,?,?,?)",
                    (capa_id,current,target,json.dumps({"comment":payload.comment}),user,now))
    audit("CAPA_TRANSITIONED", user, "capa", capa_id, {"from": current, "to": target})
    return {"ok": True, "stage": target}


@router.get("/api/reports/quality.xlsx")
def quality_report(account_id: int | None = None, process_id: int | None = None, date_from: str | None = None,
                   date_to: str | None = None, user: str = Depends(current_user)):
    data = analytics_payload(account_id, process_id, date_from, date_to, user)
    audits = list_audits(process_id=process_id, limit=1000, user=user, account_id=account_id, date_from=date_from, date_to=date_to)
    capas = list_capas(process_id=process_id, user=user, account_id=account_id, date_from=date_from, date_to=date_to)
    wb = Workbook(); summary = wb.active; summary.title = "Quality Summary"
    summary.append(["Metric", "Value"])
    for key, value in data["metrics"].items(): summary.append([key.replace("_", " ").title(), value])
    summary.append(["Control Limit Status", data["stability"]])
    pareto = wb.create_sheet("Pareto")
    pareto.append(["Category", "Count", "Percent", "Cumulative Percent"])
    for row in data["pareto"]: pareto.append([formula_safe(row["category"]), row["count"], row["percent"], row["cumulative_percent"]])
    aws = wb.create_sheet("Audits")
    audit_cols = ["audit_id","account_name","process_name","external_case_id","associate","status","weighted_score","passed","critical_fail","opportunities","defect_count","submitted_at","reviewed_at","reviewed_by"]
    aws.append([x.replace("_", " ").title() for x in audit_cols])
    for row in audits: aws.append([formula_safe(row.get(c, "")) for c in audit_cols])
    cws = wb.create_sheet("CAPA")
    capa_cols = ["capa_id","account_name","process_name","title","priority","stage","owner","due_date","created_at","closed_at"]
    cws.append([x.replace("_", " ").title() for x in capa_cols])
    for row in capas: cws.append([formula_safe(row.get(c, "")) for c in capa_cols])
    for ws in wb.worksheets:
        ws.freeze_panes = "A2"; ws.auto_filter.ref = ws.dimensions
    out = io.BytesIO(); wb.save(out); out.seek(0)
    audit("QUALITY_REPORT_EXPORTED", user, "report", None, {"account_id": account_id, "process_id": process_id,
                                                              "date_from": date_from, "date_to": date_to})
    return StreamingResponse(out, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": 'attachment; filename="Quality_Command_Center_Report.xlsx"'})


def lock_scorecard_process(con, process_id):
    # Both SQLite and PostgreSQL serialize writers to this process before MAX(version).
    con.execute("UPDATE processes SET updated_at=updated_at WHERE id=?", (process_id,))


@router.post("/api/admin/processes/{process_id}/archive")
def archive_process(process_id: int, user: str = Depends(require_roles("Administrator"))):
    with db() as con:
        access.require(con, user, allowed=("Administrator",))
        access.process(con, user, process_id)
        con.execute("UPDATE processes SET active=0,updated_at=? WHERE id=?", (iso_now(), process_id))
    audit("PROCESS_ARCHIVED", user, "process", str(process_id))
    return {"ok": True}


@router.post("/api/admin/processes/{process_id}/restore")
def restore_process(process_id: int, user: str = Depends(require_roles("Administrator"))):
    with db() as con:
        access.require(con, user, allowed=("Administrator",))
        proc = access.process(con, user, process_id)
        if not proc["account_active"]:
            raise HTTPException(409, "Restore requires an active parent account")
        con.execute("UPDATE processes SET active=1,updated_at=? WHERE id=?", (iso_now(), process_id))
    audit("PROCESS_RESTORED", user, "process", str(process_id))
    return {"ok": True}


@router.get("/api/admin/processes/{process_id}/sampling-controls")
def get_sampling_controls(process_id: int, user: str = Depends(current_user)):
    with db() as con:
        access.process(con, user, process_id)
        return sampling_config(con, process_id)


@router.put("/api/admin/processes/{process_id}/sampling-controls")
def save_sampling_controls(process_id: int, payload: ConfigIn, user: str = Depends(require_roles("Administrator"))):
    if payload.coverage_period not in {"day", "week", "month"}:
        raise HTTPException(400, "Coverage period must be day, week, or month")
    with db() as con:
        access.require(con, user, allowed=("Administrator",))
        access.process(con, user, process_id, active=True)
        initialize_process(con, process_id, iso_now())
        before = sampling_config(con, process_id)
        values = payload.model_dump()
        con.execute("""UPDATE process_sampling_config SET coverage_enabled=?,coverage_period=?,audits_per_associate=?,
            identifier_column_default=?,associate_column_default=?,exclude_previously_sampled=?,case_insensitive_ids=?,updated_at=? WHERE process_id=?""",
            (int(payload.coverage_enabled), payload.coverage_period, payload.audits_per_associate,
             payload.identifier_column_default, payload.associate_column_default, int(payload.exclude_previously_sampled),
             int(payload.case_insensitive_ids), iso_now(), process_id))
    audit("PROCESS_SAMPLING_CONTROLS_CHANGED", user, "process", str(process_id), {"before": before, "after": values})
    return {"ok": True}


@router.get("/api/accounts/{account_id}/assignees")
def account_assignees(account_id: int, role: str = "QA Auditor", user: str = Depends(current_user)):
    if role not in access.ACCOUNT_ROLES:
        raise HTTPException(400, "Unknown account role")
    with db() as con:
        access.require(con, user, account_id)
        return [dict(r) for r in con.execute("""SELECT p.username,p.display_name FROM user_profiles p WHERE p.active=1 AND
            (EXISTS(SELECT 1 FROM user_roles r WHERE r.username=p.username AND r.role_name='Administrator') OR
             EXISTS(SELECT 1 FROM account_user_roles r WHERE r.username=p.username AND r.account_id=? AND r.role_name=?))
             ORDER BY p.display_name""", (account_id, role)).fetchall()]


def editable_scorecard(con, user, scorecard_id):
    access.require(con, user, allowed=("Administrator",))
    card = access.record(con, user, "scorecard_versions", "id", scorecard_id)
    lock_scorecard_process(con, card["process_id"])
    access.process(con, user, card["process_id"], active=True)
    # Read status again after obtaining the writer lock.
    card = con.execute("SELECT * FROM scorecard_versions WHERE id=?", (scorecard_id,)).fetchone()
    if card["status"] != "DRAFT":
        raise HTTPException(409, "Published scorecards are immutable; edit as a new version")
    return card


@router.post("/api/admin/scorecards/{scorecard_id}/clone")
def clone_scorecard(scorecard_id: int, user: str = Depends(require_roles("Administrator"))):
    with db() as con:
        access.require(con, user, allowed=("Administrator",))
        card = access.record(con, user, "scorecard_versions", "id", scorecard_id)
        lock_scorecard_process(con, card["process_id"])
        access.process(con, user, card["process_id"], active=True)
        version = con.execute("SELECT COALESCE(MAX(version),0)+1 FROM scorecard_versions WHERE process_id=?", (card["process_id"],)).fetchone()[0]
        sid = con.execute("""INSERT INTO scorecard_versions(process_id,name,version,passing_score,opportunities_per_unit,
            critical_fail_override,created_at,created_by) VALUES(?,?,?,?,?,?,?,?)""",
            (card["process_id"], card["name"], version, card["passing_score"], card["opportunities_per_unit"], card["critical_fail_override"], iso_now(), user)).lastrowid
        con.execute("""INSERT INTO scorecard_items(scorecard_version_id,item_type,category,name,weight,severity,critical,
            opportunity_count,target,lsl,usl,unit,sort_order)
            SELECT ?,item_type,category,name,weight,severity,critical,opportunity_count,target,lsl,usl,unit,sort_order
            FROM scorecard_items WHERE scorecard_version_id=? AND active=1""", (sid, scorecard_id))
    audit("SCORECARD_CLONED", user, "scorecard", str(sid), {"source_id": scorecard_id, "version": version})
    return {"ok": True, "id": sid, "version": version}


@router.put("/api/admin/scorecards/{scorecard_id}")
def update_scorecard(scorecard_id: int, payload: ScorecardIn, user: str = Depends(require_roles("Administrator"))):
    with db() as con:
        card = editable_scorecard(con, user, scorecard_id)
        if card["process_id"] != payload.process_id:
            raise HTTPException(409, "A scorecard cannot be moved to another process")
        con.execute("""UPDATE scorecard_versions SET name=?,passing_score=?,opportunities_per_unit=?,critical_fail_override=? WHERE id=?""",
            (payload.name.strip(), payload.passing_score, payload.opportunities_per_unit, int(payload.critical_fail_override), scorecard_id))
    audit("SCORECARD_UPDATED", user, "scorecard", str(scorecard_id), payload.model_dump())
    return {"ok": True}


@router.put("/api/admin/scorecards/{scorecard_id}/items/{item_id}")
def update_scorecard_item(scorecard_id: int, item_id: int, payload: ScorecardItemIn, user: str = Depends(require_roles("Administrator"))):
    if payload.item_type not in {"question", "defect", "sla"}:
        raise HTTPException(400, "Invalid item type")
    with db() as con:
        editable_scorecard(con, user, scorecard_id)
        if not con.execute("SELECT 1 FROM scorecard_items WHERE id=? AND scorecard_version_id=? AND active=1", (item_id, scorecard_id)).fetchone():
            raise HTTPException(404, "Scorecard item not found")
        con.execute("""UPDATE scorecard_items SET item_type=?,category=?,name=?,weight=?,severity=?,critical=?,
            opportunity_count=?,target=?,lsl=?,usl=?,unit=?,sort_order=? WHERE id=? AND scorecard_version_id=?""",
            (payload.item_type, payload.category.strip(), payload.name.strip(), payload.weight, payload.severity, int(payload.critical),
             payload.opportunity_count, payload.target, payload.lsl, payload.usl, payload.unit, payload.sort_order, item_id, scorecard_id))
    audit("SCORECARD_ITEM_UPDATED", user, "scorecard", str(scorecard_id), {"item_id": item_id})
    return {"ok": True}


@router.delete("/api/admin/scorecards/{scorecard_id}/items/{item_id}")
def delete_scorecard_item(scorecard_id: int, item_id: int, user: str = Depends(require_roles("Administrator"))):
    with db() as con:
        editable_scorecard(con, user, scorecard_id)
        if not con.execute("SELECT 1 FROM scorecard_items WHERE id=? AND scorecard_version_id=? AND active=1", (item_id, scorecard_id)).fetchone():
            raise HTTPException(404, "Scorecard item not found")
        con.execute("UPDATE scorecard_items SET active=0 WHERE id=? AND scorecard_version_id=?", (item_id, scorecard_id))
    audit("SCORECARD_ITEM_REMOVED", user, "scorecard", str(scorecard_id), {"item_id": item_id})
    return {"ok": True}


class ScorecardOrderIn(BaseModel):
    item_ids: list[int]


@router.put("/api/admin/scorecards/{scorecard_id}/reorder")
def reorder_scorecard(scorecard_id: int, payload: ScorecardOrderIn, user: str = Depends(require_roles("Administrator"))):
    with db() as con:
        editable_scorecard(con, user, scorecard_id)
        ids = {r[0] for r in con.execute("SELECT id FROM scorecard_items WHERE scorecard_version_id=? AND active=1", (scorecard_id,)).fetchall()}
        if set(payload.item_ids) != ids or len(payload.item_ids) != len(ids):
            raise HTTPException(400, "Include every active item exactly once")
        for order, item_id in enumerate(payload.item_ids, 1):
            con.execute("UPDATE scorecard_items SET sort_order=? WHERE id=? AND scorecard_version_id=?", (order, item_id, scorecard_id))
    audit("SCORECARD_ITEMS_REORDERED", user, "scorecard", str(scorecard_id))
    return {"ok": True}


init_quality_db()
