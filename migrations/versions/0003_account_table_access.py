"""Grant the backend access to account configuration tables under RLS.

Revision ID: 0003_account_table_access
Revises: 0002_account_structure
"""
from alembic import op

revision = "0003_account_table_access"
down_revision = "0002_account_structure"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
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
    """)


def downgrade():
    raise RuntimeError("Removing backend account-table access would break this application release.")
