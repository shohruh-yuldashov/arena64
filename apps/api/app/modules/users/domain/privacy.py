"""`PrivacySettings` — which parts of a profile a stranger may see.

Framework-free like the rest of `domain/` (architecture.md §8). It holds
five booleans and one rule: they travel together.

## Why a value object rather than five columns read individually

domain-model.md §7.1 puts privacy preferences *inside* `UserProfile`, in a
named group beside gameplay, notification and locale preferences — "settings
are inside the profile, not beside it, per architecture.md §6". A type is
how that grouping survives contact with code: `user.privacy` is one thing to
pass to a mapper, one thing to default, and one thing to add a sixth flag to.

Five loose booleans on `User` would read the same at the database and behave
differently everywhere else — every function that needed them would take five
parameters, and the one that took four would be the bug.

Frozen, because a *setting* that a mapper could quietly rewrite while
rendering a profile is not a setting. Changing one produces a new value
through `updated()` and assigns it to the entity, so the only writer is the
use case that meant to write.

## Why `None` means "unchanged" in `updated()`

Everywhere else on this platform a partial update needs `UNSET`, because
`None` already means "clear it" (see `app.core.sentinels`). A boolean flag
has no cleared state — it is `True` or it is `False`, and an account always
has an answer — so `None` is free to mean "leave it alone" here without
colliding with anything. That is a property of the type rather than a
shortcut: adding a nullable field to this class later would break it, and
the field would need `UNSET` like every other three-state value.

## Why the defaults are what they are

A64-012.4 specifies them, and the asymmetry is the interesting part:

    show_country         True    a flag beside a name is what players
                                 expect of a chess site, and it is
                                 self-declared rather than observed
    show_statistics      True    a record is what an opponent uses to
                                 decide whether a challenge is worth
                                 accepting; hiding it by default would
                                 make every profile useless for matchmaking
    show_online_status   True    likewise — a challenge to a player who
                                 is not there is a challenge that expires
    show_activity        True
    show_last_seen       False   **the one default that is off**

`last_seen` is off by default because it is the only one of the five that
is a *timestamp of a person's behaviour* rather than a fact about their
account. "Online now" is coarse and momentary; "last seen 03:14" published
for months is a sleep schedule, a timezone and a work pattern, and it is
inferred rather than declared — the player never typed it. Defaults are
what most accounts will run on forever, so the field that leaks the most
per byte is the one that has to be opted into.

UP-4 is what makes any of this real: "Privacy preferences are enforced
server-side on every read path". These flags are read by the mappers that
build the public view, never by a client deciding what to draw.
"""

from dataclasses import dataclass

#: The platform defaults, named so the ORM's `server_default` and this
#: class's field defaults cannot drift — `infrastructure/models.py`
#: interpolates these exact constants, the way the CHECK constraints there
#: interpolate the length bounds (BE-06). A row inserted by a migration or
#: a repair script therefore gets the same answer as one inserted by the
#: application.
DEFAULT_SHOW_COUNTRY = True
DEFAULT_SHOW_LAST_SEEN = False
DEFAULT_SHOW_STATISTICS = True
DEFAULT_SHOW_ONLINE_STATUS = True
DEFAULT_SHOW_ACTIVITY = True


@dataclass(frozen=True, slots=True)
class PrivacySettings:
    """One account's answer to "what may a stranger see".

    Every flag is `True` for *visible* and `False` for *hidden*, with no
    exceptions and no inverted spellings. A `hide_country` beside a
    `show_statistics` is how a mapper ends up applying one of them
    backwards, and the mistake is invisible in review because both lines
    look correct on their own.
    """

    show_country: bool = DEFAULT_SHOW_COUNTRY
    """Whether the profile reports the player's country. Hidden means the
    field is `null`, indistinguishable from a player who never set one."""

    show_last_seen: bool = DEFAULT_SHOW_LAST_SEEN
    """Whether the profile reports when the player was last online. Off by
    default — see this module's docstring."""

    show_statistics: bool = DEFAULT_SHOW_STATISTICS
    """Whether the profile reports the aggregate match record — games,
    wins, losses, draws, win rate.

    **Ratings are deliberately not covered by this flag**, and it is not an
    oversight. UP-5: "profile visibility never hides *rated results* from
    the opponent of those results", and a rating is the platform's public
    statement of strength — it is what pairing is computed from and what
    every leaderboard already publishes. A player who could hide their
    rating while still accepting rated games would be sandbagging with the
    platform's help. The record of *how* they got there is discovery, which
    privacy governs; the number itself is not.
    """

    show_online_status: bool = DEFAULT_SHOW_ONLINE_STATUS
    """Whether the profile reports that the player is online right now.

    Nothing renders this today — presence lives in Redis behind a socket
    that AD-09's gateway has not yet opened (domain-model.md §141, §299).
    The flag is stored and published on `ProfileVisibility` so that whatever
    opens those sockets reads a decision the player already made, rather
    than shipping a presence feature and a privacy control for it in two
    separate releases with a gap in between.
    """

    show_activity: bool = DEFAULT_SHOW_ACTIVITY
    """Whether the profile reports recent activity — the match history and
    activity feed a profile page will show.

    Like `show_online_status`, stored and published ahead of the thing it
    governs, for the same reason: the release that adds a match list to the
    profile must not be the release that decides whether it is public.
    """

    def updated(
        self,
        *,
        show_country: bool | None = None,
        show_last_seen: bool | None = None,
        show_statistics: bool | None = None,
        show_online_status: bool | None = None,
        show_activity: bool | None = None,
    ) -> "PrivacySettings":
        """A copy with the named flags replaced; `None` leaves one alone.

        Keyword-only, because five positional booleans is a call nobody can
        read and a transposition nobody can see. `dataclasses.replace` with
        a filtered `**kwargs` would be shorter and would type as `Any` at
        every call site — the explicit signature is what makes a misspelled
        flag a type error rather than a silently ignored key.
        """
        return PrivacySettings(
            show_country=self.show_country if show_country is None else show_country,
            show_last_seen=self.show_last_seen if show_last_seen is None else show_last_seen,
            show_statistics=self.show_statistics if show_statistics is None else show_statistics,
            show_online_status=(
                self.show_online_status if show_online_status is None else show_online_status
            ),
            show_activity=self.show_activity if show_activity is None else show_activity,
        )
