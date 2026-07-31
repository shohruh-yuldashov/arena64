"""Every mixin, against a real PostgreSQL 17 table — proving what a unit
test over metadata alone cannot: that `UtcDateTime` round-trips correctly
through `timestamptz`, that `server_default=func.now()` actually populates
on insert, that the UUIDv7 primary key is genuinely unique under real
`INSERT`s, and that optimistic version checking genuinely raises on a
stale write.
"""

import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import StatementError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from tests.contract._models import ContractWidget


class TestUUIDPrimaryKeyMixin:
    async def test_id_is_none_until_flush(self, contract_session: AsyncSession) -> None:
        # The Python-side default runs during flush, not at __init__ — see
        # UUIDPrimaryKeyMixin's docstring on exactly why this is correct,
        # not a limitation.
        widget = ContractWidget(name="a")
        assert widget.id is None

    async def test_id_is_a_v7_uuid_once_flushed(self, contract_session: AsyncSession) -> None:
        widget = ContractWidget(name="a")
        contract_session.add(widget)
        await contract_session.flush()

        assert widget.id is not None
        assert widget.id.version == 7

    async def test_two_widgets_never_collide(self, contract_session: AsyncSession) -> None:
        a, b = ContractWidget(name="a"), ContractWidget(name="b")
        contract_session.add_all([a, b])
        await contract_session.flush()
        assert a.id != b.id


class TestTimestampMixin:
    async def test_created_at_is_populated_by_the_server_default(
        self, contract_session: AsyncSession
    ) -> None:
        widget = ContractWidget(name="a")
        contract_session.add(widget)
        await contract_session.flush()
        await contract_session.refresh(widget)

        assert widget.created_at is not None
        assert widget.created_at.tzinfo is not None

    async def test_updated_at_is_null_until_an_update(self, contract_session: AsyncSession) -> None:
        widget = ContractWidget(name="a")
        contract_session.add(widget)
        await contract_session.flush()
        assert widget.updated_at is None

    async def test_updated_at_is_populated_by_the_server_onupdate(
        self, contract_session: AsyncSession
    ) -> None:
        widget = ContractWidget(name="a")
        contract_session.add(widget)
        await contract_session.flush()

        widget.name = "b"
        await contract_session.flush()
        await contract_session.refresh(widget)

        assert widget.updated_at is not None

    async def test_an_explicit_application_set_value_overrides_the_server_default(
        self, contract_session: AsyncSession
    ) -> None:
        # database.md DB-19: the server default is a backstop, not the
        # primary mechanism — an explicit value from the application's own
        # injected clock must win.
        explicit = datetime(2020, 1, 1, tzinfo=UTC)
        widget = ContractWidget(name="a", created_at=explicit)
        contract_session.add(widget)
        await contract_session.flush()
        await contract_session.refresh(widget)

        assert widget.created_at == explicit


class TestUtcDateTimeType:
    async def test_rejects_a_naive_datetime_at_flush(self, contract_session: AsyncSession) -> None:
        # Not a bare ValueError: SQLAlchemy wraps any exception raised
        # inside a bind-parameter processor in `StatementError` once it
        # reaches actual statement execution (unlike calling
        # `process_bind_param` directly, as tests/unit/test_database_types.py
        # does — there it's the plain ValueError). `.orig` is the original
        # ValueError this type raised.
        widget = ContractWidget(name="a", created_at=datetime(2020, 1, 1))  # noqa: DTZ001
        contract_session.add(widget)
        with pytest.raises(StatementError, match="timezone-aware") as exc_info:
            await contract_session.flush()
        assert isinstance(exc_info.value.orig, ValueError)

    async def test_a_non_utc_zone_round_trips_as_utc(self, contract_session: AsyncSession) -> None:
        plus_five = timezone(timedelta(hours=5))
        widget = ContractWidget(name="a", created_at=datetime(2020, 1, 1, 5, 0, tzinfo=plus_five))
        contract_session.add(widget)
        await contract_session.flush()
        await contract_session.refresh(widget)

        assert widget.created_at == datetime(2020, 1, 1, 0, 0, tzinfo=UTC)


class TestSoftDeleteMixin:
    async def test_is_deleted_reflects_deleted_at(self, contract_session: AsyncSession) -> None:
        widget = ContractWidget(name="a")
        assert widget.is_deleted is False

        widget.deleted_at = datetime.now(UTC)
        assert widget.is_deleted is True

    async def test_a_soft_deleted_row_is_not_excluded_by_default(
        self, contract_session: AsyncSession
    ) -> None:
        # SoftDeleteMixin installs no global filter — a repository decides
        # explicitly. A plain select must still return the row.
        widget = ContractWidget(name="a", deleted_at=datetime.now(UTC))
        contract_session.add(widget)
        await contract_session.flush()

        result = await contract_session.scalars(
            select(ContractWidget).where(ContractWidget.id == widget.id)
        )
        assert result.one_or_none() is not None


class TestAuditMixin:
    async def test_version_defaults_to_one(self, contract_session: AsyncSession) -> None:
        widget = ContractWidget(name="a")
        contract_session.add(widget)
        await contract_session.flush()
        assert widget.version == 1

    async def test_version_increments_on_update(self, contract_session: AsyncSession) -> None:
        widget = ContractWidget(name="a")
        contract_session.add(widget)
        await contract_session.flush()

        widget.name = "b"
        await contract_session.flush()
        assert widget.version == 2

    async def test_a_stale_write_is_rejected(self, contract_session: AsyncSession) -> None:
        # The concrete proof of repositories.md §8.4's optimistic
        # concurrency mechanism. `widget` here plays the role of a writer
        # that loaded the row at version 1 and is about to save a change
        # based on that read; the raw SQL `UPDATE` below simulates a
        # second, independent writer committing first and moving the row
        # to version 2 without `widget` knowing. `widget`'s own save must
        # then fail — silently overwriting the second writer's change
        # would be exactly the lost update optimistic concurrency exists
        # to prevent.
        widget = ContractWidget(name="a")
        contract_session.add(widget)
        await contract_session.flush()
        assert widget.version == 1

        await contract_session.execute(
            text("UPDATE contract_widget SET version = 2 WHERE id = :id"),
            {"id": widget.id},
        )

        widget.name = "changed by the stale writer"
        with pytest.raises(StaleDataError):
            await contract_session.flush()


class TestCreatedByUpdatedByMixins:
    async def test_both_default_to_none(self, contract_session: AsyncSession) -> None:
        widget = ContractWidget(name="a")
        assert widget.created_by is None
        assert widget.updated_by is None

    async def test_both_accept_an_opaque_uuid_with_no_fk_enforcement(
        self, contract_session: AsyncSession
    ) -> None:
        actor_id = uuid.uuid4()
        widget = ContractWidget(name="a", created_by=actor_id, updated_by=actor_id)
        contract_session.add(widget)
        await contract_session.flush()  # does not raise — no FK constraint exists
        assert widget.created_by == actor_id
