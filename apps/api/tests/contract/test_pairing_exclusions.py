"""`blocked_pairs_among` against real PostgreSQL — A64-015.3 §5.

The batch, all-pairs block read the pairing scan runs, and the two things
only a database can show: that it is **one statement** whatever the batch
size, and that it is confined to the batch rather than loading either
player's whole block list.

Written against the repository rather than through the API, because the
subject is a query. The published port (`friends.public.PairingExclusions`)
and the service behind it are straight delegation — asserted in
`tests/unit/test_pairing_service.py`, which is where the *rule* lives.

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID

import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.identifiers import generate_uuid7
from app.modules.friends.application.services import PairingExclusionService
from app.modules.friends.domain.block import Block
from app.modules.friends.infrastructure.repositories import SqlAlchemyBlockedPlayerRepository

ALICE = generate_uuid7()
BOB = generate_uuid7()
CAROL = generate_uuid7()
DAVE = generate_uuid7()


@pytest_asyncio.fixture
async def blocks(
    contract_session: AsyncSession,
) -> AsyncIterator[SqlAlchemyBlockedPlayerRepository]:
    yield SqlAlchemyBlockedPlayerRepository(contract_session)


#: `Block.created_at` defaults to a naive `datetime.min`, which `UtcDateTime`
#: refuses (DM-14). `BlockingService` supplies the instant from the injected
#: clock; these tests are below that service, so they supply their own.
PLACED_AT = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)


async def _block(
    repository: SqlAlchemyBlockedPlayerRepository, blocker: UUID, blocked: UUID
) -> None:
    await repository.add(Block(blocker_id=blocker, blocked_id=blocked, created_at=PLACED_AT))


class TestTheBatchRead:
    async def test_a_pool_with_no_blocks_returns_nothing(
        self, blocks: SqlAlchemyBlockedPlayerRepository
    ) -> None:
        """The common case, and it must allocate nothing per candidate —
        players with no exclusions are absent rather than mapped to an
        empty set."""
        assert await blocks.blocked_pairs_among([ALICE, BOB, CAROL]) == {}

    async def test_a_block_excludes_the_pair_in_both_directions(
        self, blocks: SqlAlchemyBlockedPlayerRepository
    ) -> None:
        """BL-1 makes the block one-directional; BL-2's pairing consequence
        is symmetric, and the row is stored only one way."""
        await _block(blocks, ALICE, BOB)

        pairs = await blocks.blocked_pairs_among([ALICE, BOB])

        assert pairs[ALICE] == frozenset({BOB})
        assert pairs[BOB] == frozenset({ALICE})

    async def test_a_block_against_somebody_outside_the_batch_is_ignored(
        self, blocks: SqlAlchemyBlockedPlayerRepository
    ) -> None:
        """The half a per-player read would get wrong: `blocked_ids_for`
        loads a player's whole block list, most of which is irrelevant to
        any one pool. This never leaves the database."""
        await _block(blocks, ALICE, DAVE)

        assert await blocks.blocked_pairs_among([ALICE, BOB, CAROL]) == {}

    async def test_only_the_blocked_pair_is_excluded(
        self, blocks: SqlAlchemyBlockedPlayerRepository
    ) -> None:
        """A block between two candidates must not make either unpairable
        with everybody else — otherwise one block would empty a pool."""
        await _block(blocks, ALICE, BOB)

        pairs = await blocks.blocked_pairs_among([ALICE, BOB, CAROL])

        assert CAROL not in pairs

    async def test_several_blocks_are_all_reported(
        self, blocks: SqlAlchemyBlockedPlayerRepository
    ) -> None:
        await _block(blocks, ALICE, BOB)
        await _block(blocks, CAROL, ALICE)

        pairs = await blocks.blocked_pairs_among([ALICE, BOB, CAROL])

        assert pairs[ALICE] == frozenset({BOB, CAROL})

    async def test_a_single_candidate_touches_the_database_at_all(
        self, blocks: SqlAlchemyBlockedPlayerRepository
    ) -> None:
        """One player cannot be a pair, so the query is skipped entirely —
        the guard that keeps a scan on a nearly-empty pool free."""
        await _block(blocks, ALICE, BOB)

        assert await blocks.blocked_pairs_among([ALICE]) == {}

    async def test_an_empty_batch_returns_nothing(
        self, blocks: SqlAlchemyBlockedPlayerRepository
    ) -> None:
        assert await blocks.blocked_pairs_among([]) == {}

    async def test_a_duplicated_candidate_does_not_break_the_read(
        self, blocks: SqlAlchemyBlockedPlayerRepository
    ) -> None:
        """Defensive: the scan cannot produce one, because QT-1 gives a
        player one live ticket — but a query that broke on it would break
        at the worst moment, which is when that invariant is violated."""
        await _block(blocks, ALICE, BOB)

        pairs = await blocks.blocked_pairs_among([ALICE, ALICE, BOB])

        assert pairs[ALICE] == frozenset({BOB})


class TestItIsOneStatement:
    """§5's "no N+1 block queries", asserted rather than asserted-about.

    The count is taken from SQLAlchemy's own cursor events, so it counts
    what was actually sent rather than what the code appears to send.
    """

    async def test_a_batch_of_twenty_issues_one_query(
        self, blocks: SqlAlchemyBlockedPlayerRepository, contract_session: AsyncSession
    ) -> None:
        candidates = [generate_uuid7() for _ in range(20)]
        await _block(blocks, candidates[0], candidates[1])
        await contract_session.flush()

        with await _counting(contract_session) as statements:
            await blocks.blocked_pairs_among(candidates)

        assert statements.count == 1

    async def test_the_count_does_not_grow_with_the_batch(
        self, blocks: SqlAlchemyBlockedPlayerRepository, contract_session: AsyncSession
    ) -> None:
        """The property that matters: doubling the pool must not double the
        round trips."""
        small = [generate_uuid7() for _ in range(5)]
        large = [generate_uuid7() for _ in range(100)]

        with await _counting(contract_session) as first:
            await blocks.blocked_pairs_among(small)
        with await _counting(contract_session) as second:
            await blocks.blocked_pairs_among(large)

        assert first.count == second.count == 1


class TestThePublishedPort:
    async def test_the_service_delegates_unchanged(
        self, blocks: SqlAlchemyBlockedPlayerRepository
    ) -> None:
        """`PairingExclusionService` is the object `matchmaking` is handed.
        One test that it is wired to the same query, because a seam that
        silently returned `{}` would disable BL-2 in production and pass
        every unit test."""
        await _block(blocks, ALICE, BOB)

        pairs = await PairingExclusionService(blocks).blocked_pairs_among([ALICE, BOB])

        assert pairs[ALICE] == frozenset({BOB})


async def _counting(session: AsyncSession) -> "_CountingStatements":
    """A statement counter over `session`'s connection.

    `await`ed rather than constructed, because obtaining the underlying
    connection is itself IO — doing it inside `__enter__` reaches the driver
    from a synchronous frame and SQLAlchemy raises `MissingGreenlet`. The
    await establishes it first; the context manager then only attaches a
    listener.
    """
    await session.connection()
    return _CountingStatements(session)


class _CountingStatements:
    """Counts SQL statements executed on one session, as a context manager.

    Hooks `before_cursor_execute` on the session's own connection, which is
    the only vantage point that sees what the driver was actually asked to
    run — a counter around the repository method would count calls rather
    than queries, which is the thing under test.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self.count = 0

    def __enter__(self) -> "_CountingStatements":
        self._connection = self._session.sync_session.connection()
        event.listen(self._connection, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *_: object) -> None:
        event.remove(self._connection, "before_cursor_execute", self._record)

    def _record(self, *_: object, **__: object) -> None:
        self.count += 1
