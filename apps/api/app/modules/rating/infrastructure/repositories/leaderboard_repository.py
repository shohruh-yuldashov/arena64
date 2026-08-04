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

from sqlalchemy import Float, cast, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.rating.domain.keys import RatingKey
from app.modules.rating.domain.player_rating import PROVISIONAL_GAMES_THRESHOLD
from app.modules.rating.infrastructure.models import PlayerRatingModel
from app.modules.rating.public.leaderboard import (
    LeaderboardCursor,
    LeaderboardEntry,
    LeaderboardPage,
)

#: The most rows one page may return — §10.5's "every list endpoint
#: paginates". A caller asking for more gets this; a caller asking for none
#: gets the default.
MAX_PAGE_SIZE: Final = 200

DEFAULT_PAGE_SIZE: Final = 50


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


__all__ = ["DEFAULT_PAGE_SIZE", "MAX_PAGE_SIZE", "SqlAlchemyLeaderboardReader"]
