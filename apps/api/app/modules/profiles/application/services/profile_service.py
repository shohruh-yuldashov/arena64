"""`ProfileService` — read a public profile by username, and read an
owner's own record.

Orchestrates; does not compute (services.md §3.2). Identity comes from
`users` through a published port, ratings and counts from two more, and
`win_rate` is the domain's. What lives here is the sequencing and two
decisions that are nowhere else: **who is visible**, and **what a missing
player looks like**.

Read-only. Opens no transaction — there is nothing to commit, and a unit
of work around three reads would be ceremony that suggests otherwise.

## Deactivated accounts have no public profile

`is_active=False` yields `ProfileNotFound`, identical in every respect to a
username nobody has ever registered — and this module does not implement
that rule, which is worth saying plainly because the code below looks like
somewhere it would live.

`users` enforces it, inside `PublicProfileReader`, because `users` owns
`is_active`. Enforcing it here would require the flag on
`PublicUserProfile` so this service could read it, and "which accounts are
deactivated" is itself a disclosure — an impersonator choosing whom to
imitate wants exactly that list. So the flag is not published, a withdrawn
account arrives as `None`, and it takes the same branch below as a
username nobody ever registered.

Identical rather than merely similar matters. A distinct 403, or a 404 with
a different message, would still answer the question.

## Privacy is applied here, and the endpoint knows nothing about it

A64-012.4 requires that "privacy filtering must happen inside the
PublicProfileReader / mapper layer" and that "endpoints must not manually
hide fields". This module is the second half of that, and the split with
`users` follows ownership exactly:

    country          `users` redacts it before it crosses the port. This
                     module never sees a hidden one.
    statistics       composed here, from a source `users` does not own —
                     so `users` publishes the decision on
                     `PublicUserProfile.visibility` and this service
                     declines to fetch it.
    last_seen        same shape, nothing to fetch yet.

By the time `ProfileResponse.of` runs there is nothing left to hide: a
hidden statistic is already `None`, and the router does not receive a flag
it could act on even if it wanted to. That is what makes "endpoints must
not manually hide fields" structural rather than a rule somebody has to
remember — see `presentation/router.py`, which has no privacy logic in it.

## Why the three reads are sequential rather than concurrent

Identity is fetched first and the other two only if it resolved. That
ordering is not incidental: fetching ratings for a username that does not
exist would be work done on behalf of a scraper, and on the platform's most
enumerable endpoint the cheapest possible rejection is the right one.

The two remaining reads are independent and could run concurrently with
`asyncio.gather`. They do not, today, because both are in-process constants
— gathering them would add a scheduling round trip to save nothing. When
either becomes a real network read this is the place that changes, and the
comment at the call site says so.
"""

import logging
from uuid import UUID

from app.modules.profiles.application.ports import RatingProvider, StatisticsProvider
from app.modules.profiles.domain.exceptions import ProfileNotFound
from app.modules.profiles.domain.profile import PublicProfile
from app.modules.statistics.public import PlayerStatistics
from app.modules.users.public import PublicProfileReader

logger = logging.getLogger(__name__)

_GENERIC_REJECTION = "No profile found for that username."


class ProfileService:
    def __init__(
        self,
        *,
        profiles: PublicProfileReader,
        ratings: RatingProvider,
        statistics: StatisticsProvider,
    ) -> None:
        self._profiles = profiles
        self._ratings = ratings
        self._statistics = statistics

    async def get_public_profile(self, username: str) -> PublicProfile:
        """Composes the public view of the player holding `username`.

        **Case-insensitive** (UP-1). `Alice`, `alice` and `ALICE` resolve
        to the same account, and the profile reports the casing that player
        chose — matching on the folded form is `users`' repository's job,
        which is why this method does no normalisation of its own.

        Raises `ProfileNotFound` (404) when no visible profile exists —
        whether the username was never registered, or belongs to a
        deactivated account. The two are deliberately indistinguishable;
        see this module's docstring.
        """
        identity = await self._profiles.find_public_profile(username)

        if identity is None:
            # Neither the username nor the reason. A miss is the ordinary
            # outcome on a public endpoint, and this line exists to be
            # *counted* — a rate of misses from one caller is the shape of
            # enumeration — not to record what was asked for. Logging the
            # requested name would turn the access log into the very list
            # of probed usernames the endpoint declines to confirm.
            logger.info("profile_lookup_missed")
            raise ProfileNotFound(_GENERIC_REJECTION)

        visibility = identity.visibility

        # Both reads are in-process today. When either becomes a network
        # call, this is where `asyncio.gather` belongs — they are
        # independent of each other and both depend only on the id.
        ratings = await self._ratings.ratings_for(identity.id)

        # **Not fetched at all when hidden**, rather than fetched and
        # discarded. Two reasons, and the second is the one that lasts: a
        # value that is never loaded cannot be leaked by a later mapper
        # that forgets the flag, and once `statistics` is a real service
        # this is a cross-context read skipped entirely for every player
        # who opted out.
        statistics = (
            await self._statistics.statistics_for(identity.id) if visibility.statistics else None
        )

        # The player id, never the username. An id joins to everything for
        # anyone entitled to see it, and keeps a permanent access record
        # from being a searchable index of who looked at whom
        # (services.md §8.5).
        logger.info("profile_lookup_succeeded", extra={"user_id": str(identity.id)})

        return PublicProfile(
            identity=identity,
            ratings=ratings,
            statistics=statistics,
            # `last_seen` stays at its default of `None`, for two reasons
            # now rather than one: presence is not implemented (A64-012.1),
            # and `visibility.last_seen` is off for most accounts anyway
            # (A64-012.4's one default-off flag). A gate written here today
            # would be a branch whose arms are both `None` — dead code
            # dressed as enforcement. The real read belongs where the
            # statistics read is, guarded the same way, and the flag is in
            # place waiting for it. See `PublicProfile.last_seen`.
        )

    async def get_own_statistics(self, player_id: UUID) -> PlayerStatistics:
        """The account holder's own record — **never redacted.**

        A64-012.6: "only profile owners may always see their own
        statistics." `show_statistics` governs what a *stranger* sees, and
        a settings screen that hid a player's record from the player would
        be a control nobody could verify they had set.

        So this deliberately does not consult `visibility` and deliberately
        takes a `player_id` rather than a username: the only caller is
        `GET /profile/me`, which has already authenticated the id it passes
        and cannot name a different one.

        Reads through the same `StatisticsProvider` the public path uses,
        which is what keeps the fallback honest — a deployment with
        statistics switched off reports the empty record to owner and
        stranger alike rather than only to one of them.

        Never raises for a player with no history: the port returns the
        empty record, which is the correct answer for a new account.
        """
        statistics = await self._statistics.statistics_for(player_id)

        # Distinguishable from `profile_lookup_succeeded` above because the
        # two answer different audit questions: that one is "somebody
        # looked at this player", this one is "this player looked at
        # themselves". Id only, no counts — the numbers are in the response
        # the caller already holds (services.md §8.5).
        logger.info("own_statistics_lookup", extra={"user_id": str(player_id)})

        return statistics
