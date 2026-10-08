# Quality Command Center

Quality Command Center supports operational sampling, versioned scorecard audits, Six Sigma analytics, corrective and preventive action (CAPA), reporting, and account-scoped administration. The application in `main` uses FastAPI with a bundled web interface. It runs locally with SQLite and supports PostgreSQL and Supabase Storage for hosted backend deployments.

## Hosted site

Explore the [Quality Command Center visual demo](https://qccenter.netlify.app/). The Netlify site is built from the separate `netlify-site` branch and uses sample data. It is not connected to the FastAPI application in `main`, so actions on the demo do not create or change application records.

The live application code and local run instructions are below. The planned Netlify frontend and Render API integration is documented in [`docs/migration/50-master-plan.md`](docs/migration/50-master-plan.md).

## Run locally on Windows

1. Install Python 3.11 or later from python.org. During installation, enable **Add Python to PATH**.
2. Double-click `start_windows.bat`.
3. The first launch creates `.venv`, installs the pinned packages, and opens http://127.0.0.1:8765.
4. If this is a new database, create the administrator password. Existing installations retain the current administrator account and password.

The first dependency installation may need internet access. Chart.js, Bootstrap Icons, fonts, and application assets are bundled locally; normal operation is offline.

Local configuration loads from `~/.config/quality-command-center/.env`, outside the source tree. Set `QCC_ENV_FILE` to use another private configuration file; process environment variables take precedence. Copy `.env.example` to that private location when configuring a new installation. Keep real database URLs and cloud secrets out of project files. Hosted deployments can supply settings through their environment/secret manager. Session cookies require HTTPS in staging/production or when `PUBLIC_ORIGIN` uses HTTPS; local HTTP development remains supported.

## Product areas

- **Home** — account/process filters, p/u control chart, Yield, DPMO, Sigma level, critical findings, Pareto, and urgent CAPA queue.
- **Scorecards** — published, versioned scorecard definitions by process.
- **Sampling** — upload, inspect, map, preview, commit, and automatically create auditable cases for selected records.
- **Results** — auditor queues, weighted checklist and SLA scoring, defect capture, evidence, submission, reviewer approval/rejection, and historical CSV/XLSX imports.
- **Analytics** — Six Sigma scorecard, p/u control charts, Pareto, capability statistics, and process exceptions.
- **CAPA** — staged corrective/preventive action from Draft through Effectiveness Review and Closed, with evidence and event history.
- **Reports** — filtered quality workbooks and existing sampling-run exports.
- **Admin** — users/roles, accounts/processes, scorecard versions, process policies, import mappings, CAPA controls, backups, restore, system defaults, and audit history.

For presentations, an Administrator can open **Admin → System & audit** and enable **Showcase data**. The switch loads a labeled synthetic account with two processes, published scorecards, 181 audit cases, 30-day control-chart history, numeric SLA observations, Pareto defects, and five CAPAs. Turning the switch off removes only those generated showcase records; existing accounts and history are preserved.

## Roles and lifecycle

The built-in roles are Administrator, QA Auditor, QA Reviewer, and Operations Manager. Authorization is enforced by the API, not only by the navigation.

Administrator is global. The other roles are assigned per account in **Admin → Users & roles**; one user may have different roles in different accounts. Lists, record details, sampling, imports, analytics, and exports enforce those account assignments. Users without assignments can sign in but see no account data.

**Admin → Accounts & processes** supports process Archive and Restore. Archiving stops new work and retains history; existing audits and CAPAs can finish. Use **Show archived processes** to restore a process whose parent account is active. Reporting filters continue to include archived history.

Audit cases follow `UNASSIGNED -> ASSIGNED -> IN_PROGRESS -> SUBMITTED -> REVIEWED`. Reviewers can reject a submission back to the auditor; voiding is retained in history. Approved critical findings create a draft CAPA when the process policy enables the automatic trigger.

CAPA stages are Draft, Containment, Root Cause, Action Plan, Implementation, Effectiveness Review, and Closed. Stage changes require the previous stage and append immutable events.

Published scorecard versions are immutable so later template changes cannot alter historical audits.

In **Admin → Scorecards**, select Account, then Process. Admin can edit draft details and items, remove or reorder items, and use **Edit as new version** for existing published or archived scorecards. Publishing the new version applies it to future audits; existing audits keep their original version.

Both panels in **Admin → Process policies** require Account and Process. Sampling coverage quotas, previous-identifier checks, case matching, and remembered column mappings are independent for each process.

## Account-structure upgrade

SQLite applies schema version 3 automatically at startup. PostgreSQL deployments must run `alembic upgrade head` using the migration connection before starting this backend/frontend release. Revision `0002_account_structure` adds the schema; `0003_account_table_access` grants the backend role access under row-level security while denying browser roles. Take the usual database backup before upgrading. Render's current build/start commands do not run migrations: run them separately with the migration role before deploying. The readiness endpoint rejects missing account-schema tables instead of reporting a broken release as ready.

The migration copies each account's sampling controls into its existing processes once. Future processes start with application defaults. Administrator grants and credentials remain intact. Existing global non-Administrator grants are retained as migration reference, but no longer authorize access: Admin must assign accounts to users marked **Account assignment required**. Repeat startups preserve the new grants and settings.

PostgreSQL revision `0004_sampling_table_access` adds backend-only row-level security policies for `uploads`, `sampling_runs`, and `sample_records`, including access to the sample-record identity sequence. Without these policies, storage may accept a file while saving its upload record fails. Run `alembic upgrade head`, or use `docs/repair-sampling-access.sql` in Supabase SQL Editor when the database is at revision `0003_account_table_access`. Browser roles remain denied; account and upload-owner authorization stays enforced by the API.

User APIs now expose global `roles` (Administrator only) and `account_roles`, for example `[{"account_id": 1, "roles": ["QA Auditor"]}]`. Sampling configuration uses `GET/PUT /api/admin/processes/{process_id}/sampling-controls`. The former account configuration PUT returns HTTP 410 and points callers to the process endpoint. No automatic grant is made when a new account is created.

## Six Sigma definitions

- Yield = defect-free reviewed audits / reviewed audits.
- DPU = defects / audited units.
- DPMO = defects / recorded opportunities × 1,000,000.
- Sigma level = inverse-normal yield plus the conventional 1.5-sigma shift.
- p-chart = defective proportion with subgroup-specific three-sigma limits.
- u-chart = defects per unit with subgroup-specific three-sigma limits.
- I-MR and Cp/Cpk are calculated for numeric front-office SLA measures with configured specification limits.
- Pareto includes defect counts, share, and cumulative percentage.

Control limits are unavailable below five subgroups, provisional for 5–19, and stable from 20. Analytics include only reviewed audits and exclude drafts, rejected cases, voided runs, and open imports.

## Historical results import

Results accepts `.xlsx` and `.csv` files through a controlled preview-and-commit flow. Map case ID, date, associate, defects, opportunities, score, pass/fail, and critical fields; validate rows and duplicates; then atomically commit valid results. Invalid rows remain downloadable as an error CSV. Spreadsheet cells beginning with formula characters are neutralized in exports.

## Local data

- `data/quality_randomizer.db` — SQLite database.
- `data/uploads/` — temporary sampling and import uploads.
- `backups/` — automatic/manual database backups.
- `logs/quality_randomizer.log` — application log without source row content.
- `exports/` — reserved export workspace; browser downloads are generated on demand.

Schema migrations are idempotent. Existing accounts receive a default Legacy Process, prior sampling runs are attached without fabricating audit results, and the existing administrator is assigned the Administrator role.

## Security and audit controls

- PBKDF2-SHA256 password hashing and HttpOnly local sessions.
- Same-origin CSRF cookie/header protection for mutations.
- Server-side role checks.
- Formula-injection protection in CSV/XLSX output.
- Immutable run, review, import, configuration, and CAPA event records.
- Integrity-checked backup/restore with a pre-restore backup.
- Security response headers and no inline JavaScript.

## Run verification

From the project folder:

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe -m py_compile app.py quality.py
node --check static\app.js
node --check static\account-admin.js
```

See `TEST_REPORT.md` and `design-qa.md` for the latest verification evidence.

## Stop the application

Close the command window that started the app, or press Ctrl+C in that window.
