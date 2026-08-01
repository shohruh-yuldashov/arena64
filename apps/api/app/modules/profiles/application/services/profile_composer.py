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
from collections.abc import Sequence

from app.modules.profiles.application.ports import RatingProvider, StatisticsProvider
from app.modules.profiles.domain.profile import PublicProfile
from app.modules.profiles.domain.ratings import PlayerRatings
from app.modules.statistics.public import PlayerStatistics
from app.modules.users.public import Presence, PresenceProvider, PublicUserProfile

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
    ) -> None:
        self._ratings = ratings
        self._statistics = statistics
        self._presence = presence

    async def compose(self, identity: PublicUserProfile) -> PublicProfile:
        """One player, through the singular providers.

        The path `GET /profiles/{username}` takes. Reads are sequential
        rather than gathered because all three are in-process or a single
        indexed read today; when one becomes a slow network call this is
        where `asyncio.gather` belongs, and the three are independent.
        """
        visibility = identity.visibility

        ratings = await self._ratings.ratings_for(identity.id)

        statistics = (
            await self._statistics.statistics_for(identity.id) if visibility.statistics else None
        )

        presence: Presence | None = None
        if _wants_presence(identity):
            presence = await self._presence.presence_for(identity.id)
            logger.debug(
                "presence_lookup",
                extra={"user_id": str(identity.id), "observed": presence is not None},
            )
        else:
            logger.debug("presence_lookup_skipped", extra={"user_id": str(identity.id)})

        return _assemble(identity, ratings=ratings, statistics=statistics, presence=presence)

    async def compose_many(self, identities: Sequence[PublicUserProfile]) -> list[PublicProfile]:
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

        Order is preserved: the ranking is the caller's and must survive
        composition unchanged.
        """
        if not identities:
            # No sources consulted for an empty page — the ordinary result
            # of a search nobody matched. Every provider below tolerates an
            # empty sequence, so this is an early return for clarity rather
            # than for correctness.
            return []

        statistics_ids = [one.id for one in identities if one.visibility.statistics]
        presence_ids = [one.id for one in identities if _wants_presence(one)]

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
            )
            for identity in identities
        ]


def _wants_presence(identity: PublicUserProfile) -> bool:
    """Whether either presence field is visible for this player.

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
    return visibility.online_status or visibility.last_seen


def _assemble(
    identity: PublicUserProfile,
    *,
    ratings: PlayerRatings,
    statistics: PlayerStatistics | None,
    presence: Presence | None,
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
        last_seen=presence.last_seen if presence and visibility.last_seen else None,
        is_online=presence.is_online if presence and visibility.online_status else None,
    )
