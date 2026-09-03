"""The search query's **execution plan** — real PostgreSQL, real indexes,
real statistics.

One test, and it is the only one on the platform that asserts a plan rather
than a result. It exists because of a failure mode nothing else can catch.

`SqlAlchemyUserRepository.search` calls `users.search_normalise(...)` on the
username and the display name, and the two GIN indexes are built on exactly
those expressions. PostgreSQL uses an expression index only when the query's
expression matches the index's **character for character** after parsing. If
the two ever drift — a changed argument, a wrapper added on one side, a
migration and `search_ddl.py` disagreeing — the query keeps returning
correct results and quietly starts scanning the whole table.

That is invisible to every other test in this repository. `test_user_search_api.py`
passes either way; it runs against a handful of rows, where a sequential scan
is not merely acceptable but *faster*. The regression only shows up in
production, as a search that gets slower every month.

So this test seeds enough rows for the planner to have a real choice, runs
`ANALYZE`, and asserts that the plan reaches the index. A64-013.1: "reject
full-table scans" — this is the half of that requirement the input filter
cannot enforce.

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.users.public.search import UserSearchQuery

#: Enough rows that a sequential scan is the expensive option.
#:
#: Chosen by measurement rather than by feel: below roughly a thousand rows
#: PostgreSQL correctly prefers a scan whatever indexes exist, so a smaller
#: fixture would make this test pass by asserting nothing. Five thousand
#: inserts in one statement costs well under a second and leaves the
#: planner no reason to scan.
_SEEDED_ROWS = 5_000

#: What the plan must contain. `Bitmap Index Scan` is what a GIN index
#: produces; the index names are asserted too, because a plan that reached
#: *some* index — the unique one on `username_folded`, say — would satisfy a
#: looser check while the trigram indexes sat unused.
_EXPECTED_INDEXES = ("ix_user__username_search", "ix_user__display_name_search")


@pytest.fixture
def search_query() -> UserSearchQuery:
    """A term shaped like a real one: long enough to be selective, and
    matching nothing in the seeded data, so the planner's row estimate is
    the one a miss produces."""
    return UserSearchQuery(term="qxzvwyt", limit=20)


async def _seed(session: AsyncSession) -> None:
    """Bulk-inserts players in one statement, then `ANALYZE`s.

    `ANALYZE` is not optional. A freshly populated table has no statistics,
    and without them PostgreSQL falls back to defaults that make a
    sequential scan look cheap — so the assertion below would fail for a
    reason that has nothing to do with the query.

    Raw SQL rather than the repository, because this is fixture data whose
    only property that matters is its cardinality: five thousand round trips
    through the ORM would make this the slowest test in the suite to
    establish something one statement establishes.
    """
    await session.execute(
        text(
            """
            INSERT INTO users.user (id, username, email, password_hash, created_at)
            SELECT
                gen_random_uuid(),
                'planprobe' || g,
                'planprobe' || g || '@example.com',
                'argon2id$fake$notarealhash',
                now()
            FROM generate_series(1, :rows) g
            """
        ),
        {"rows": _SEEDED_ROWS},
    )
    # Inside the test's transaction, so it is rolled back with everything
    # else. `ANALYZE` sees uncommitted rows from its own transaction, which
    # is exactly what is needed here.
    await session.execute(text("ANALYZE users.user"))


async def _plan(session: AsyncSession, query: UserSearchQuery) -> str:
    """The planner's chosen plan for the **real** search statement.

    Built by asking the repository to compile its own query rather than by
    restating the SQL here. A hand-written copy would be a second expression
    that could match the indexes while the repository's did not — which is
    the precise failure this test exists to detect, reintroduced by the
    test.
    """
    from app.modules.users.infrastructure.repositories import SqlAlchemyUserRepository

    statement = SqlAlchemyUserRepository(session).build_search_statement(query)
    compiled = statement.compile(
        dialect=session.bind.dialect,
        compile_kwargs={"literal_binds": True},
    )

    rows: Any = await session.execute(text(f"EXPLAIN {compiled}"))
    return "\n".join(row[0] for row in rows)


class TestSearchUsesItsIndexes:
    async def test_the_query_does_not_sequentially_scan_the_user_table(
        self, contract_session: AsyncSession, search_query: UserSearchQuery
    ) -> None:
        """The assertion A64-013.1's "reject full-table scans" reduces to
        once the input filter has done its half.

        Both index names are required. A plan reaching only one of them
        would mean the other expression had drifted — the display-name half
        is the likelier casualty, since `search_normalise` is `STRICT` there
        and a nullable column is easy to wrap differently by accident.
        """
        await _seed(contract_session)

        plan = await _plan(contract_session, search_query)

        assert "Seq Scan" not in plan, plan
        for index in _EXPECTED_INDEXES:
            assert index in plan, f"{index} unused:\n{plan}"
