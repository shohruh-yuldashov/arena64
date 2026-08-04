"""The models and the migrations agree — the check that was missing.

Every other suite on this platform builds its schema from
`Base.metadata.create_all`, which makes the models true **by construction**.
That is right for a test about behaviour and it is blind to the one failure
this file exists to catch: a column mapped on a model and never migrated.

`MatchRecordModel.received_at` was exactly that. It sat in the metadata from
A64-016.5 and in no migration, so contract tests had the column, a
migration-built database did not, and **every ORM read and write of
`game.match` failed in production** — while `alembic upgrade head` ran
clean, `mypy` passed and the A64-016.8 audit's up/down/up round trip was
green, because all three prove the migrations are consistent with *each
other* rather than with the models.

So this suite builds its database the way a deployment does — `alembic
upgrade head`, on a scratch database of its own — and then asks Alembic the
only question that finds the gap: is there any difference between what the
models declare and what the migrations produced?

Skipped, not failed, when PostgreSQL is unreachable.
"""

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import app.database.models  # noqa: F401 — registers every module's tables
from app.database.base import Base
from app.modules.game.domain.match_record import MatchRecordStatus
from app.modules.game.domain.variants import ProductVariant
from app.modules.game.infrastructure.models import MatchRecordModel

#: A database this suite owns, created and dropped per run.
#:
#: Its own, rather than the contract suite's `arena64_test`: that one is
#: built by `create_all` and rebuilt by other tests, and running migrations
#: into it would make this suite's answer depend on what ran before it.
_DRIFT_DATABASE = "arena64_drift_check"

_DEFAULT_DSN = "postgresql+asyncpg://arena64:arena64@localhost:55432/arena64_test"


def _admin_dsn() -> str:
    configured = os.environ.get("CONTRACT_TEST_POSTGRES_DSN", _DEFAULT_DSN)
    return configured.replace("+asyncpg", "").rsplit("/", 1)[0] + "/postgres"


def _drift_dsn(*, driver: str) -> str:
    configured = os.environ.get("CONTRACT_TEST_POSTGRES_DSN", _DEFAULT_DSN)
    base = configured.rsplit("/", 1)[0]
    if driver == "sync":
        base = base.replace("+asyncpg", "")
    return f"{base}/{_DRIFT_DATABASE}"


@pytest_asyncio.fixture(scope="module")
async def migrated_database() -> AsyncIterator[str]:
    """A scratch database built by `alembic upgrade head`.

    Module-scoped: running twenty-eight migrations is seconds of work and
    every test here asks the same question of the same schema.
    """
    try:
        admin = await asyncpg.connect(_admin_dsn())
    except (OSError, asyncpg.PostgresError) as unreachable:  # pragma: no cover
        pytest.skip(f"PostgreSQL unreachable: {unreachable}")

    await admin.execute(f'DROP DATABASE IF EXISTS "{_DRIFT_DATABASE}"')
    await admin.execute(f'CREATE DATABASE "{_DRIFT_DATABASE}"')
    await admin.close()

    # A **subprocess**, not `alembic.command`, and that is not laziness:
    # `alembic/env.py` overwrites `sqlalchemy.url` with
    # `get_settings().postgres.dsn`, so a config option set in-process is
    # ignored and the migrations would silently run against the developer's
    # own database. `POSTGRES_DSN` is the only input env.py reads, and a
    # subprocess is the only way to set it without mutating a cached
    # `Settings` the rest of the suite shares.
    #
    # It is also how a deployment runs them, which is the point of this file.
    upgrade = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env={**os.environ, "POSTGRES_DSN": _drift_dsn(driver="async")},
        capture_output=True,
        text=True,
    )
    assert upgrade.returncode == 0, f"alembic upgrade head failed:\n{upgrade.stderr}"

    try:
        yield _drift_dsn(driver="async")
    finally:
        admin = await asyncpg.connect(_admin_dsn())
        await admin.execute(f'DROP DATABASE IF EXISTS "{_DRIFT_DATABASE}" WITH (FORCE)')
        await admin.close()


