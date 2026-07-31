"""A bounded, typed predicate builder — filtering that stays inside the
lines repositories.md §4 draws around "a generic filter or query-builder
API": no arbitrary `**kwargs`, no string-keyed field lookup, no operators
beyond a fixed enum. A caller must import the model's actual column
object, so what can be filtered on is exactly a model's own typed
attributes — never a string that could reference a column that doesn't
exist, or one from a different table entirely.

**Use this only for the case RP-03 names explicitly: "admin search over
small, bounded result sets where jump-to-page is a genuine requirement."**
A named business query — "matches this player is currently in" — is
`select(Match).where(Match.status == MatchStatus.ACTIVE)` directly, in a
method called `find_active_matches_for_player`, not a `Filter` list
assembled by a caller. The moment a filter's meaning has a name, give it
one; this module is for the moment it genuinely doesn't yet, because the
caller is a search box, not a use case.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from sqlalchemy import ColumnElement, Select
from sqlalchemy.orm import InstrumentedAttribute


class FilterOp(StrEnum):
    """A closed set of comparisons — deliberately not "any SQL
    expression". Extending this list is a reviewed decision; passing an
    arbitrary callable was never an option to begin with.
    """

    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    IN = "in"
    LIKE = "like"
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"


@dataclass(frozen=True, slots=True)
class Filter:
    """One named, typed predicate. `column` is a real model attribute —
    `Model.some_column`, not `"some_column"` — so a typo fails at the type
    checker, not with a runtime `AttributeError` three layers into a
    request.
    """

    column: InstrumentedAttribute[Any]
    op: FilterOp
    value: Any = None


def apply_filters[ModelT](
    statement: Select[tuple[ModelT]], filters: list[Filter]
) -> Select[tuple[ModelT]]:
    for one_filter in filters:
        statement = statement.where(_predicate(one_filter))
    return statement


def _predicate(f: Filter) -> ColumnElement[bool]:
    # `f.value: Any` is what lets one `Filter` accept a scalar, a list (for
    # `IN`), or nothing (`IS_NULL`) — but it also means every branch below
    # types as `Any` to mypy, since SQLAlchemy's comparison operators
    # propagate the `Any` operand. The `cast` states what is true at
    # runtime for every branch: a boolean SQL predicate.
    match f.op:
        case FilterOp.EQ:
            expr = f.column == f.value
        case FilterOp.NE:
            expr = f.column != f.value
        case FilterOp.GT:
            expr = f.column > f.value
        case FilterOp.GTE:
            expr = f.column >= f.value
        case FilterOp.LT:
            expr = f.column < f.value
        case FilterOp.LTE:
            expr = f.column <= f.value
        case FilterOp.IN:
            expr = f.column.in_(f.value)
        case FilterOp.LIKE:
            expr = f.column.like(f.value)
        case FilterOp.IS_NULL:
            expr = f.column.is_(None)
        case FilterOp.IS_NOT_NULL:
            expr = f.column.is_not(None)
    return cast("ColumnElement[bool]", expr)
