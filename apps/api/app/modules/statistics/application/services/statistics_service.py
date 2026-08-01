"""`StatisticsService` — read one player's record.

A query service in the sense of services.md §3.1: read-only, opening no
transaction, because there is nothing to commit and a unit of work around a
single `SELECT` would be ceremony suggesting otherwise.

It orchestrates; it does not compute (services.md §3.2). `win_rate` is the
domain's, the counts are the repository's, and what lives here is the one
decision neither of them owns: **what the absence of a row means**.

## Absence is a value, not a failure

A projection is built by folding match results in (domain-model.md §11.5),
so a player who has finished no matches has nothing to fold and therefore
no row. That is the state of every account on the day it registers — the
most common case on a young platform, not an error — so this returns
`NO_MATCHES_PLAYED` rather than raising or returning `None`.

The consequence worth stating: **this service cannot tell a caller whether
a player exists.** It answers "what is this player's record" with zeroes
for an unknown id exactly as it does for a brand-new account, which is the
right answer for a statistics context (it does not own the player
directory) and incidentally denies an enumeration oracle to anyone probing
`/profiles/{username}` for ids.
"""

import logging
from collections.abc import Mapping, Sequence
from uuid import UUID

from app.modules.statistics.application.ports import StatisticsRepository
from app.modules.statistics.domain.statistics import NO_MATCHES_PLAYED, PlayerStatistics

logger = logging.getLogger(__name__)


class StatisticsService:
    def __init__(self, statistics: StatisticsRepository) -> None:
        self._statistics = statistics

    async def for_player(self, player_id: UUID) -> PlayerStatistics:
        """This player's record, defaulting to the empty one.

        Never raises for an unknown player and never returns `None` — see
        this module's docstring.
        """
        stored = await self._statistics.get_for_player(player_id)

        if stored is None:
            # A64-012.6 asks for fallback usage to be logged, and this is
            # the *within-provider* half of that: the store was reachable
            # and simply had nothing for this player. Distinct from
            # `statistics_provider_fallback`, which the composition root
            # emits when the database provider is not wired at all — the
            # two look identical in a response and mean entirely different
            # things to an operator.
            #
            # DEBUG rather than INFO: on a platform with no matches yet
            # this is every single profile read, and a signal that fires on
            # every request is not a signal (services.md §7.1).
            logger.debug("statistics_absent", extra={"player_id": str(player_id)})
            return NO_MATCHES_PLAYED

        # The id and nothing else. Never a username, never a count — a
        # permanent access record must not become a searchable index of who
        # looked at whom (services.md §8.5), and the numbers are in the
        # response the caller already has.
        logger.info("statistics_lookup_succeeded", extra={"player_id": str(player_id)})
        return stored

    async def for_players(self, player_ids: Sequence[UUID]) -> Mapping[UUID, PlayerStatistics]:
        """A page of records, defaulting every absence to the empty one.

        **Complete by construction**: every id asked for has an entry, so a
        caller indexes the mapping rather than writing a `.get(id) or
        NO_MATCHES_PLAYED` at each call site — which is the line somebody
        eventually writes as `.get(id)` alone and renders a null record.

        That is the opposite of `PresenceProvider.presence_for_many`, which
        omits absent players, and the asymmetry is deliberate: absence of
        statistics has a well-defined value (nobody has played nothing
        *unknowably*), while absence of presence genuinely means unknown.
        Padding presence would invent an observation; padding statistics
        states a fact.

        One log line for the page rather than one per player. Twenty
        `statistics_lookup_succeeded` records per search would drown the
        signal the single-profile path emits (CLAUDE.md §8.8), and the
        count is what an operator would actually want from a batch.
        """
        if not player_ids:
            return {}

        stored = await self._statistics.get_for_players(player_ids)

        # Ids only, and the two counts. Never the numbers, and never a
        # username — a permanent access record must not become a searchable
        # index of who looked at whom (services.md §8.5). `missing` is the
        # useful diagnostic: on a platform with no matches it is the whole
        # page, and a sudden change means the projection moved.
        logger.debug(
            "statistics_batch_lookup",
            extra={"requested": len(player_ids), "found": len(stored)},
        )

        return {player_id: stored.get(player_id, NO_MATCHES_PLAYED) for player_id in player_ids}
