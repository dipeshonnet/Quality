-- Backend sampling access repair. Apply after 0003_account_table_access.
BEGIN;
SET LOCAL ROLE qcc_migrator;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
DO $check$
BEGIN
    IF (SELECT count(*) FROM public.alembic_version) <> 1
       OR NOT EXISTS (SELECT 1 FROM public.alembic_version WHERE version_num = '0003_account_table_access') THEN
        RAISE EXCEPTION 'Expected migration 0003_account_table_access; inspect the current revision before applying this repair.';
    END IF;
END
$check$;

-- Running upgrade 0003_account_table_access -> 0004_sampling_table_access

DO $access$
        DECLARE target_table text; browser_role text; identity_sequence text;
        BEGIN
            FOREACH target_table IN ARRAY ARRAY[
                'uploads', 'sampling_runs', 'sample_records'
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
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_policies WHERE schemaname='public'
                            AND tablename=target_table AND policyname='qcc_backend_sampling_access'
                    ) THEN
                        EXECUTE format('CREATE POLICY qcc_backend_sampling_access ON public.%I FOR ALL TO qcc_app USING (true) WITH CHECK (true)', target_table);
                    END IF;
                END IF;
            END LOOP;
            identity_sequence := pg_get_serial_sequence('public.sample_records', 'id');
            IF identity_sequence IS NOT NULL AND EXISTS(SELECT 1 FROM pg_roles WHERE rolname='qcc_app') THEN
                EXECUTE format('GRANT USAGE ON SEQUENCE %s TO qcc_app', identity_sequence);
            END IF;
        END
        $access$;

UPDATE alembic_version SET version_num='0004_sampling_table_access' WHERE alembic_version.version_num = '0003_account_table_access';

COMMIT;
