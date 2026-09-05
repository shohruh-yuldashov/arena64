"""The async SQLAlchemy engine and session factory.

One engine per process, built once at startup and disposed at shutdown
(dependency-injection.md §1.3 — the pool *is* the shared resource; a
per-request engine would open a connection per request and defeat pooling
entirely).
"""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config.settings import PostgresSettings


def create_engine(settings: PostgresSettings) -> AsyncEngine:
    return create_async_engine(
        settings.dsn.get_secret_value(),
        pool_size=settings.pool_size,
        max_overflow=settings.max_overflow,
        pool_timeout=settings.pool_timeout_seconds,
        # A64-028.3, P2-1. Measured: without it, the first request after a
        # pooled backend dies is answered with an `InterfaceError` — up to
        # one per pooled connection after a database restart. See
        # `PostgresSettings.pool_pre_ping` and
        # `tests/contract/test_pool_resilience.py`.
        pool_pre_ping=settings.pool_pre_ping,
        echo=settings.echo,
        connect_args={
            # database.md §13.3 DB-23: per-role timeouts are set rather
            # than left to defaults. Only one role (this process) exists
            # until the gateway/worker/clock entrypoints are split out.
            "server_settings": {"statement_timeout": str(settings.statement_timeout_ms)},
            # asyncpg's own default is 60s, which is six times the statement
            # budget and twice the pool wait — A64-028.3 §16.
            "timeout": settings.connect_timeout_seconds,
        },
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """`expire_on_commit=False`: repositories return domain entities mapped
    from ORM rows (repositories.md §4); expiring attributes on commit would
    force an unwanted reload the instant a service reads a value it just
    persisted in the same unit of work.
    """
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
