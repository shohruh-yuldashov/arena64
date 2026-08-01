"""`ProfileDirectoryService` — player ids in, composed public profiles out.

The third consumer of `PublicProfileComposer`, and the one that exists so
other *modules* can render players without reimplementing composition.

`ProfileService` starts from a username and `ProfileSearchService` starts
from a term; `friends` starts from a page of ids, because a friend request
stores `requester_id` and `addressee_id` and nothing else. This service is
the adapter between those ids and the platform's one public representation
of a player.

## Why `friends` does not compose for itself

It would need the reader, the composer, three providers and the privacy
rules that connect them — which is `profiles`, rebuilt in another module.
The alternative to this class is a second public view of a player, and
A64-013.1 established that there is exactly one.

## Batch only, deliberately

There is no `profile_for(player_id)` here, and its absence is the design.
A64-013.2 requires that friend-request pages never compose in a loop, and
the strongest form of that requirement is a service that **cannot** be
called for one player: a caller with a single id passes a one-element
sequence, which costs the same three round trips a loop would spend per
row.

Adding the singular convenience method would make the N+1 reachable again
in one line, so it is not added. `ProfileService.get_public_profile` remains
the singular path, and it starts from a username.
"""

import logging
from collections.abc import Mapping, Sequence
from uuid import UUID

from app.modules.profiles.application.services.profile_composer import PublicProfileComposer
from app.modules.profiles.domain.profile import PublicProfile
from app.modules.users.public import PublicProfileReader, ViewerRelationship

logger = logging.getLogger(__name__)


class ProfileDirectoryService:
    def __init__(
        self,
        *,
        profiles: PublicProfileReader,
        composer: PublicProfileComposer,
    ) -> None:
        self._profiles = profiles
        self._composer = composer

    async def profiles_for(
        self,
        player_ids: Sequence[UUID],
        *,
        viewer: ViewerRelationship = ViewerRelationship.STRANGER,
    ) -> Mapping[UUID, PublicProfile]:
        """Composed public profiles for a page of ids, keyed by id.

        **Four round trips for any page size** — one identity read plus the
        composer's three — and the count does not grow with the page. That
        is the property A64-013.2 asks for by name.

        Keyed rather than ordered, because the caller holds the ordering: a
        friend-request list is ordered by when the request arrived, which is
        a fact about the request and not about the player. Returning a list
        would force the caller to re-sort or to trust an order this service
        has no reason to guarantee.

        **A missing id is a missing key.** Deactivated accounts are omitted
        by `find_public_profiles`, and the caller decides what that means
        for its own row — `friends` drops the request, because a request
        from a withdrawn account is not something to render.

        `viewer` is the composition point A64-013.5 will use: once blocks
        and friendships exist, the caller passes what it knows about the
        relationship and every privacy gate honours it. Today every caller
        passes the default, so the parameter is threaded rather than
        exercised — but it is threaded, which is what stops the eventual
        change from reaching into the mappers.
        """
        if not player_ids:
            return {}

        identities = await self._profiles.find_public_profiles(player_ids)

        # Ordered by the caller's sequence rather than by the mapping's
        # iteration order, so `compose_many`'s batch reads are issued in a
        # stable order and the result can be zipped back deterministically.
        ordered = [identities[player_id] for player_id in player_ids if player_id in identities]

        composed = await self._composer.compose_many(ordered, viewer=viewer)

        # Counts only. Which players a caller looked up is a social-graph
        # read — on the friend-request path it is literally who asked whom —
        # and a log line naming them would be more sensitive than the
        # profiles it returns (services.md §8.5).
        logger.debug(
            "profile_directory_lookup",
            extra={"requested": len(player_ids), "resolved": len(composed)},
        )

        return {profile.identity.id: profile for profile in composed}
