"""The ports `statistics` programs against — AD-06: declared in
`application/`, satisfied by `infrastructure/`.

Two ports. `StatisticsRepository` is the read this module shipped with;
`MatchProjectionUseCase` is the write A64-020.5F adds.

## The writer arrived, and the conditions it was waiting for

A64-012.6 recorded why there was none: "a projection's writer is not a
repository method — it is a consumer of `match.completed` folding results
in idempotently... That consumer needs a watermark column, a dead-letter
path and an ordering guarantee, none of which exist."

All three exist now. The watermark is `(counted_at, counted_match_id)` on
`player_statistics`; the dead-letter path is the outbox relay's retry and
attempt cap; the ordering guarantee is that same watermark compared as a
total order, so a match arriving late still counts and does not rewrite a
streak (§3).

`StatisticsRepository` gains the three operations a projection needs, and
they are deliberately the smallest set that can express one: claim a match
for a player, read the row under a lock, write it back. Anything wider —
"set games_played", "reset a player" — would be a way to produce a row no
sequence of matches could produce.

The port returns **values**, never ORM rows: `ProjectionState` carries the
counters and the watermark, and the mapping stays in infrastructure. That
is what `statistics layers point inward` enforces, and it is why the lock
and the write are two calls rather than a handle the application holds.

`MatchProjectionUseCase` abstracts something different — the *transaction
boundary*, so the consumer and the backfill can call one service without
either knowing how the other batches.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from app.modules.statistics.domain.projection import Projected, ProjectionState
from app.modules.statistics.domain.statistics import PlayerStatistics

if TYPE_CHECKING:
    from app.modules.statistics.application.services.match_projection_service import (
        CompletedMatchFacts,
        ProjectionOutcome,
    )


class StatisticsRepository(Protocol):
    """Reads one player's stored record.

    A `Protocol`, not an ABC, so the SQLAlchemy adapter and an in-memory
    fake satisfy it structurally without inheriting from anything this
    module owns (repositories.md RP-05).
    """

    async def get_for_player(self, player_id: UUID) -> PlayerStatistics | None:
        """The stored record, or `None` when the player has no row.

        **`None` is an ordinary outcome, not a failure.** A projection is
        built by folding match results in, so a player who has finished no
        matches has nothing to fold and therefore no row — which is the
        state of every account on the day it registers. Raising here would
        make the most common case the exceptional path and would force
        every caller to write a `try` around a new account.

        Deciding what `None` *means* is the service's job, not this one's:
        the repository reports what storage holds and
        `StatisticsService.for_player` turns absence into
        `NO_MATCHES_PLAYED`.
        """
        ...

    async def get_for_players(self, player_ids: Sequence[UUID]) -> Mapping[UUID, PlayerStatistics]:
        """The stored records for a page of players, in **one** statement.

        Added by A64-013.1, the first caller that renders more than one
        player at a time. `get_for_player` in a loop over a page of search
        results is the N+1 access pattern CLAUDE.md §10.4 calls "the single
        most common cause of slow endpoints", and it would return with
        friend lists and leaderboards.

        **Players with no row are omitted**, exactly as `get_for_player`
        returns `None` for them — absence stays an ordinary outcome and the
        service is still the layer that decides what it *means*. A mapping
        padded with `NO_MATCHES_PLAYED` would move that decision into
        storage.

        An empty `player_ids` returns an empty mapping without issuing a
        statement. `WHERE player_id = ANY('{}')` is a round trip that can
        only return nothing, and an empty page is the ordinary result of a
        search nobody matched.
        """
        ...


class StatisticsProjectionRepository(Protocol):
    """The writes a projection needs — A64-020.5F §4.

    Separate from `StatisticsRepository` above, which is the read every
    other module consumes through `statistics.public`. Splitting them is
    what keeps a reader unable to write: `profiles` holds the first and has
    no way to reach the second.
    """

    async def claim(self, match_id: UUID, player_id: UUID, *, at: datetime) -> bool:
        """Records that this player has been credited with this match.

        `True` if this call made the record, `False` if it already existed.
        **The exactly-once mechanism** — the decision is a unique
        constraint's, not a read the caller could lose a race on.
        """
        ...

    async def state_for_update(self, player_id: UUID) -> "ProjectionState":
        """This player's counters and watermark, with the row locked.

        Creates the row if absent: a player's first completed match is
        exactly when one should appear, and the absence of a row is a
        legitimate state for a projection (DM-03).

        The lock is held for the caller's transaction, which is what makes
        the separate `write` below safe without a compare-and-set.
        """
        ...

    async def write(self, player_id: UUID, projected: "Projected") -> None:
        """Stores a folded record. Requires the row locked by
        `state_for_update` in the same transaction."""
        ...


class MatchProjectionUseCase(Protocol):
    """Counting one completed match into both players' records — §8.

    Held by the consumer and by the backfill, so neither knows whether the
    transaction is per event or per batch. That is the only thing this
    abstracts, and it is the only thing that genuinely differs between
    them: the rules, the marker and the ordering are identical by
    construction, because both call the same service.
    """

    async def apply(self, facts: "CompletedMatchFacts") -> "ProjectionOutcome":
        """Folds one match in, exactly once per player.

        Idempotent by construction: a match already counted for a player
        returns `ALREADY_PROCESSED` rather than counting twice. The
        guarantee is `pk_processed_match`, not this contract — a caller
        cannot break it by calling twice.
        """
        ...
