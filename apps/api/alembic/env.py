"""Alembic environment — wired to the async engine and the naming-convention
metadata so a generated migration and the running application never disagree
about a constraint name (database.md §2, §17 R-1).

No revision exists yet: no ORM models have been declared (this bootstrap task
deliberately does not implement any — A64-006 scope). The first module to
declare a table is also the first to run `alembic revision --autogenerate`.
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app.config.settings import get_settings
from app.database.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Single source of truth for "what tables exist" — every module's Base
# metadata is expected to merge into this one target (database.md §3,
# one schema namespace per module, one MetaData naming convention).
target_metadata = Base.metadata


def _dsn() -> str:
    # Settings, not alembic.ini, own the DSN — see alembic.ini's comment.
    # Migrations always run against the primary; there is no replica case
    # for DDL (database.md §13.2 replica routing is a read-only concern).
    return get_settings().postgres.dsn


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live database connection."""
    context.configure(
        url=_dsn(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations_sync(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Emit SQL against a live database connection, via the async engine."""
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _dsn()

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        future=True,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(_run_migrations_sync)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
