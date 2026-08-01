"""The two implementations of `application.ports.ViewerRelationshipProvider`
— A64-013.3.

    FriendshipRelationshipProvider   the real one, reading `friends.public`
    NoRelationshipsProvider          everybody is a stranger

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

from app.modules.friends.public import FriendshipReader
from app.modules.users.public import ViewerRelationship

logger = logging.getLogger(__name__)


class FriendshipRelationshipProvider:
    """Resolves relationships from the live social graph.

    Holds `friends.public.FriendshipReader` — the published port — and not a
    repository or a session, because R-1 forbids reaching into another
    module's storage. That is also what keeps this class three lines: the
    query is `friends`' business, and this is the seam that lets `profiles`
    keep a port it owns.

    It is where a consumer-side concern would go if one arrives — a
    per-request memo across two compositions in one handler, a circuit
    breaker around a degraded graph — none of which belongs in the owning
    context.
    """

    def __init__(self, friendships: FriendshipReader) -> None:
        self._friendships = friendships

    async def relationships_for(
        self, viewer_id: UUID, player_ids: Sequence[UUID]
    ) -> Mapping[UUID, ViewerRelationship]:
        """One query for the page, then a total mapping.

        **Complete**: every id asked for gets an entry. The friends are
        `FRIEND` and everybody else is `STRANGER`, so a caller never has to
        decide what a missing key means — which is the decision that would
        eventually be made wrongly on a privacy path.

        The viewer's own id, if it appears among `player_ids`, comes back
        `STRANGER`: nobody is their own friend, and the two endpoints that
        serve an account holder their own data bypass privacy entirely
        rather than passing a relationship.
        """
        if not player_ids:
            return {}

        friends = await self._friendships.friend_ids_among(viewer_id, player_ids)

        return {
            player_id: (
                ViewerRelationship.FRIEND if player_id in friends else ViewerRelationship.STRANGER
            )
            for player_id in player_ids
        }


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
