"""Pagination over a SQLAlchemy `Select` — the execution half of
`app.core.pagination`'s DTOs, which describe the *shape* of a page. This
module describes how to *produce* one. Split from
`app.repositories.base` because pagination is orthogonal to CRUD: a
paginated query is still just a `Select`, whichever method produced it.
"""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import Select, cast, func, literal, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.core.pagination import (
    CursorPageInfo,
    CursorPageParams,
    OffsetPageParams,
    PageInfo,
    decode_cursor,
    encode_cursor,
)


async def paginate_offset[ModelT](
    session: AsyncSession,
    statement: Select[tuple[ModelT]],
    params: OffsetPageParams,
) -> tuple[Sequence[ModelT], PageInfo]:
    """repositories.md RP-03's documented exception: offset pagination for
    small, bounded, jump-to-page listings. Never use this for a collection
    that grows without bound — see `paginate_cursor`, the default.
    """
    total = await session.scalar(select(func.count()).select_from(statement.subquery()))
    total = total or 0

    page_statement = statement.limit(params.limit).offset(params.offset)
    items = (await session.scalars(page_statement)).all()

    return items, PageInfo(
        total=total,
        limit=params.limit,
        offset=params.offset,
        has_more=params.offset + len(items) < total,
    )


async def paginate_cursor[ModelT](
    session: AsyncSession,
    statement: Select[tuple[ModelT]],
    params: CursorPageParams,
    order_column: InstrumentedAttribute[Any],
    id_column: InstrumentedAttribute[Any],
) -> tuple[Sequence[ModelT], CursorPageInfo]:
    """Keyset pagination over `(order_column, id_column)` — RP-03's
    ordering key, generalised to any two-column tiebreak (an ordering
    value that is not itself unique, plus a unique column to break ties
    deterministically — e.g. `(created_at, id)`).

    A concrete, business-specific cursor shape (match history's
    `(created_at, match_id)`, per database.md's still-open keyset-key
    question) is the module's own decision; this is the mechanical
    primitive underneath it.
    """
    if params.cursor is not None:
        try:
            last_order_value, last_id_value = decode_cursor(params.cursor)
        except ValueError as exc:
            raise ValueError("invalid pagination cursor") from exc

        # `decode_cursor` hands back JSON-native values, so a `uuid` or a
        # `timestamptz` key both arrive as plain `str` (its docstring is
        # explicit that it does not reconstruct types).
        #
        # `cast(literal(value), column.type)` — **not** `literal(value,
        # type_=column.type)`. The latter runs the string through the
        # column's *Python-side* bind processor, and a processor written
        # for real values chokes on a string: A64-010's contract suite hit
        # exactly this, with `UtcDateTime.process_bind_param` raising
        # `AttributeError: 'str' object has no attribute 'tzinfo'` on a
        # `(created_at, id)` keyset. It went unnoticed in A64-009 only
        # because that suite's ordering key was an integer, which needs no
        # processing, and SQLAlchemy's `Uuid` happens to accept strings.
        #
        # A SQL-level `CAST` sidesteps Python bind processing entirely and
        # lets PostgreSQL parse its own literal formats, which it does for
        # every type a keyset key could reasonably be.
        last_values = tuple_(
            cast(literal(last_order_value), order_column.type),
            cast(literal(last_id_value), id_column.type),
        )
        statement = statement.where(tuple_(order_column, id_column) > last_values)

    # Over-fetch by one to learn whether a next page exists without a
    # separate count query — RP-03's whole point is avoiding a count that
    # gets slower the deeper a page goes.
    statement = statement.order_by(order_column, id_column).limit(params.limit + 1)
    rows = list((await session.scalars(statement)).all())

    has_more = len(rows) > params.limit
    page_rows = rows[: params.limit]

    next_cursor: str | None = None
    if has_more and page_rows:
        last_row = page_rows[-1]
        next_cursor = encode_cursor(
            getattr(last_row, order_column.key), getattr(last_row, id_column.key)
        )

    return page_rows, CursorPageInfo(next_cursor=next_cursor, has_more=has_more)
