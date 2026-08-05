"""`LeaderboardReader` over `rating.player_rating` — A64-017.4.

One query per page, served by `ix_player_rating__standings`. No projection
table, no cache, and nothing to invalidate: see
`rating.public.leaderboard` on why the source relation *is* the read model.

## The keyset predicate

    (rating, -deviation, -player_id) < (cursor.rating, ...)

expressed as the row-wise comparison PostgreSQL can serve from the index
directly. Writing it as nested `OR`s instead — `rating < c OR (rating = c
AND deviation > d) OR (...)` — is the same logic and the planner does not
always reduce it to an index range, so the tuple form is a performance
property rather than a style choice.

The two descending components are negated so the comparison is a single
`<` on a tuple that sorts the same way the index does. That is why
`deviation` and `player_id` are negated here and ascending in the ordering:
one `ORDER BY` and one `WHERE`, agreeing by construction.

## Why `limit + 1`

The page asks for one more row than it returns. If it comes back, there is
a next page and the cursor is built from the *last returned* row; if it does
not, `next_cursor` is `None`. Deciding from the count alone would send a
reader back for an empty page whenever a ladder's length is an exact
multiple of the limit.
"""

from typing import Final
from uuid import UUID

from sqlalchemy import Float, cast, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.rating.domain.keys import RatingKey
from app.modules.rating.domain.player_rating import PROVISIONAL_GAMES_THRESHOLD
from app.modules.rating.infrastructure.models import PlayerRatingModel
from app.modules.rating.public.leaderboard import (
    LeaderboardCursor,
    LeaderboardEntry,
    LeaderboardNeighbourhood,
    LeaderboardPage,
)

#: The most rows one page may return — §10.5's "every list endpoint
#: paginates". A caller asking for more gets this; a caller asking for none
#: gets the default.
MAX_PAGE_SIZE: Final = 200

DEFAULT_PAGE_SIZE: Final = 50

#: How many rows either side of a player `around` returns by default, and
#: the ceiling on it. Bounded like every other read here (§10.5): the result
#: is at most `2 * span + 1` rows however large the ladder grows.
DEFAULT_SPAN: Final = 5

MAX_SPAN: Final = 25


