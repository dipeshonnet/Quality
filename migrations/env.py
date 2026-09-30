from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from qcc.config import settings


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None
database_url = os.getenv("DIRECT_DATABASE_URL") or settings.direct_database_url or settings.database_url or "postgresql+psycopg://"
config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))


def run_migrations_offline() -> None:
    context.configure(url=database_url, target_metadata=target_metadata, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    supplied_connection = config.attributes.get("connection")
    if supplied_connection is not None:
        context.configure(connection=supplied_connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
        return
    if not (settings.direct_database_url or settings.database_url):
        raise RuntimeError("Set DIRECT_DATABASE_URL before running online migrations")
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args={
            "sslmode": "verify-full",
            **({"sslrootcert": settings.database_ssl_root_cert_file} if settings.database_ssl_root_cert_file else {}),
        },
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
