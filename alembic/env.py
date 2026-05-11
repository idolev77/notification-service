"""
Alembic migration environment.

WHY this file pulls config + metadata from the application:
  - `sqlalchemy.url` is intentionally NOT in `alembic.ini` (no secrets in VCS).
    It is sourced from `Settings().database_url` so the same env vars drive
    runtime *and* migrations.
  - `target_metadata = Base.metadata` after importing `app.models` ensures
    `--autogenerate` sees every ORM model.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Import application config + models so metadata is fully populated.
from app.core.config import get_settings
from app.models import Base  # noqa: F401  (registers all model tables)
import app.models  # noqa: F401  (side-effect: imports every model module)

# Standard alembic plumbing.
config = context.config

# Inject the runtime DB URL into Alembic's config at execution time.
config.set_main_option("sqlalchemy.url", get_settings().database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Generate SQL without a DB connection ('offline' mode)."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live DB connection ('online' mode)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