class TestTheMigrationsMatchTheModels:
    async def test_no_table_or_column_differs_from_what_the_models_declare(
        self, migrated_database: str
    ) -> None:
        """Alembic's own comparison, asserted to be empty.

        This is `alembic revision --autogenerate` run as an assertion: if it
        would have anything to write, the models and the migrations disagree
        and one of them is wrong.

        Differences are reported in full rather than counted, because the
        useful failure message is *which* column — "3 differences" sends the
        reader back to run autogenerate by hand, which is the step that was
        skipped when this defect shipped.

        Indexes created by raw DDL rather than by a model — `users`' two
        trigram search indexes, attached in `register_search_ddl` — are
        excluded. They exist in the database and cannot exist in the
        metadata, so Alembic reports them as removals on every run; that is
        a property of expression indexes, not drift.
        """
        engine = create_async_engine(_drift_dsn(driver="async"))
        try:
            async with engine.connect() as connection:
                differences = await connection.run_sync(_differences)
        finally:
            await engine.dispose()

        unexpected = [
            difference
            for difference in differences
            if not _is_raw_ddl_index(difference) and not _is_test_fixture_table(difference)
        ]

        assert unexpected == [], f"models and migrations disagree: {unexpected}"

    async def test_a_match_row_can_be_written_and_read_back(self, migrated_database: str) -> None:
        """The failure the drift actually caused, reproduced directly.

        `MatchRecordModel.received_at` made both of these raise
        `UndefinedColumnError` against a migration-built database while
        every `create_all` suite passed. An INSERT and a SELECT through the
        ORM are the cheapest possible statement of "this table is usable",
        and they are what nothing was asserting.
        """
        engine = create_async_engine(_drift_dsn(driver="async"))
        match_id, pairing_id = uuid4(), uuid4()
        now = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)

        try:
            async with AsyncSession(engine) as session:
                session.add(
                    MatchRecordModel(
                        id=match_id,
                        pairing_id=pairing_id,
                        variant=ProductVariant.RUSSIAN_8X8,
                        rated=True,
                        engine_version=2,
                        status=MatchRecordStatus.PENDING_ACCEPTANCE,
                        light_player_id=uuid4(),
                        light_ticket_id=uuid4(),
                        dark_player_id=uuid4(),
                        dark_ticket_id=uuid4(),
                        # Later than `created_at`, which
                        # `ck_match__acceptance_window_positive` requires.
                        acceptance_deadline=now + timedelta(seconds=30),
                        ply_number=0,
                        created_at=now,
                    )
                )
                await session.commit()

                stored = await session.scalar(
                    select(MatchRecordModel).where(MatchRecordModel.id == match_id)
                )

            assert stored is not None
            assert stored.pairing_id == pairing_id
        finally:
            await engine.dispose()


def _differences(connection: Connection) -> list[object]:
    """Alembic's comparison, run on a synchronous connection.

    `compare_metadata` is synchronous and this suite's engine is not, so it
    runs through `run_sync` rather than through a second engine on a driver
    (`psycopg2`) this project does not install.
    """
    context = MigrationContext.configure(
        connection, opts={"include_schemas": True, "compare_type": False}
    )
    return list(compare_metadata(context, Base.metadata))


def _is_test_fixture_table(difference: object) -> bool:
    """Whether a difference is `tests/contract/_models.py`'s own table.

    `ContractWidget` exists to exercise the repository base class and is
    registered on `Base.metadata` by the contract suite's import, never by
    a migration. Excluded by name: a real table missing from the migrations
    is still a failure.
    """
    return "contract_widget" in str(difference)


def _is_raw_ddl_index(difference: object) -> bool:
    """Whether a reported difference is an index the metadata cannot hold.

    `users`' trigram search indexes are created by `register_search_ddl`
    over an expression, which SQLAlchemy's metadata does not model — so
    Alembic sees them in the database, not in the models, and reports a
    removal every time. Excluded by name rather than by type, so a *real*
    dropped index is still a failure.
    """
    if not isinstance(difference, tuple) or len(difference) < 2:
        return False
    return "search" in str(difference[1])
