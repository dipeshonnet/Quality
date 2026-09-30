-- Run the complete script once in Supabase SQL Editor as postgres.
-- Requires the existing qcc_migrator and qcc_app roles.
-- Preserves Administrator grants and existing process controls.
-- Non-Administrator users need account assignments after this migration.
BEGIN;
SET LOCAL ROLE qcc_migrator;
SET LOCAL search_path = public, extensions;
DO $check$
BEGIN
    IF (SELECT count(*) FROM public.alembic_version) <> 1
       OR NOT EXISTS (SELECT 1 FROM public.alembic_version WHERE version_num = '0001_postgres_schema') THEN
        RAISE EXCEPTION 'Expected migration 0001_postgres_schema. Stop and inspect the current revision before applying this repair.';
    END IF;
END
$check$;


-- Running upgrade 0001_postgres_schema -> 0002_account_structure

CREATE TABLE account_user_roles (
            username citext NOT NULL REFERENCES users(username) ON DELETE CASCADE,
            account_id bigint NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            role_name TEXT NOT NULL REFERENCES roles(name) CHECK(role_name IN ('QA Auditor','QA Reviewer','Operations Manager')),
            PRIMARY KEY(username,account_id,role_name));

CREATE INDEX ix_account_user_roles_account ON account_user_roles(account_id,username);

CREATE TABLE legacy_user_roles (
            username citext NOT NULL REFERENCES users(username) ON DELETE CASCADE,
            role_name TEXT NOT NULL, PRIMARY KEY(username,role_name));

INSERT INTO legacy_user_roles SELECT username,role_name FROM user_roles WHERE role_name<>'Administrator';

DELETE FROM user_roles WHERE role_name<>'Administrator';

CREATE TABLE process_sampling_config (
            process_id bigint PRIMARY KEY REFERENCES processes(id) ON DELETE CASCADE,
            coverage_enabled INTEGER NOT NULL DEFAULT 0 CHECK(coverage_enabled IN (0,1)),
            coverage_period TEXT NOT NULL DEFAULT 'week' CHECK(coverage_period IN ('day','week','month')),
            audits_per_associate INTEGER NOT NULL DEFAULT 3 CHECK(audits_per_associate>0),
            identifier_column_default TEXT, associate_column_default TEXT,
            exclude_previously_sampled INTEGER NOT NULL DEFAULT 1 CHECK(exclude_previously_sampled IN (0,1)),
            case_insensitive_ids INTEGER NOT NULL DEFAULT 1 CHECK(case_insensitive_ids IN (0,1)),
            updated_at timestamptz NOT NULL);

INSERT INTO process_sampling_config
            SELECT p.id,COALESCE(c.coverage_enabled,0),COALESCE(c.coverage_period,'week'),COALESCE(c.audits_per_associate,3),
            c.identifier_column_default,c.associate_column_default,COALESCE(c.exclude_previously_sampled,1),
            COALESCE(c.case_insensitive_ids,1),p.updated_at
            FROM processes p LEFT JOIN account_config c ON c.account_id=p.account_id;

ALTER TABLE uploads ADD COLUMN created_by citext REFERENCES users(username);

UPDATE alembic_version SET version_num='0002_account_structure' WHERE alembic_version.version_num = '0001_postgres_schema';

-- Running upgrade 0002_account_structure -> 0003_account_table_access

DO $access$
        DECLARE target_table text; browser_role text;
        BEGIN
            FOREACH target_table IN ARRAY ARRAY[
                'account_user_roles', 'legacy_user_roles', 'process_sampling_config'
            ] LOOP
                EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', target_table);
                EXECUTE format('REVOKE ALL ON TABLE public.%I FROM PUBLIC', target_table);
                FOREACH browser_role IN ARRAY ARRAY['anon','authenticated'] LOOP
                    IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname=browser_role) THEN
                        EXECUTE format('REVOKE ALL ON TABLE public.%I FROM %I', target_table, browser_role);
                    END IF;
                END LOOP;
                IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='qcc_app') THEN
                    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.%I TO qcc_app', target_table);
                    EXECUTE format('CREATE POLICY qcc_backend_access ON public.%I FOR ALL TO qcc_app USING (true) WITH CHECK (true)', target_table);
                END IF;
            END LOOP;
            IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='qcc_app') THEN
                GRANT SELECT ON TABLE public.alembic_version TO qcc_app;
                CREATE POLICY qcc_backend_schema_version ON public.alembic_version
                    FOR SELECT TO qcc_app USING (true);
            END IF;
        END
        $access$;

UPDATE alembic_version SET version_num='0003_account_table_access' WHERE alembic_version.version_num = '0002_account_structure';

COMMIT;


-- Expected revision: 0003_account_table_access
SELECT version_num FROM public.alembic_version;
SELECT to_regclass('public.account_user_roles') AS account_roles,
       to_regclass('public.process_sampling_config') AS sampling_controls;
