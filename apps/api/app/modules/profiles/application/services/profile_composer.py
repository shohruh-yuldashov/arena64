"""`PublicProfileComposer` — identity plus the three things `users` does
not own, with privacy applied.

Extracted from `ProfileService` by A64-013.1, which added a second read path
that has to produce the same thing: search results and profile pages are the
same public view of the same player, and A64-013.1 requires that literally —
"search results must use the same public representation as profile pages".

## Why this is a class of its own rather than two methods on `ProfileService`

Because of what would have happened otherwise, which is worth naming
precisely. `ProfileService.get_public_profile` held four lines that decide
**which privacy flag gates which field**. A search service written beside it
would have held four more, and they would have agreed on the day they were
written. The failure mode is not that somebody copies them wrongly — it is
that a *sixth* privacy flag arrives, gets wired into one of the two, and the
other keeps serving the field for a year.

CLAUDE.md §3.4: one source of truth per concept. The concept here is "what a
stranger may see of a player", and this class is now the only place it is
expressed.

## The two entry points differ in fetching, never in deciding

`compose` and `compose_many` both end in `_assemble`, which is the gate.
What differs above it is how many round trips the sources cost:

    compose        three singular reads — one profile, one player
    compose_many   three batched reads — one page, however many players

They are not collapsed into one (`compose` delegating to `compose_many` with
a one-element list) even though that would be shorter and would make the
shared path even more obviously shared. Two reasons, and the second is the
one that decided it:

  - the singular providers emit per-player log lines the batch ones
    deliberately do not (`statistics_lookup_succeeded` is `INFO` for one
    profile and would be noise twenty times over for a page), so collapsing
    would silently change what the platform's most-read endpoint records;
  - `GET /profiles/{username}` is that endpoint, and routing it through a
    batch path to save a few lines here would be paying its latency budget
    for this module's tidiness.

## Not fetched is stronger than fetched and discarded

Every gate below decides *whether to read*, not *whether to render*. A
statistic that is never loaded cannot be leaked by a later mapper that
forgets a flag, and on a page of fifty it means the platform does no work at
all on behalf of the players who opted out — which is the difference between
a privacy setting and a privacy label.
"""

import logging
from collections.abc import Mapping, Sequence
from uuid import UUID

from app.modules.profiles.application.ports import (
    RatingProvider,
    StatisticsProvider,
    ViewerRelationshipProvider,
)
from app.modules.profiles.domain.profile import PublicProfile
from app.modules.profiles.domain.ratings import PlayerRatings
from app.modules.statistics.public import PlayerStatistics
from app.modules.users.public import (
    Presence,
    PresenceProvider,
    PublicUserProfile,
    ViewerRelationship,
)

logger = logging.getLogger(__name__)


