"""`admin.audit_entry` against real PostgreSQL — A64-024.8.

`tests/unit/test_admin_audit.py` covers what the services decide, over
in-memory storage. What it cannot cover is what only a real database has,
and it is precisely the guarantee this table exists for:

    append-only     `UPDATE`, `DELETE` and `TRUNCATE` are refused by a
                    trigger, so the record of what administrators did
                    survives a repository bug, a migration and an operator
                    with `psql`
    the keyset      `(created_at, id)` orders entries written in the same
                    millisecond totally, so a page neither repeats nor skips
    the check       `actor_id` and `actor_type` cannot disagree, even for a
                    row that did not go through the domain

None of the four is falsifiable against a dictionary — a fake can model them,
and a model that agrees with itself proves nothing.

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import delete, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.identifiers import generate_uuid7
from app.modules.admin.application.ports import AuditEntryFilters
from app.modules.admin.domain.audit import (
    AuditAction,
    AuditActorType,
    AuditEntry,
    AuditOutcome,
    AuditSubjectType,
)
from app.modules.admin.infrastructure.models import AuditEntryModel
from app.modules.admin.infrastructure.repositories import SqlAlchemyAuditEntryRepository

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def entries(contract_session: AsyncSession) -> SqlAlchemyAuditEntryRepository:
    return SqlAlchemyAuditEntryRepository(contract_session)


def _entry(
    *,
    actor_id: UUID | None = None,
    action: AuditAction = AuditAction.ROLE_GRANTED,
    subject_ref: str | None = None,
    created_at: datetime = NOW,
) -> AuditEntry:
    administrator = actor_id is not None
    return AuditEntry(
        id=generate_uuid7(),
        actor_type=(AuditActorType.ADMINISTRATOR if administrator else AuditActorType.OPERATOR),
        actor_id=actor_id,
        action=action,
        subject_type=AuditSubjectType.ACCOUNT,
        subject_ref=subject_ref or str(generate_uuid7()),
        outcome=AuditOutcome.SUCCEEDED,
        created_at=created_at,
        correlation_id="corr-1",
        before={},
        after={"role": "admin"},
    )


class TestTheTableIsAppendOnly:
    """The guarantee, asserted where it lives.

    Not "the repository offers no update" — that is the layer above, and a
    layer can be bypassed. These assert that the *database* refuses, which
    is what makes the trail evidence rather than a convention.
    """

    async def test_an_update_is_refused(
        self, entries: SqlAlchemyAuditEntryRepository, contract_session: AsyncSession
    ) -> None:
        written = await entries.append(_entry())

        with pytest.raises(DBAPIError, match="append-only"):
            await contract_session.execute(
                update(AuditEntryModel)
                .where(AuditEntryModel.id == written.id)
                .values(outcome=AuditOutcome.FAILED)
            )

    async def test_a_delete_is_refused(
        self, entries: SqlAlchemyAuditEntryRepository, contract_session: AsyncSession
    ) -> None:
        written = await entries.append(_entry())

        with pytest.raises(DBAPIError, match="append-only"):
            await contract_session.execute(
                delete(AuditEntryModel).where(AuditEntryModel.id == written.id)
            )

    async def test_a_truncate_is_refused(
        self, entries: SqlAlchemyAuditEntryRepository, contract_session: AsyncSession
    ) -> None:
        """The statement a row-level trigger would not see.

        `TRUNCATE` fires no row trigger, so guarding `UPDATE` and `DELETE`
        alone would leave the single statement that empties the entire
        trail unguarded — and it is the one an attacker covering their
        tracks would reach for first.
        """
        await entries.append(_entry())

        with pytest.raises(DBAPIError, match="append-only"):
            await contract_session.execute(text("TRUNCATE admin.audit_entry"))


class TestTheRowCannotContradictItself:
    async def test_an_administrator_row_without_an_account_is_refused(
        self, contract_session: AsyncSession
    ) -> None:
        """The check constraint, not the dataclass.

        `AuditEntry.__post_init__` already refuses this, and that is the
        copy the application keeps. This is the copy the database keeps for
        a row that arrived some other way — a migration, a backfill, or a
        future writer that skipped the domain.
        """
        contract_session.add(
            AuditEntryModel(
                id=generate_uuid7(),
                actor_type=AuditActorType.ADMINISTRATOR,
                actor_id=None,
                action=AuditAction.ROLE_GRANTED,
                subject_type=AuditSubjectType.ACCOUNT,
                subject_ref=str(generate_uuid7()),
                outcome=AuditOutcome.SUCCEEDED,
                before={},
                after={},
                correlation_id=None,
                created_at=NOW,
            )
        )
        with pytest.raises(IntegrityError, match="ck_audit_entry__actor_matches_type"):
            await contract_session.flush()


class TestTheEntryRoundTrips:
    async def test_the_json_slices_and_the_instant_survive_storage(
        self, entries: SqlAlchemyAuditEntryRepository, contract_session: AsyncSession
    ) -> None:
        """`jsonb` and `timestamptz`, both of which can lose information.

        A naive datetime column would return the instant without its zone
        and every comparison afterwards would be wrong by the server's
        offset.
        """
        written = await entries.append(_entry(actor_id=generate_uuid7(), subject_ref="account-1"))
        await contract_session.commit()

        stored = await contract_session.get(AuditEntryModel, written.id)
        assert stored is not None
        assert stored.after == {"role": "admin"}
        assert stored.before == {}
        assert stored.created_at == NOW
        assert stored.created_at.tzinfo is not None
        assert stored.correlation_id == "corr-1"


class TestTheKeysetIsTotal:
    async def test_a_page_neither_repeats_nor_skips_entries_sharing_an_instant(
        self, entries: SqlAlchemyAuditEntryRepository
    ) -> None:
        """The reason the cursor carries `id` as well as `created_at`.

        Five entries at the **same** instant is not contrived — a grant and
        its revocation in one script land inside the same millisecond — and
        a cursor on time alone would silently drop or duplicate rows at
        every page boundary.
        """
        for _ in range(5):
            await entries.append(_entry(created_at=NOW))

        seen: list[UUID] = []
        cursor: str | None = None
        for _ in range(3):
            page = await entries.page(filters=AuditEntryFilters(), limit=2, cursor=cursor)
            seen.extend(entry.id for entry in page.entries)
            cursor = page.next_cursor
            if cursor is None:
                break

        assert len(seen) == 5
        assert len(set(seen)) == 5
        assert cursor is None

    async def test_the_page_is_newest_first(self, entries: SqlAlchemyAuditEntryRepository) -> None:
        oldest = await entries.append(_entry(created_at=NOW - timedelta(minutes=5)))
        newest = await entries.append(_entry(created_at=NOW))

        page = await entries.page(filters=AuditEntryFilters(), limit=10, cursor=None)

        assert [entry.id for entry in page.entries] == [newest.id, oldest.id]

    async def test_each_filter_narrows_on_its_own_index(
        self, entries: SqlAlchemyAuditEntryRepository
    ) -> None:
        """The three filters the port offers, each with an index behind it.

        Asserted together because what matters is that they compose: an
        operator narrowing by actor *and* by action must not get the union.
        """
        actor, subject = generate_uuid7(), str(generate_uuid7())
        await entries.append(_entry(actor_id=actor, action=AuditAction.ROLE_GRANTED))
        await entries.append(
            _entry(actor_id=actor, action=AuditAction.ROLE_REVOKED, subject_ref=subject)
        )
        await entries.append(_entry(action=AuditAction.ROLE_GRANTED))

        by_actor = await entries.page(
            filters=AuditEntryFilters(actor_id=actor), limit=10, cursor=None
        )
        assert len(by_actor.entries) == 2

        by_action = await entries.page(
            filters=AuditEntryFilters(action=AuditAction.ROLE_REVOKED), limit=10, cursor=None
        )
        assert len(by_action.entries) == 1

        by_subject = await entries.page(
            filters=AuditEntryFilters(subject_type=AuditSubjectType.ACCOUNT, subject_ref=subject),
            limit=10,
            cursor=None,
        )
        assert [entry.subject_ref for entry in by_subject.entries] == [subject]

        both = await entries.page(
            filters=AuditEntryFilters(actor_id=actor, action=AuditAction.ROLE_GRANTED),
            limit=10,
            cursor=None,
        )
        assert len(both.entries) == 1
