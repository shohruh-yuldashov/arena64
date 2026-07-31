"""`app.repositories.base.BaseRepository`, against real PostgreSQL."""

import uuid

import pytest
from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.base import BaseRepository
from tests.contract._models import ContractWidget


@pytest.fixture
def repo(contract_session: AsyncSession) -> BaseRepository[ContractWidget, object]:
    return BaseRepository(contract_session, ContractWidget)


class TestGetById:
    async def test_returns_none_for_a_missing_id(
        self, repo: BaseRepository[ContractWidget, object]
    ) -> None:
        assert await repo.get_by_id(uuid.uuid4()) is None

    async def test_returns_the_row_for_a_real_id(
        self, repo: BaseRepository[ContractWidget, object]
    ) -> None:
        widget = await repo.add(ContractWidget(name="a"))
        found = await repo.get_by_id(widget.id)
        assert found is not None
        assert found.id == widget.id


class TestAdd:
    async def test_flushes_without_committing(
        self, contract_session: AsyncSession, repo: BaseRepository[ContractWidget, object]
    ) -> None:
        # repositories.md §4: a repository never commits. Proven here by
        # rolling back the session directly and confirming the row is
        # gone — if `add()` had committed, this rollback would do nothing.
        widget = await repo.add(ContractWidget(name="a"))
        widget_id = widget.id

        await contract_session.rollback()

        assert await contract_session.get(ContractWidget, widget_id) is None


class TestDelete:
    async def test_removes_the_row(self, repo: BaseRepository[ContractWidget, object]) -> None:
        widget = await repo.add(ContractWidget(name="a"))
        await repo.delete(widget)
        assert await repo.get_by_id(widget.id) is None


class TestSelectAndCount:
    def test_select_returns_a_statement_not_results(
        self, repo: BaseRepository[ContractWidget, object]
    ) -> None:
        assert isinstance(repo.select(), Select)

    async def test_count_reflects_inserted_rows(
        self, repo: BaseRepository[ContractWidget, object]
    ) -> None:
        await repo.add(ContractWidget(name="a"))
        await repo.add(ContractWidget(name="b"))
        assert await repo.count() == 2

    async def test_count_respects_a_filtered_statement(
        self, repo: BaseRepository[ContractWidget, object]
    ) -> None:
        await repo.add(ContractWidget(name="a"))
        await repo.add(ContractWidget(name="b"))

        filtered = repo.select().where(ContractWidget.name == "a")
        assert await repo.count(filtered) == 1
