"""The SQLAlchemy adapter for `application.ports.PlayerRatingRepository`.

Two operations: read a rating by key, and write a rating with the adjustment
that produced it.

## `save` is an upsert plus an insert, in that order

The rating row may not exist — SPEC-RATING §7.5 creates it on the first
rated match — so the write is `INSERT … ON CONFLICT DO UPDATE`. The
adjustment is a plain `INSERT`, and its unique constraint is what makes the
pair exactly-once.

The **order matters**: the adjustment carries a foreign key to the rating,
so the rating must exist first. It also means a duplicate delivery upserts
the rating before the adjustment is refused — which sounds like a bug and is
not, because the upsert writes the same values the first delivery wrote. The
computation is a pure function of the seat snapshots (PR-3), so a redelivery
recomputes an identical result.

## Why the unique violation is translated rather than propagated

`IntegrityError` names a driver, not a domain fact, and it covers every
constraint on the table. Catching it and re-raising `AdjustmentAlreadyApplied`
**only** for the adjustment's own unique constraint means a check violation —
a score of 0.75, an expectation outside (0, 1) — still surfaces as a failure
rather than being silently counted as "already done".

That distinction is the whole reason this is not a bare `except
IntegrityError: pass`.
"""

import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Final

from sqlalchemy import Executable, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.identifiers import generate_uuid7
from app.modules.rating.application.ports import AdjustmentAlreadyApplied
from app.modules.rating.domain.glicko2 import Glicko2Rating
from app.modules.rating.domain.keys import RatingKey, SpeedClass
from app.modules.rating.domain.player_rating import PlayerRating, RatingAdjustment
from app.modules.rating.infrastructure.models import PlayerRatingModel, RatingAdjustmentModel
from app.modules.rating.public.ratings import RatingSnapshot

#: The constraint whose violation means "already applied" — PR-1.
#:
#: Matched by name rather than by inspecting the exception's text, so a
#: *different* constraint failing on this table is never mistaken for a
#: duplicate delivery. See this module's docstring.
_DUPLICATE_CONSTRAINT: Final = "uq_rating_adjustment__player_match"


