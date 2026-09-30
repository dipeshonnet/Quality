"""Allow the backend sampling workflow through PostgreSQL row-level security.

Revision ID: 0004_sampling_table_access
Revises: 0003_account_table_access
"""
from alembic import op

revision = "0004_sampling_table_access"
down_revision = "0003_account_table_access"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
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
    """)


def downgrade():
    for table in ('uploads', 'sampling_runs', 'sample_records'):
        op.execute(f'DROP POLICY IF EXISTS qcc_backend_sampling_access ON public.{table}')
