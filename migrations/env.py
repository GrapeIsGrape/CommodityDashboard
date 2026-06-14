"""Alembic environment. Reads the DB URL from env (via common.config) so no
credentials live in alembic.ini."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from common.config import get_database_url

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# No declarative models yet (Phase 2 introduces tables); autogenerate is unused.
target_metadata = None


def run_migrations_offline() -> None:
    context.configure(
        url=get_database_url().render_as_string(hide_password=False),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(get_database_url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
