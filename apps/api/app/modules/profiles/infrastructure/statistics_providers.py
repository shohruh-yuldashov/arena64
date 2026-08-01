"""The two implementations of `application.ports.StatisticsProvider`.

AD-06: the port is declared in `application/`, satisfied here. AD-08: a
cross-context read reaches the other context through *its* published
surface, never its storage — which is why `DatabaseStatisticsProvider`
below holds a `StatisticsReader` and not a session.

## Why there are two, and how one is chosen

    DatabaseStatisticsProvider   the real record, read through
                                 `statistics.public.StatisticsReader`
    NoMatchesStatisticsProvider  every player has played nothing

A64-012.8 moved `UnratedRatingProvider` out to `rating_providers.py`. It had
been here since this file was renamed from `unrated_providers.py`, and a
rating adapter in a file named for statistics is a name that no longer says
what is inside it. Both are still exported from
`infrastructure/__init__.py`, so no caller moved.

The choice is made once per request in the composition root
(`presentation/dependencies/__init__.py`) from `StatisticsSettings.enabled`,
and it is logged there. A64-012.6 asks for provider selection and fallback
usage to be logged; both happen at the point of selection, because that is
the only place that knows a *choice* was made — neither class below can
tell you what it was chosen instead of.

## What the fallback is actually for

Not "the statistics module has not shipped yet" — it has, and it is wired
by default. It is an **operational kill switch**, the same shape as
`RateLimitSettings.enabled`: a deployment whose statistics store is being
rebuilt, migrated, or is simply unhealthy can set `STATISTICS_ENABLED=false`
and keep serving profiles, rather than taking down the platform's
highest-volume public read for a projection that is rebuildable by
definition (database.md C5).

That is why the fallback returns zeroes rather than raising, and why it
must not depend on `statistics` at all — a fallback that imported the
module it replaces would fail for exactly the reasons it exists.

The honest cost, stated because it is not obvious: while the switch is off,
every profile reports a blank record and no client can tell that apart from
a genuinely new player. It is a degradation, not a transparent one, and the
`WARNING` at selection is what makes it visible to an operator.
"""

import logging
from collections.abc import Mapping, Sequence
from uuid import UUID

from app.modules.statistics.public import NO_MATCHES_PLAYED, PlayerStatistics, StatisticsReader

logger = logging.getLogger(__name__)


class DatabaseStatisticsProvider:
    """A player's real record, read from the `statistics` context.

    ## The name, which is slightly wrong on purpose

    A64-012.6 names this `DatabaseStatisticsProvider`, and it holds no
    session, issues no SQL and knows no table. It holds
    `statistics.public.StatisticsReader` and delegates — because R-1 forbids
    reaching into another module's storage, and a provider in `profiles`
    that ran a `SELECT` against `statistics.player_statistics` would be
    exactly that violation wearing the right name.

    The name is kept because it says the useful thing to a reader of the
    composition root: this is the one that returns real, stored numbers, as
    opposed to the one that returns zeroes. Where those numbers physically
    live is `statistics`' business and may stop being a database without
    this class changing.

    ## Why this adapter exists at all

    `StatisticsReader.for_player` and `StatisticsProvider.statistics_for`
    have the same signature, so this class is three lines of delegation and
    looks like ceremony. It is the seam that lets `profiles` keep a port it
    owns: `NoMatchesStatisticsProvider` satisfies `StatisticsProvider`
    without depending on `statistics` at all, which a fallback must, and
    `profiles` does not break if `statistics` widens or renames its port.

    It is also where a consumer-side concern would go if one arrives — a
    per-request cache, a circuit breaker around a degraded store — none of
    which belongs in the owning context.
    """

    def __init__(self, statistics: StatisticsReader) -> None:
        self._statistics = statistics

    async def statistics_for(self, player_id: UUID) -> PlayerStatistics:
        """The stored record, or the empty one for a player with no
        history.

        Never raises for an unknown player: `StatisticsReader` guarantees a
        value either way, and this adapter adds no branch of its own. The
        "no row" case is logged inside `statistics`, where the distinction
        between "the store had nothing" and "the store was not consulted"
        is knowable.
        """
        return await self._statistics.for_player(player_id)

    async def statistics_for_many(
        self, player_ids: Sequence[UUID]
    ) -> Mapping[UUID, PlayerStatistics]:
        """A page of stored records, delegating the batch straight through.

        Three lines again, and the seam earns its place for the reason the
        single read's does: `NoMatchesStatisticsProvider` below satisfies
        this without depending on `statistics` at all, which a fallback must.
        """
        return await self._statistics.for_players(player_ids)


class NoMatchesStatisticsProvider:
    """Every player has finished no matches — the fallback.

    Returns `NO_MATCHES_PLAYED`: all four counts zero, both ratings at the
    starting value, no streaks, and therefore a `win_rate` of `0.0` rather
    than a division by zero. The frozen singleton is safe to share —
    `PlayerStatistics` is immutable, so no caller can mutate the value
    another caller holds.

    **Imports `statistics.public` for the value and nothing else.** It takes
    the published empty record rather than constructing its own, so
    "this player has no history" is visibly one value across both providers
    rather than two definitions that could drift. What it does *not* do is
    depend on the reader, the service, or the schema — so it keeps working
    when those are exactly what is unavailable.

    Stateless, infallible, and ignores the `player_id`, like
    `UnratedRatingProvider` above and for the same reasons.
    """

    async def statistics_for(self, player_id: UUID) -> PlayerStatistics:
        return NO_MATCHES_PLAYED

    async def statistics_for_many(
        self, player_ids: Sequence[UUID]
    ) -> Mapping[UUID, PlayerStatistics]:
        """The empty record for everyone asked about.

        Complete rather than empty, matching `StatisticsService.for_players`:
        a caller indexing this mapping must not have to know which provider
        it was handed. Sharing the frozen singleton across every entry is
        safe because `PlayerStatistics` is immutable.
        """
        return dict.fromkeys(player_ids, NO_MATCHES_PLAYED)