class PublicProfileComposer:
    """Turns `users`' published identity into the platform's public view.

    Holds no repository, no session and no privacy flag of its own: the
    flags arrive on `PublicUserProfile.visibility`, decided by `users`,
    which owns them. This class only *applies* them.
    """

    def __init__(
        self,
        *,
        ratings: RatingProvider,
        statistics: StatisticsProvider,
        presence: PresenceProvider,
        relationships: ViewerRelationshipProvider,
    ) -> None:
        self._ratings = ratings
        self._statistics = statistics
        self._presence = presence
        # A64-013.3. Typed as the port, so nothing here can learn that a
        # `friends` module exists: this class cannot name a friendship, a
        # table or a repository, and cannot tell the real provider from the
        # fallback.
        self._relationships = relationships

    async def compose(
        self,
        identity: PublicUserProfile,
        *,
        viewer_id: UUID | None = None,
    ) -> PublicProfile:
        """One player, through the singular providers.

        The path `GET /profiles/{username}` takes. Reads are sequential
        rather than gathered because all three are in-process or a single
        indexed read today; when one becomes a slow network call this is
        where `asyncio.gather` belongs, and the four are independent.

        `viewer_id` is the **authenticated caller**, or `None` for an
        anonymous one. A64-013.3 replaced the `viewer: ViewerRelationship`
        parameter this used to take, and the change is not cosmetic: a
        relationship is a fact about a *pair*, so a caller that passed one
        in would have to compute it — and on the batch path it would have to
        compute a different one per player. Taking the viewer's id and
        resolving here is what keeps that single-sourced.

        `None` resolves nothing and issues no query: a signed-out visitor
        has no relationships, and every setting evaluates against
        `STRANGER`.
        """
        visibility = identity.visibility

        ratings = await self._ratings.ratings_for(identity.id)

        statistics = (
            await self._statistics.statistics_for(identity.id) if visibility.statistics else None
        )

        viewer = await self._relationship_to(viewer_id, identity.id)

        presence: Presence | None = None
        if _wants_presence(identity, viewer):
            presence = await self._presence.presence_for(identity.id)
            logger.debug(
                "presence_lookup",
                extra={"user_id": str(identity.id), "observed": presence is not None},
            )
        else:
            logger.debug("presence_lookup_skipped", extra={"user_id": str(identity.id)})

        return _assemble(
            identity, ratings=ratings, statistics=statistics, presence=presence, viewer=viewer
        )

    async def compose_many(
        self,
        identities: Sequence[PublicUserProfile],
        *,
        viewer_id: UUID | None = None,
        known_relationship: ViewerRelationship | None = None,
    ) -> list[PublicProfile]:
        """A page of players, in a fixed number of round trips.

        **Three reads for any page size**, and the count does not grow with
        it — that is the whole reason this method exists rather than a loop
        over `compose`. A page of fifty through the singular path would be
        a hundred and fifty round trips to render one screen.

        The ids sent to each source are **filtered by that source's own
        flag**, so a page of players who have all hidden their record costs
        no statistics query at all, and a page where nobody publishes
        presence costs no Redis command. The filtering is the same gate
        `compose` applies, one level up.

        **The relationship is resolved per player, not per page** —
        A64-013.3. A search result or a friend-request list mixes friends
        and strangers, so one relationship applied to the whole page would
        either publish a friends-only field to strangers or hide it from
        friends. That resolution happens first, because the presence filter
        below depends on it.

        `known_relationship` is the A64-013.4 short circuit: a caller that
        already knows what every player on the page is to the viewer passes
        it and the resolution query is skipped entirely. The friend list is
        the case — every player in it is, by definition, a friend — and
        resolving that from the social graph would be asking a question the
        caller answered in order to build the page.

        It is an **assertion by the caller**, not a hint, so it is only ever
        correct where the page's membership *defines* the relationship.
        Search results and request lists must not pass it: those pages mix
        friends and strangers, and one relationship applied to all of them
        would either publish a friends-only field to a stranger or hide it
        from a friend.

        Order is preserved: the ranking is the caller's and must survive
        composition unchanged.
        """
        if not identities:
            # No sources consulted for an empty page — the ordinary result
            # of a search nobody matched. Every provider below tolerates an
            # empty sequence, so this is an early return for clarity rather
            # than for correctness.
            return []

        relationships = await self._relationships_to(
            viewer_id, [one.id for one in identities], known=known_relationship
        )

        statistics_ids = [one.id for one in identities if one.visibility.statistics]
        presence_ids = [one.id for one in identities if _wants_presence(one, relationships[one.id])]

        statistics = await self._statistics.statistics_for_many(statistics_ids)
        presence = await self._presence.presence_for_many(presence_ids)
        ratings = await self._ratings.ratings_for_many([one.id for one in identities])

        # Counts, never ids or values. A search log that recorded which
        # players were returned would be a record of who looked for whom,
        # which is more sensitive than the profiles it leads to
        # (services.md §8.5).
        logger.debug(
            "profile_batch_composed",
            extra={
                "players": len(identities),
                "statistics_visible": len(statistics_ids),
                "presence_visible": len(presence_ids),
                "presence_observed": len(presence),
                # How many of the page the viewer is friends with. A count,
                # never the ids — which players somebody is friends with is
                # precisely what `VisibilityLevel.FRIENDS` exists to control,
                # so it must not be reassembled from a log (services.md §8.5).
                "friends_in_page": sum(
                    1 for level in relationships.values() if level is ViewerRelationship.FRIEND
                ),
            },
        )

        return [
            _assemble(
                identity,
                ratings=ratings[identity.id],
                # `.get` rather than `[]` for statistics too: the mapping is
                # complete for the ids *asked for*, and a player who hid
                # their record was never in that list.
                statistics=statistics.get(identity.id),
                presence=presence.get(identity.id),
                viewer=relationships[identity.id],
            )
            for identity in identities
        ]

    async def _relationship_to(self, viewer_id: UUID | None, player_id: UUID) -> ViewerRelationship:
        """What `viewer_id` is to one player.

        Delegates to the batch form with a one-element sequence rather than
        reaching for a singular port, because there is no singular port —
        see `ViewerRelationshipProvider` on why its absence is the design.
        """
        relationships = await self._relationships_to(viewer_id, [player_id])
        return relationships[player_id]

    async def _relationships_to(
        self,
        viewer_id: UUID | None,
        player_ids: Sequence[UUID],
        *,
        known: ViewerRelationship | None = None,
    ) -> Mapping[UUID, ViewerRelationship]:
        """What `viewer_id` is to each player, defaulting to `STRANGER`.

        Two short circuits, and both exist because the answer is already
        known rather than to make anything faster in the abstract:

          **An anonymous viewer costs no query.** `None` means signed out,
          which has no relationships by definition. That matters most: the
          anonymous path is `GET /profiles/{username}`, the platform's
          highest-volume read.

          **A stated relationship costs no query** (A64-013.4). The friend
          list already knows every player on the page is a friend, and
          `friend_ids_among` is now on the composition path for every render
          — so asking it to confirm what the page's own membership
          guarantees is the "unnecessary query" that requirement names.

        `known` is checked before `viewer_id`, which is not arbitrary: a
        caller stating a relationship has, by stating it, told us there is a
        viewer.
        """
        if known is not None:
            return dict.fromkeys(player_ids, known)
        if viewer_id is None:
            return dict.fromkeys(player_ids, ViewerRelationship.STRANGER)
        return await self._relationships.relationships_for(viewer_id, player_ids)


