"""`app.repositories.pagination`, against real PostgreSQL — proving the
keyset comparison (`tuple_(...) > tuple_(...)`) is valid SQL PostgreSQL
actually executes, which a unit test over the query-construction alone
cannot.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import CursorPageParams, OffsetPageParams
from app.repositories.base import BaseRepository
from app.repositories.pagination import paginate_cursor, paginate_offset
from tests.contract._models import ContractWidget


@pytest.fixture
def repo(contract_session: AsyncSession) -> BaseRepository[ContractWidget, object]:
    return BaseRepository(contract_session, ContractWidget)


async def _seed(session: AsyncSession, count: int) -> None:
    for i in range(count):
        session.add(ContractWidget(name=f"widget-{i}", rank=i))
    await session.flush()


class TestPaginateOffset:
    async def test_first_page(
        self, contract_session: AsyncSession, repo: BaseRepository[ContractWidget, object]
    ) -> None:
        await _seed(contract_session, 5)
        items, page = await paginate_offset(
            contract_session, repo.select(), OffsetPageParams(limit=2, offset=0)
        )
        assert len(items) == 2
        assert page.total == 5
        assert page.has_more is True

    async def test_last_page_has_more_false(
        self, contract_session: AsyncSession, repo: BaseRepository[ContractWidget, object]
    ) -> None:
        await _seed(contract_session, 5)
        items, page = await paginate_offset(
            contract_session, repo.select(), OffsetPageParams(limit=2, offset=4)
        )
        assert len(items) == 1
        assert page.has_more is False

    async def test_empty_table(
        self, contract_session: AsyncSession, repo: BaseRepository[ContractWidget, object]
    ) -> None:
        items, page = await paginate_offset(
            contract_session, repo.select(), OffsetPageParams(limit=10, offset=0)
        )
        assert items == []
        assert page.total == 0
        assert page.has_more is False


class TestPaginateCursor:
    async def test_first_page_orders_by_rank_then_id(
        self, contract_session: AsyncSession, repo: BaseRepository[ContractWidget, object]
    ) -> None:
        await _seed(contract_session, 5)
        items, page = await paginate_cursor(
            contract_session,
            repo.select(),
            CursorPageParams(limit=2),
            order_column=ContractWidget.rank,
            id_column=ContractWidget.id,
        )
        assert [w.rank for w in items] == [0, 1]
        assert page.has_more is True
        assert page.next_cursor is not None

    async def test_second_page_continues_after_the_cursor(
        self, contract_session: AsyncSession, repo: BaseRepository[ContractWidget, object]
    ) -> None:
        await _seed(contract_session, 5)
        first_items, first_page = await paginate_cursor(
            contract_session,
            repo.select(),
            CursorPageParams(limit=2),
            order_column=ContractWidget.rank,
            id_column=ContractWidget.id,
        )
        assert first_page.next_cursor is not None

        second_items, second_page = await paginate_cursor(
            contract_session,
            repo.select(),
            CursorPageParams(limit=2, cursor=first_page.next_cursor),
            order_column=ContractWidget.rank,
            id_column=ContractWidget.id,
        )
        assert [w.rank for w in second_items] == [2, 3]
        assert {w.id for w in first_items}.isdisjoint({w.id for w in second_items})

    async def test_walking_every_page_visits_every_row_exactly_once(
        self, contract_session: AsyncSession, repo: BaseRepository[ContractWidget, object]
    ) -> None:
        await _seed(contract_session, 7)
        seen: list[int] = []
        cursor: str | None = None
        for _ in range(10):  # bounded — a real bug here must not hang the suite
            items, page = await paginate_cursor(
                contract_session,
                repo.select(),
                CursorPageParams(limit=3, cursor=cursor),
                order_column=ContractWidget.rank,
                id_column=ContractWidget.id,
            )
            seen.extend(w.rank for w in items)
            if not page.has_more:
                break
            cursor = page.next_cursor

        assert seen == list(range(7))

    async def test_final_page_has_no_next_cursor(
        self, contract_session: AsyncSession, repo: BaseRepository[ContractWidget, object]
    ) -> None:
        await _seed(contract_session, 2)
        items, page = await paginate_cursor(
            contract_session,
            repo.select(),
            CursorPageParams(limit=10),
            order_column=ContractWidget.rank,
            id_column=ContractWidget.id,
        )
        assert len(items) == 2
        assert page.has_more is False
        assert page.next_cursor is None

    async def test_an_invalid_cursor_raises(
        self, contract_session: AsyncSession, repo: BaseRepository[ContractWidget, object]
    ) -> None:
        with pytest.raises(ValueError, match="invalid pagination cursor"):
            await paginate_cursor(
                contract_session,
                repo.select(),
                CursorPageParams(cursor="not-a-real-cursor"),
                order_column=ContractWidget.rank,
                id_column=ContractWidget.id,
            )
