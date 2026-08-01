"""`PresenceAudienceService` — who may be told about a player's presence.

A64-013.6: "blocked pairs must never receive presence updates about each
other. **Filtering belongs at the source of fan-out.** Do not rely on
clients filtering events."

This is that source. Nothing fans out yet — WebSockets are excluded — so
what exists here is the integration point a gateway calls to build its
recipient list, implemented and tested rather than described.

## The audience is social; the permission is not

    observers_of(player) = friends(player) - blocked(player)

Two subtractions and no privacy check, which is deliberate and is the
constraint A64-013.6 states as "no permission logic outside the composer".

Whether a *field* may be shown to a given viewer is `VisibilityLevel` and
`ViewerRelationship`, applied by `PublicProfileComposer`. A gateway pushing
a presence frame must render each recipient's view through that same gate —
not reimplement it here, and not trust that membership of this set implies
permission. What this set guarantees is narrower and is exactly what the
brief asks for: **nobody in it is blocked**.

## Why blocked players are subtracted rather than filtered downstream

A block is invisible to its subject (BL-1). If a blocked player received a
presence frame and a client dropped it, the frame still crossed the network
to a machine the blocker excluded — and any client that did not drop it
would leak. Subtracting at the source is the only form of this that holds
against a client the platform does not control.

Blocking already ends friendships (FS-3), so a blocked pair should not be in
`friends(player)` to begin with. The subtraction runs anyway, for the reason
`FriendshipRelationshipProvider` ranks `BLOCKED` above `FRIEND`: "should not
arise" is a claim about another transaction, and a fan-out filter is the
wrong place to assume one held.

## Why friends, and only friends

Presence is pushed to people who have a reason to receive it. A stranger who
opens a profile page *reads* presence through the composer, gated by
`show_online_status`; they do not subscribe to it. Broadcasting a player's
comings and goings to everybody who ever looked at them would be a
behavioural feed nobody asked for — and `show_last_seen` defaulting closed
says what this platform thinks of that.
"""

import logging
from uuid import UUID

from app.modules.friends.application.ports import (
    BlockedPlayerRepository,
    FriendshipRepository,
)

logger = logging.getLogger(__name__)


class PresenceAudienceService:
    """Builds the recipient list for a presence change.

    Holds two repositories and nothing else: no presence store, no
    transport, no privacy. It answers one question and a future gateway
    answers the rest.
    """

    def __init__(
        self,
        *,
        friendships: FriendshipRepository,
        blocks: BlockedPlayerRepository,
    ) -> None:
        self._friendships = friendships
        self._blocks = blocks

    async def observers_of(self, player_id: UUID) -> frozenset[UUID]:
        """Who may be told that this player came online or went.

        **Two queries, whatever the answer's size** — both are per-player
        set reads served by partial indexes, so this does not grow with the
        number of friends beyond the size of the sets themselves. A version
        that asked "is this recipient blocked" per friend would be the N+1
        pattern on a path that runs on every presence transition.

        Returns a `frozenset` because callers only iterate and test
        membership, and because a mutable recipient list is one a caller
        could add to.

        An empty set is the ordinary answer for a player with no friends,
        and a gateway must treat it as "send nothing" rather than "send to
        everybody" — which is why this returns the audience rather than a
        predicate.
        """
        friends = await self._friendships.friend_ids_for(player_id)
        if not friends:
            # No recipients, so no reason to read the block set. The common
            # case on a young platform, and the one where a fan-out has
            # nothing to do.
            return frozenset()

        blocked = await self._blocks.blocked_ids_for(player_id)
        audience = friends - blocked

        # Counts only. *Who* is friends with whom, and who has blocked whom,
        # are the two edges this platform is most careful with — a log line
        # naming them would reassemble the social graph somewhere with
        # broader read access than the rows (services.md §8.5).
        logger.debug(
            "presence_audience_resolved",
            extra={"friends": len(friends), "excluded": len(friends) - len(audience)},
        )
        return audience