def _wants_presence(identity: PublicUserProfile, viewer: ViewerRelationship) -> bool:
    """Whether either presence field is visible to `viewer`.

    One read serves two flags. `show_online_status` and `show_last_seen`
    govern different fields of the same record and have different defaults
    — the second is the only privacy flag on the platform that is off out
    of the box — so presence is fetched when *either* is on and each field
    is gated separately afterwards.

    Fetching per field would be two reads of one key to answer one
    question; folding them into one flag would either publish a timestamp
    for a player who only agreed to an indicator, or withhold the indicator
    from the great majority of accounts running on the defaults.
    """
    visibility = identity.visibility
    return visibility.online_status.permits(viewer) or visibility.last_seen.permits(viewer)


def _assemble(
    identity: PublicUserProfile,
    *,
    ratings: PlayerRatings,
    statistics: PlayerStatistics | None,
    presence: Presence | None,
    viewer: ViewerRelationship,
) -> PublicProfile:
    """**The gate.** The one place a privacy flag becomes a `None`.

    Everything above this function decides what to *fetch*; this decides
    what to *render*, and the two must agree — a field fetched but not
    gated here would be a leak, and a field gated here but fetched anyway
    is work done on behalf of somebody who opted out. Both entry points end
    here, so there is one answer rather than two that agree until they do
    not.

    Takes already-resolved values rather than the providers, which is what
    keeps it synchronous, total and trivially testable: given an identity
    and three optional values there is exactly one `PublicProfile`, with no
    I/O to arrange in order to assert it.

    `statistics` arrives `None` for a player who hid it *and* for a player
    the caller declined to look up, which are the same thing by
    construction — nothing above ever fetches a hidden record.
    """
    visibility = identity.visibility

    return PublicProfile(
        identity=identity,
        ratings=ratings,
        statistics=statistics,
        # Each presence field gated by its own flag, from the one record.
        # A player showing an indicator but not a timestamp — which is what
        # the platform defaults produce — gets `is_online` and a `None`
        # `last_seen`, and nothing in the response says which of the four
        # reasons for that `None` applies.
        last_seen=(
            presence.last_seen if presence and visibility.last_seen.permits(viewer) else None
        ),
        is_online=(
            presence.is_online if presence and visibility.online_status.permits(viewer) else None
        ),
    )