class SqlAlchemyPlayerRatingRepository:
    """`PlayerRatingRepository` over one session.

    Constructed per unit of work, like every other repository on this
    platform: the session is the transaction, and a repository that outlived
    one would be writing into somebody else's.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load(self, player_id: uuid.UUID, *, key: RatingKey) -> PlayerRating:
        """This player's rating in this key, or the unrated starting state.

        Served by the primary key, so it is one index lookup. No `FOR
        UPDATE`: two matches completing for one player at the same instant
        are two legitimate games, and the unique index on the adjustment is
        what stops either being applied twice — a row lock here would
        serialise unrelated games for no invariant.
        """
        row = await self._session.get(PlayerRatingModel, (player_id, key.variant, key.speed_class))
        if row is None:
            return PlayerRating.unrated(player_id, key)
        return _to_domain(row)

    async def save(self, rating: PlayerRating, adjustment: RatingAdjustment) -> None:
        """Writes the rating and its adjustment. Raises on a duplicate.

        `flush`, not `commit`: the unit of work owns the transaction
        boundary, and committing here would put the outbox row this
        adjustment publishes into a different transaction from the
        adjustment itself — which is exactly what AD-16 exists to prevent.
        """
        await self._session.execute(_upsert(rating))
        self._session.add(_to_model(adjustment))

        try:
            await self._session.flush()
        except IntegrityError as violation:
            if _DUPLICATE_CONSTRAINT not in str(violation.orig):
                raise
            raise AdjustmentAlreadyApplied(
                f"match {adjustment.match_id} has already been applied to this rating"
            ) from violation


def _upsert(rating: PlayerRating) -> Executable:
    """`INSERT … ON CONFLICT DO UPDATE` on the aggregate's own key.

    An upsert rather than a read-then-branch because the row's absence is
    the ordinary first-match case, and a caller that had to know which it
    was would be one that can get it wrong under concurrency.

    Every mutable column is in the `SET`, and `player_id`/`variant`/
    `speed_class` are not — they are the conflict target, so writing them
    would be assigning a column its own value.
    """
    values: dict[str, Any] = {
        "player_id": rating.player_id,
        "variant": rating.key.variant,
        "speed_class": rating.key.speed_class,
        "rating_value": rating.rating.value,
        "rating_deviation": rating.rating.deviation,
        "rating_volatility": rating.rating.volatility,
        "games_played": rating.games_played,
        "is_frozen": rating.is_frozen,
        "peak_value": rating.peak_value,
        "peak_at": rating.peak_at,
        "last_rated_at": rating.last_rated_at,
        "season_id": rating.season_id,
    }
    statement = insert(PlayerRatingModel).values(**values)
    return statement.on_conflict_do_update(
        index_elements=["player_id", "variant", "speed_class"],
        set_={
            key: statement.excluded[key]
            for key in values
            if key not in ("player_id", "variant", "speed_class")
        },
    )


def _to_model(adjustment: RatingAdjustment) -> RatingAdjustmentModel:
    return RatingAdjustmentModel(
        # A surrogate key, because an adjustment genuinely has an identity
        # of its own — `(player, match)` is its *uniqueness*, which the
        # constraint holds, and using it as the primary key would put two
        # foreign UUIDs in every index that references one.
        id=generate_uuid7(),
        player_id=adjustment.player_id,
        match_id=adjustment.match_id,
        variant=adjustment.key.variant,
        speed_class=adjustment.key.speed_class,
        rating_before=adjustment.before.value,
        deviation_before=adjustment.before.deviation,
        volatility_before=adjustment.before.volatility,
        rating_after=adjustment.after.value,
        deviation_after=adjustment.after.deviation,
        volatility_after=adjustment.after.volatility,
        opponent_rating=adjustment.opponent.value,
        opponent_deviation=adjustment.opponent.deviation,
        opponent_volatility=adjustment.opponent.volatility,
        expected_score=adjustment.expected_score,
        actual_score=adjustment.actual_score,
        algorithm_version=adjustment.algorithm_version,
        applied_at=adjustment.applied_at,
        season_id=adjustment.season_id,
    )


def _to_domain(row: PlayerRatingModel) -> PlayerRating:
    return PlayerRating(
        player_id=row.player_id,
        key=RatingKey(variant=row.variant, speed_class=SpeedClass(row.speed_class)),
        rating=Glicko2Rating(
            value=row.rating_value,
            deviation=row.rating_deviation,
            volatility=row.rating_volatility,
        ),
        games_played=row.games_played,
        is_frozen=row.is_frozen,
        peak_value=row.peak_value,
        peak_at=row.peak_at,
        last_rated_at=row.last_rated_at,
        season_id=row.season_id,
    )


class SqlAlchemyRatingReader:
    """`rating.public.RatingReader` over one session.

    Separate from the repository, and that is the boundary rather than a
    duplication: the repository loads an *aggregate* so it can be mutated
    and saved, and this returns a *snapshot* that cannot be. `matchmaking`
    and `profiles` hold this one, so neither can reach a method that writes.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def rating_for(self, player_id: uuid.UUID, *, key: RatingKey) -> RatingSnapshot:
        """One player's rating. Delegates to the batch so there is one query
        to keep correct rather than two."""
        ratings = await self.ratings_for([player_id], key=key)
        return ratings[player_id]

    async def ratings_for(
        self, player_ids: Sequence[uuid.UUID], *, key: RatingKey
    ) -> Mapping[uuid.UUID, RatingSnapshot]:
        """Every named player's rating, in one query.

        **Complete**: players with no row are filled in with the unrated
        snapshot, so a caller cannot silently skip somebody by reading a key
        that is absent from the result.

        Deduplicated, because a caller composing a page may legitimately
        name the same player twice and that is one row rather than two.
        """
        wanted = list(dict.fromkeys(player_ids))
        if not wanted:
            return {}

        rows = await self._session.scalars(
            select(PlayerRatingModel).where(
                PlayerRatingModel.player_id.in_(wanted),
                PlayerRatingModel.variant == key.variant,
                PlayerRatingModel.speed_class == key.speed_class,
            )
        )
        found = {row.player_id: _to_snapshot(row) for row in rows}
        return {player_id: found.get(player_id, RatingSnapshot.unrated()) for player_id in wanted}

    async def ratings_across(
        self, player_ids: Sequence[uuid.UUID], *, keys: Sequence[RatingKey]
    ) -> Mapping[tuple[uuid.UUID, RatingKey], RatingSnapshot]:
        """Every player, every key, one query.

        `variant IN (...) AND speed_class IN (...)` rather than a key-by-key
        loop, so a profile page costs one round trip whatever number of
        classes it renders. The cross product is filtered back down in
        Python because the pairs a caller asked for are the pairs it gets —
        a key the query matched but the caller did not name is not in the
        result.
        """
        wanted_players = list(dict.fromkeys(player_ids))
        wanted_keys = list(dict.fromkeys(keys))
        if not wanted_players or not wanted_keys:
            return {}

        rows = await self._session.scalars(
            select(PlayerRatingModel).where(
                PlayerRatingModel.player_id.in_(wanted_players),
                PlayerRatingModel.variant.in_({key.variant for key in wanted_keys}),
                PlayerRatingModel.speed_class.in_({key.speed_class for key in wanted_keys}),
            )
        )
        found = {
            (
                row.player_id,
                RatingKey(variant=row.variant, speed_class=SpeedClass(row.speed_class)),
            ): _to_snapshot(row)
            for row in rows
        }
        return {
            (player_id, key): found.get((player_id, key), RatingSnapshot.unrated())
            for player_id in wanted_players
            for key in wanted_keys
        }


def _to_snapshot(row: PlayerRatingModel) -> RatingSnapshot:
    """A row as the published reading.

    `is_provisional` is derived through the aggregate rather than
    recomputed here, so the threshold lives in exactly one place — a second
    `games_played < 25` in this file would be the stored-flag problem with
    extra steps.
    """
    return RatingSnapshot(
        value=row.rating_value,
        deviation=row.rating_deviation,
        volatility=row.rating_volatility,
        games_played=row.games_played,
        is_provisional=_to_domain(row).is_provisional,
    )


__all__ = ["SqlAlchemyPlayerRatingRepository", "SqlAlchemyRatingReader"]