class SqlAlchemyLeaderboardReader:
    """`LeaderboardReader` over one session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def page(
        self,
        key: RatingKey,
        *,
        after: LeaderboardCursor | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
    ) -> LeaderboardPage:
        """One page of standings, highest first."""
        wanted = max(1, min(limit, MAX_PAGE_SIZE))

        statement = (
            select(PlayerRatingModel)
            .where(
                PlayerRatingModel.variant == key.variant,
                PlayerRatingModel.speed_class == key.speed_class,
            )
            .order_by(
                PlayerRatingModel.rating_value.desc(),
                PlayerRatingModel.rating_deviation.asc(),
                PlayerRatingModel.player_id.asc(),
            )
            .limit(wanted + 1)
        )

        if after is not None:
            # The parentheses are load-bearing: `|` binds tighter than the
            # tuple comparison under SQLAlchemy's operator overloading, so
            # without them the `OR` is folded into the *right-hand side* of
            # the `<` and the predicate compares a tuple against a boolean.
            statement = statement.where(
                (
                    tuple_(
                        PlayerRatingModel.rating_value,
                        -PlayerRatingModel.rating_deviation,
                    )
                    < tuple_(cast(after.rating, Float), cast(-after.deviation, Float))
                )
                | _same_rank_after(after)
            )

        rows = list(await self._session.scalars(statement))
        page, has_more = rows[:wanted], len(rows) > wanted

        entries = [_to_entry(row) for row in page]
        return LeaderboardPage(
            entries=entries,
            next_cursor=_cursor_of(entries[-1]) if has_more and entries else None,
        )

    async def around(
        self,
        player_id: UUID,
        *,
        key: RatingKey,
        span: int = DEFAULT_SPAN,
    ) -> LeaderboardNeighbourhood | None:
        """This player's rank and the rows either side — A64-020.0A.

        Four statements, and each is one index scan:

            the player's own row     primary key
            how many sort above      a COUNT over the same predicate the
                                     page uses, which is the rank
            the rows below           one bounded scan down
            the rows above           one bounded scan up

        Four however large the ladder is and however wide the span —
        measured, not assumed.

        The rank is a `COUNT`, not a stored column: it is a property of the
        whole relation, so storing it would make every rating update rewrite
        an unbounded number of rows — and a stale rank is worse than none.

        The two neighbour scans reuse the page's own ordering predicate
        rather than re-deriving one, so "the row after mine" here and on a
        page are the same row.
        """
        wanted = max(1, min(span, MAX_SPAN))

        own = await self._session.scalar(
            select(PlayerRatingModel).where(
                PlayerRatingModel.player_id == player_id,
                PlayerRatingModel.variant == key.variant,
                PlayerRatingModel.speed_class == key.speed_class,
            )
        )
        if own is None:
            # Not on this ladder. A legitimate answer rather than an error —
            # an unrated player has no position, and `RatingSnapshot.unrated`
            # is what describes them.
            return None

        entry = _to_entry(own)
        better = await self._session.scalar(
            select(func.count())
            .select_from(PlayerRatingModel)
            .where(
                PlayerRatingModel.variant == key.variant,
                PlayerRatingModel.speed_class == key.speed_class,
                _sorts_above(entry),
            )
        )

        below = list(
            await self._session.scalars(
                select(PlayerRatingModel)
                .where(
                    PlayerRatingModel.variant == key.variant,
                    PlayerRatingModel.speed_class == key.speed_class,
                    _sorts_below(entry),
                )
                .order_by(
                    PlayerRatingModel.rating_value.desc(),
                    PlayerRatingModel.rating_deviation.asc(),
                    PlayerRatingModel.player_id.asc(),
                )
                .limit(wanted)
            )
        )
        above = list(
            await self._session.scalars(
                select(PlayerRatingModel)
                .where(
                    PlayerRatingModel.variant == key.variant,
                    PlayerRatingModel.speed_class == key.speed_class,
                    _sorts_above(entry),
                )
                # **Ascending**, so `LIMIT` takes the rows *nearest* this
                # player rather than the top of the ladder. Reversed below,
                # because a caller renders them best-first.
                .order_by(
                    PlayerRatingModel.rating_value.asc(),
                    PlayerRatingModel.rating_deviation.desc(),
                    PlayerRatingModel.player_id.desc(),
                )
                .limit(wanted)
            )
        )

        return LeaderboardNeighbourhood(
            rank=int(better or 0) + 1,
            entry=entry,
            above=[_to_entry(row) for row in reversed(above)],
            below=[_to_entry(row) for row in below],
        )


def _sorts_above(entry: LeaderboardEntry):  # type: ignore[no-untyped-def]
    """Rows that come **before** this one in the published order.

    The same three-key comparison `page` continues on, written once here so
    the rank, the neighbours and a page cannot disagree about which row is
    next.
    """
    return (
        tuple_(PlayerRatingModel.rating_value, -PlayerRatingModel.rating_deviation)
        > tuple_(cast(entry.rating, Float), cast(-entry.deviation, Float))
    ) | (
        (PlayerRatingModel.rating_value == entry.rating)
        & (PlayerRatingModel.rating_deviation == entry.deviation)
        & (PlayerRatingModel.player_id < entry.player_id)
    )


def _sorts_below(entry: LeaderboardEntry):  # type: ignore[no-untyped-def]
    """Rows that come **after** this one — the mirror of `_sorts_above`."""
    return (
        tuple_(PlayerRatingModel.rating_value, -PlayerRatingModel.rating_deviation)
        < tuple_(cast(entry.rating, Float), cast(-entry.deviation, Float))
    ) | (
        (PlayerRatingModel.rating_value == entry.rating)
        & (PlayerRatingModel.rating_deviation == entry.deviation)
        & (PlayerRatingModel.player_id > entry.player_id)
    )


def _same_rank_after(after: LeaderboardCursor):  # type: ignore[no-untyped-def]
    """The tie-break half of the keyset predicate.

    Two players on the same rating **and** the same deviation are ordered by
    `player_id`, and this is the clause that continues past them. Separate
    from the tuple comparison above because `player_id` is a UUID: including
    it in the same tuple would compare it against two floats.
    """
    return (
        (PlayerRatingModel.rating_value == after.rating)
        & (PlayerRatingModel.rating_deviation == after.deviation)
        & (PlayerRatingModel.player_id > after.player_id)
    )


def _to_entry(row: PlayerRatingModel) -> LeaderboardEntry:
    return LeaderboardEntry(
        player_id=row.player_id,
        rating=row.rating_value,
        deviation=row.rating_deviation,
        games_played=row.games_played,
        # Derived here as it is everywhere: a stored flag is a second copy
        # of what the counter says, and the copy is what goes stale.
        is_provisional=row.games_played < PROVISIONAL_GAMES_THRESHOLD,
    )


def _cursor_of(entry: LeaderboardEntry) -> LeaderboardCursor:
    return LeaderboardCursor(
        rating=entry.rating, deviation=entry.deviation, player_id=entry.player_id
    )


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "DEFAULT_SPAN",
    "MAX_PAGE_SIZE",
    "MAX_SPAN",
    "SqlAlchemyLeaderboardReader",
]
