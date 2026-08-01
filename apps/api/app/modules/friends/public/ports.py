"""The port other modules may depend on — BE-03's published surface.

One port, two methods. `profiles` is the only consumer and needs exactly
these: which of a page of players the viewer is friends with (A64-013.3),
and which players they cannot interact with at all (A64-013.5).

## Why one port rather than two

They are two reads of one graph, consumed together by one module: relationship
resolution asks both on every composition, because a block outranks a
friendship and the answer is wrong without either. Two published ports would
be two things to wire, two adapters to keep in step, and — the part that
matters — two chances for a consumer to hold the friendship half and not the
block half, which is precisely the leak BL-1 forbids.

The *consumer-side* narrowing still happens, in `profiles.application.ports`:
`ViewerRelationshipProvider` and `BlockedPlayersProvider` are separate there,
because the search path needs the exclusion set and not the relationships.
"""

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID


class SocialGraphReader(Protocol):
    """Answers what the social graph says about one player's relationships.

    **Read-only by construction.** There is no way here to create or end a
    friendship, or to place or lift a block, which is what makes it safe to
    hand to the module that composes every public profile on the platform.

    `friend_ids_among` is **batch-only**, which stops that module calling it
    per row. `blocked_ids_for` is not, because a block set is per *viewer*
    rather than per rendered player — one call answers a whole page however
    long it is.

    Takes `UUID`s — DM-06's `player_id`, the only reference that crosses a
    context boundary. Deliberately not profiles or usernames: a social graph
    has no business receiving a display name.

    **Applies no privacy.** Whether two players are friends is a fact about
    the graph; whether a *field* may be shown to a friend is
    `users.domain.privacy`'s, applied by `PublicProfileComposer`. A port
    that answered both would be a second place a visibility rule lives.
    """

    async def friend_ids_among(self, player_id: UUID, others: Sequence[UUID]) -> set[UUID]:
        """The subset of `others` currently friends with `player_id`.

        **One query for the whole page.** This runs on the composition path,
        so a per-player form would multiply every profile render on the
        platform — the N+1 pattern CLAUDE.md §10.4 names as the single most
        common cause of slow endpoints.

        Returns the *other* players' ids rather than friendship rows,
        because that is what a caller does with the answer: index it.

        Never raises, and an empty `others` returns an empty set without
        touching the database. A friendship that ended is not a friendship:
        only live rows count.
        """
        ...

    async def blocked_ids_for(self, player_id: UUID) -> frozenset[UUID]:
        """Every player this one cannot interact with, in **either**
        direction — A64-013.5.

        Symmetric even though a block is not. BL-1 makes a block
        one-directional and invisible to its subject, but the *visibility*
        consequence runs both ways: a blocker who kept seeing the person
        they blocked would have gained nothing, and a blocked player who
        could still see the blocker would notice the asymmetry. One set,
        both directions, and neither party can tell which applies.

        Per **viewer**, not per rendered player, so one call answers a page
        of any length — which is why this has no batch form and needs none.

        Never raises. An empty set means no blocks, which is the common
        case.
        """
        ...
