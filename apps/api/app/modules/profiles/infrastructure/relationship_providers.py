"""The two implementations of `application.ports.ViewerRelationshipProvider`
— A64-013.3.

    FriendshipRelationshipProvider    the real one, reading `friends.public`
    NoRelationshipsProvider           everybody is a stranger
    SocialGraphBlockedPlayersProvider the real exclusion set (A64-013.5)
    NoBlockedPlayersProvider          nobody is blocked

The choice is made once per request in the composition root
(`profiles.presentation.dependencies`) and logged there, because that is the
only place that knows a *choice* was made — neither class below can say what
it was chosen instead of. The arrangement `statistics_providers.py` uses,
for the same reasons.

## What the fallback is for

An operational kill switch, the same shape as `StatisticsSettings.enabled`
and `PresenceSettings.enabled`: a deployment whose `friends` relation is
being migrated, or whose social graph is simply unhealthy, sets
`FRIENDS_ENABLED=false` and keeps serving profiles.

**The degradation narrows rather than widens**, which is the property that
makes it safe. With every viewer reported as a stranger, a field set to
`FRIENDS` is hidden from everyone — including actual friends. That is a
visible loss of functionality and not a disclosure, which is the correct
direction for a privacy control to fail in; the opposite fallback
("everybody is a friend") would publish, during an incident, exactly what
players had restricted.
"""

import logging
from collections.abc import Mapping, Sequence
from uuid import UUID

from app.modules.friends.public import SocialGraphReader
from app.modules.users.public import ViewerRelationship

logger = logging.getLogger(__name__)


class FriendshipRelationshipProvider:
    """Resolves relationships from the live social graph.

    Holds `friends.public.SocialGraphReader` — the published port — and not a
    repository or a session, because R-1 forbids reaching into another
    module's storage. That is also what keeps this class three lines: the
    query is `friends`' business, and this is the seam that lets `profiles`
    keep a port it owns.

    It is where a consumer-side concern would go if one arrives — a
    per-request memo across two compositions in one handler, a circuit
    breaker around a degraded graph — none of which belongs in the owning
    context.
    """

    def __init__(self, graph: SocialGraphReader) -> None:
        self._graph = graph

    async def relationships_for(
        self, viewer_id: UUID, player_ids: Sequence[UUID]
    ) -> Mapping[UUID, ViewerRelationship]:
        """One query for the page, then a total mapping.

        **Complete**: every id asked for gets an entry. The friends are
        `FRIEND` and everybody else is `STRANGER`, so a caller never has to
        decide what a missing key means — which is the decision that would
        eventually be made wrongly on a privacy path.

        The viewer's own id, if it appears among `player_ids`, comes back
        `STRANGER`: nobody is their own friend or their own blocker, and the
        two endpoints that serve an account holder their own data bypass
        privacy entirely rather than passing a relationship.

        **`BLOCKED` wins over `FRIEND`** — A64-013.5, and it should never
        arise: `BlockingService.block` ends the friendship in the same
        transaction that places the block (FS-3). The precedence is written
        down anyway because "should never arise" is a claim about another
        module's transaction, and a visibility gate is the wrong place to
        assume one.
        """
        if not player_ids:
            return {}

        # Two reads, and the block set is fetched **whatever the friendship
        # set says** — a block outranks a friendship, so an answer computed
        # from friendships alone would be wrong precisely for the pairs that
        # matter most. Both are per-viewer or per-page, so this is two
        # queries for a page of any length.
        blocked = await self._graph.blocked_ids_for(viewer_id)
        friends = await self._graph.friend_ids_among(viewer_id, player_ids)

        return {player_id: _relationship(player_id, blocked, friends) for player_id in player_ids}


class NoRelationshipsProvider:
    """Everybody is a stranger — the fallback, and the anonymous path.

    Two callers, and the second is not a degradation at all: a signed-out
    visitor has no relationships to resolve, so `PublicProfileComposer`
    reaches for this rather than issuing a query whose answer is already
    known. That is why this class is not merely a kill switch.

    **Depends on nothing.** No `friends`, no session — a fallback that
    imported the module it replaces would fail for exactly the reasons it
    exists. Stateless and infallible, and it ignores both arguments, which
    is the honest signature when the answer is the same for everyone.
    """

    async def relationships_for(
        self, viewer_id: UUID, player_ids: Sequence[UUID]
    ) -> Mapping[UUID, ViewerRelationship]:
        return dict.fromkeys(player_ids, ViewerRelationship.STRANGER)


class NoBlockedPlayersProvider:
    """Nobody is blocked — the fallback for the exclusion set.

    **Never fabricates a block**, which A64-013.5 states outright and which
    is the same direction `NoRelationshipsProvider` fails in for the
    opposite reason. A fallback that reported everybody blocked would hide
    the whole platform from search during an incident; one that reports
    nobody blocked shows a blocked player in a search result.

    The second is the lesser harm and is the only honest option: the
    fallback exists because the graph is *unavailable*, and inventing
    restrictions from missing data is how a kill switch becomes an outage.
    It is also bounded — a blocked player appearing in a search result is a
    row somebody can ignore, while the *visibility* consequence still holds
    everywhere it matters, because `NoRelationshipsProvider` reports
    `STRANGER` rather than `FRIEND` and every friends-only field stays
    hidden.

    Stateless, infallible, depends on nothing.
    """

    async def blocked_ids_for(self, player_id: UUID) -> frozenset[UUID]:
        return frozenset()


class SocialGraphBlockedPlayersProvider:
    """The real exclusion set, read from the social graph.

    Holds `friends.public.SocialGraphReader` — the published port — and not
    a repository, because R-1 forbids reaching into another module's
    storage.

    Separate from `FriendshipRelationshipProvider` even though both wrap the
    same reader, for the reason every port pair on this platform is
    separate: what differs is the *capability*. Search needs the exclusion
    set and never resolves a relationship; composition resolves
    relationships and never excludes anybody. A single provider would hand
    each of them the other's.
    """

    def __init__(self, graph: SocialGraphReader) -> None:
        self._graph = graph

    async def blocked_ids_for(self, player_id: UUID) -> frozenset[UUID]:
        """Every player to exclude from this viewer's results.

        One query per search, not per result: a block set is per *viewer*,
        so its cost does not grow with the page.
        """
        return await self._graph.blocked_ids_for(player_id)


def _relationship(
    player_id: UUID, blocked: frozenset[UUID], friends: set[UUID]
) -> ViewerRelationship:
    """The one place the three values are ranked.

    **Blocked first**, unconditionally. A block is not "less than a
    friendship", it is a different answer that outranks every other — the
    same precedence `VisibilityLevel.permits` applies one level down, and
    writing it in one function here is what keeps the two from disagreeing.
    """
    if player_id in blocked:
        return ViewerRelationship.BLOCKED
    if player_id in friends:
        return ViewerRelationship.FRIEND
    return ViewerRelationship.STRANGER
