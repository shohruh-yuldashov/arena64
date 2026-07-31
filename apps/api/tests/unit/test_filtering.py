"""app.repositories.filtering — pure query construction; only the shape of
the resulting SQL is checked, no database needed. Kept independent of
`tests/contract/`'s model on purpose, so this suite never needs a
database.
"""

import uuid

from sqlalchemy import Select, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.repositories.filtering import Filter, FilterOp, apply_filters


class _Base(DeclarativeBase):
    pass


class _Widget(_Base):
    __tablename__ = "widget"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    name: Mapped[str]
    rank: Mapped[int]


def _sql(statement: Select[tuple[_Widget]]) -> str:
    return str(statement.compile(compile_kwargs={"literal_binds": True}))


class TestApplyFilters:
    def test_no_filters_is_a_no_op(self) -> None:
        statement = apply_filters(select(_Widget), [])
        assert "WHERE" not in _sql(statement)

    def test_eq(self) -> None:
        statement = apply_filters(select(_Widget), [Filter(_Widget.name, FilterOp.EQ, "a")])
        assert "widget.name = 'a'" in _sql(statement)

    def test_ne(self) -> None:
        statement = apply_filters(select(_Widget), [Filter(_Widget.name, FilterOp.NE, "a")])
        assert "widget.name != 'a'" in _sql(statement)

    def test_gt(self) -> None:
        statement = apply_filters(select(_Widget), [Filter(_Widget.rank, FilterOp.GT, 5)])
        assert "widget.rank > 5" in _sql(statement)

    def test_gte(self) -> None:
        statement = apply_filters(select(_Widget), [Filter(_Widget.rank, FilterOp.GTE, 5)])
        assert "widget.rank >= 5" in _sql(statement)

    def test_lt(self) -> None:
        statement = apply_filters(select(_Widget), [Filter(_Widget.rank, FilterOp.LT, 5)])
        assert "widget.rank < 5" in _sql(statement)

    def test_lte(self) -> None:
        statement = apply_filters(select(_Widget), [Filter(_Widget.rank, FilterOp.LTE, 5)])
        assert "widget.rank <= 5" in _sql(statement)

    def test_in(self) -> None:
        statement = apply_filters(select(_Widget), [Filter(_Widget.rank, FilterOp.IN, [1, 2, 3])])
        sql = _sql(statement)
        assert "widget.rank IN" in sql
        assert "1, 2, 3" in sql

    def test_like(self) -> None:
        statement = apply_filters(select(_Widget), [Filter(_Widget.name, FilterOp.LIKE, "a%")])
        assert "widget.name LIKE 'a%'" in _sql(statement)

    def test_is_null(self) -> None:
        statement = apply_filters(select(_Widget), [Filter(_Widget.name, FilterOp.IS_NULL)])
        assert "widget.name IS NULL" in _sql(statement)

    def test_is_not_null(self) -> None:
        statement = apply_filters(select(_Widget), [Filter(_Widget.name, FilterOp.IS_NOT_NULL)])
        assert "widget.name IS NOT NULL" in _sql(statement)

    def test_multiple_filters_combine_with_and(self) -> None:
        statement = apply_filters(
            select(_Widget),
            [
                Filter(_Widget.name, FilterOp.EQ, "a"),
                Filter(_Widget.rank, FilterOp.GT, 5),
            ],
        )
        sql = _sql(statement)
        assert "widget.name = 'a'" in sql
        assert "widget.rank > 5" in sql
        assert " AND " in sql
