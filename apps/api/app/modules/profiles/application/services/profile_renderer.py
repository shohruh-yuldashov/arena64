"""`BatchProfileRenderer` — the public profile view, for a consumer that is
not an HTTP request.

A64-013.7 needs notification payloads rendered "through
`PublicProfileComposer`, respecting `VisibilityLevel`, `ViewerRelationship`
and blocking", from inside a background worker. Everything required to do
that already exists — `PublicProfileReader` fetches identities,
`PublicProfileComposer` applies the gate — and this class is the fifteen
lines that join them, published so `notifications` can use them without
importing either.

## Why the worker does not simply build a composer

Because it would have to know how one is built. `PublicProfileComposer`
takes four providers, two of which (`RatingProvider`, `StatisticsProvider`)
are `profiles`' own private ports; a consumer assembling that graph would be
a second composition root for this module's internals, drifting from the
real one the first time a provider is added.

What `notifications` gets instead is one method with one decision in it —
which relationship to render for — and no way to skip the gate.

## The relationship is the caller's assertion, and it is checked upstream

`render_many` takes a `ViewerRelationship` and hands it to `compose_many`'s
`known_relationship`, which is A64-013.4's short circuit: a caller that
already knows what every player on the page is to the viewer skips the
resolution query.

That is exactly the notification case and it is not a shortcut taken lightly.
The audience for a presence event is *friends minus blocked*
(`PresenceAudience`), so every recipient is a friend by construction and one
render serves all of them — which is what turns "render per recipient" into
"render once per event". The assertion is only ever safe where the audience's
membership **defines** the relationship, and `SocialNotificationDispatcher`
is where that argument is made per event type.
"""

import logging
from collections.abc import Mapping, Sequence
from uuid import UUID

from app.modules.profiles.application.services.profile_composer import PublicProfileComposer
from app.modules.profiles.domain.profile import PublicProfile
from app.modules.users.public import PublicProfileReader, ViewerRelationship

logger = logging.getLogger(__name__)


class BatchProfileRenderer:
    """Renders many players' public views under one asserted relationship."""

    def __init__(self, *, players: PublicProfileReader, composer: PublicProfileComposer) -> None:
        self._players = players
        self._composer = composer

    async def render_many(
        self, player_ids: Sequence[UUID], *, relationship: ViewerRelationship
    ) -> Mapping[UUID, PublicProfile]:
        """The public view of each player, keyed by id.

        **Four reads for any number of players**: one to fetch identities,
        three inside `compose_many` for ratings, statistics and presence.
        The count does not grow with the batch, which is what A64-013.7 asks
        for as "avoid N+1 profile rendering" and what makes a worker draining
        a backlog of fifty presence events cost the same as one.

        Players absent from the result are **deactivated or unknown**:
        `find_public_profiles` omits them, and this method does not
        substitute a placeholder. A notification about somebody with no
        public profile is one that must not be sent, and returning nothing
        for them is how that becomes the caller's obvious next step rather
        than a rendered tombstone.
        """
        if not player_ids:
            return {}

        identities = await self._players.find_public_profiles(player_ids)
        if not identities:
            return {}

        ordered = [identities[player_id] for player_id in player_ids if player_id in identities]
        composed = await self._composer.compose_many(ordered, known_relationship=relationship)
        return {profile.identity.id: profile for profile in composed}
