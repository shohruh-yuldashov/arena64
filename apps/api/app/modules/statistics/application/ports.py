"""The ports `statistics` programs against — AD-06: declared in
`application/`, satisfied by `infrastructure/`.

One port, because this module has one job today: read a player's record.
There is deliberately no writer.

## Why there is no write port yet

A64-012.6 excludes game result processing, and a projection's writer is not
a repository method — it is a consumer of `match.completed` folding results
in idempotently (domain-model.md §227: "downstream contexts consume
`match.completed` as a self-contained fact and never call back"). That
consumer needs a watermark column, a dead-letter path and an ordering
guarantee, none of which exist.

Publishing an `upsert` now would be a method with no caller and no
correctness story, which is exactly the speculative generality CLAUDE.md §1
rule 7 rules out. The seam that matters — that `profiles` reads through a
port and never touches this table — is already in place, and a writer joins
the same `application/` layer when there is something to write.
"""

from collections.abc import Mapping, Sequence
from typing import Protocol
from uuid import UUID

from app.modules.statistics.domain.statistics import PlayerStatistics


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
