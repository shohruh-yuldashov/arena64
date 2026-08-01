"""`ProfileService` — read a public profile by username, and read an
owner's own record.

Orchestrates; does not compute (services.md §3.2). Identity comes from
`users` through a published port and the public view is assembled by
`PublicProfileComposer`. What lives here is a username lookup and two
decisions that are nowhere else: **what a missing player looks like**, and
**what an account holder may see of themselves that a stranger may not**.

Read-only. Opens no transaction — there is nothing to commit, and a unit
of work around a read would be ceremony that suggests otherwise.

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

## Privacy is applied by the composer, and nothing here knows about it

A64-012.4 requires that "privacy filtering must happen inside the
PublicProfileReader / mapper layer" and that "endpoints must not manually
hide fields". The split with `users` follows ownership exactly:

    country          `users` redacts it before it crosses the port. Nothing
                     outside `users` ever sees a hidden one.
    statistics       composed from a source `users` does not own — so
                     `users` publishes the *decision* on
                     `PublicUserProfile.visibility` and the composer
                     declines to fetch the value.
    is_online        same shape, from `PresenceProvider` (A64-012.7).
    last_seen        same shape, from the same single read.

**A64-013.1 moved the second half out of this class.** It used to live in
`get_public_profile` as four lines that decided which flag gated which
field; it now lives in `PublicProfileComposer`, because user search became a
second path producing the same view and a privacy gate with two
implementations is a privacy gate that eventually disagrees with itself.

By the time `ProfileResponse.of` runs there is still nothing left to hide,
and the router still receives no flag it could act on. What changed is that
there is now exactly one place to look for the rule rather than one place
per endpoint.

## Why identity is resolved before anything else

Identity is fetched first and the composition runs only if it resolved.
That ordering is not incidental: composing a profile for a username that
does not exist would be work done on behalf of a scraper, and on the
platform's most enumerable endpoint the cheapest possible rejection is the
right one.

What the composition then costs, and in what order, is
`PublicProfileComposer`'s business rather than this module's.

"""

import logging
from uuid import UUID

from app.modules.profiles.application.ports import StatisticsProvider
from app.modules.profiles.application.services.profile_composer import PublicProfileComposer
from app.modules.profiles.domain.exceptions import ProfileNotFound
from app.modules.profiles.domain.profile import PublicProfile
from app.modules.statistics.public import PlayerStatistics
from app.modules.users.public import Presence, PresenceProvider, PublicProfileReader

logger = logging.getLogger(__name__)

_GENERIC_REJECTION = "No profile found for that username."


class ProfileService:
    def __init__(
        self,
        *,
        profiles: PublicProfileReader,
        composer: PublicProfileComposer,
        statistics: StatisticsProvider,
        presence: PresenceProvider,
    ) -> None:
        self._profiles = profiles
        # Composition and every privacy gate. A64-013.1 replaced the three
        # providers this service used to hold with the one object that
        # already holds them, because search needs the identical
        # composition and two copies of a privacy gate is one copy too
        # many. This service is left with what is genuinely its own: the
        # username lookup, and the two decisions in this module's docstring.
        self._composer = composer
        # Kept **beside** the composer rather than reached through it,
        # because the two owner-only reads below are not compositions: they
        # deliberately bypass every privacy flag, which is the one thing the
        # composer must never do. Routing them through it would mean giving
        # it an "ignore privacy" mode, and a gate with a bypass is not a
        # gate.
        self._statistics = statistics
        # The **reader**, never `PresenceRecorder`. The two are separate
        # published ports precisely so that the module behind the platform's
        # only anonymous endpoint cannot assert that somebody is online, and
        # this attribute is where that would otherwise stop being true.
        self._presence = presence

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

        # The player id, never the username. An id joins to everything for
        # anyone entitled to see it, and keeps a permanent access record
        # from being a searchable index of who looked at whom
        # (services.md §8.5).
        logger.info("profile_lookup_succeeded", extra={"user_id": str(identity.id)})

        # **Composition, including every privacy gate, lives in the
        # composer.** A64-013.1 moved it there when user search became a
        # second path that has to produce the same view — see
        # `PublicProfileComposer` on why the gate has exactly one home.
        return await self._composer.compose(identity)

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

    async def get_own_presence(self, player_id: UUID) -> Presence | None:
        """The account holder's own presence — **never redacted.**

        A64-012.7: "authenticated users may always view their own presence
        information." `show_online_status` and `show_last_seen` govern what a
        *stranger* sees; a settings screen that hid a player's own presence
        from them would be a control nobody could verify they had set — the
        argument `get_own_statistics` above makes, applied to the two flags
        beside `show_statistics`.

        So this deliberately does not consult `visibility`, and deliberately
        takes a `player_id` rather than a username: the only caller is
        `GET /profile/me`, which has already authenticated the id it passes
        and cannot name a different one. There is no path by which this
        returns somebody else's presence, which is why there is no ownership
        check in it.

        Reads through the same `PresenceProvider` the public path uses,
        which is what keeps the fallback honest — a deployment with presence
        switched off reports "unknown" to owner and stranger alike rather
        than only to one of them.

        `None` for an owner nobody has observed, which today is everybody:
        nothing writes presence until AD-09's gateway does. Unredacted does
        not mean invented.
        """
        presence = await self._presence.presence_for(player_id)

        # Distinguishable from `presence_lookup` above because the two answer
        # different questions: that one is "somebody looked at this player's
        # presence", this one is "this player looked at their own". Id and
        # whether there was a record — never the value, for the reason that
        # one gives.
        logger.debug(
            "own_presence_lookup",
            extra={"user_id": str(player_id), "observed": presence is not None},
        )

        return presence
