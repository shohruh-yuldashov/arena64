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

    show_country      True       a flag beside a name is what players
                                 expect of a chess site, and it is
                                 self-declared rather than observed
    show_statistics   True       a record is what an opponent uses to
                                 decide whether a challenge is worth
                                 accepting; hiding it by default would
                                 make every profile useless for matchmaking
    online_status     EVERYONE   likewise — a challenge to a player who
                                 is not there is a challenge that expires
    activity          EVERYONE
    last_seen         NOBODY     **the one default that is closed**

`last_seen` is closed by default because it is the only one of the five
that is a *timestamp of a person's behaviour* rather than a fact about
their account. "Online now" is coarse and momentary; "last seen 03:14"
published for months is a sleep schedule, a timezone and a work pattern,
and it is inferred rather than declared — the player never typed it.
Defaults are what most accounts will run on forever, so the field that
leaks the most per byte is the one that has to be opted into.

A64-013.2 widened three of the five from booleans to `VisibilityLevel`
without moving any of them: `EVERYONE` is what `True` meant and `NOBODY` is
what `False` meant, so every account's effective settings are unchanged.
See `domain/visibility.py` for which three and why.

UP-4 is what makes any of this real: "Privacy preferences are enforced
server-side on every read path". These settings are read by the mappers and
the composer that build the public view, never by a client deciding what to
draw.
"""

from dataclasses import dataclass

from app.modules.users.domain.visibility import ViewerRelationship, VisibilityLevel

#: The platform defaults, named so the ORM's `server_default` and this
#: class's field defaults cannot drift — `infrastructure/models.py`
#: interpolates these exact constants, the way the CHECK constraints there
#: interpolate the length bounds (BE-06). A row inserted by a migration or
#: a repair script therefore gets the same answer as one inserted by the
#: application.
DEFAULT_SHOW_COUNTRY = True
DEFAULT_SHOW_STATISTICS = True

#: The three audience-valued defaults (A64-013.2). Each is the widening of
#: the boolean default it replaces, so an account created before this task
#: and one created after it are indistinguishable — which is what makes the
#: migration lossless in both directions.
DEFAULT_LAST_SEEN = VisibilityLevel.NOBODY
DEFAULT_ONLINE_STATUS = VisibilityLevel.EVERYONE
DEFAULT_ACTIVITY = VisibilityLevel.EVERYONE


@dataclass(frozen=True, slots=True)
class PrivacySettings:
    """One account's answer to "what may other players see".

    **Two kinds of field since A64-013.2**, and the split is the brief's
    "apply this only where future friend-based visibility requires it":

        show_country, show_statistics       still booleans
        last_seen, online_status, activity  `VisibilityLevel`

    Every boolean is `True` for *visible*, with no exceptions and no
    inverted spellings — a `hide_country` beside a `show_statistics` is how
    a mapper ends up applying one backwards, and the mistake is invisible in
    review because both lines look correct on their own.

    The three audience-valued fields dropped the `show_` prefix with their
    type. A `show_last_seen: VisibilityLevel` would read as a boolean at
    every call site and would be assigned one by somebody eventually;
    renaming makes the type change impossible to miss in review, which is
    worth more than a smaller diff. See `domain/visibility.py` for why only
    these three moved.
    """

    show_country: bool = DEFAULT_SHOW_COUNTRY
    """Whether the profile reports the player's country. Hidden means the
    field is `null`, indistinguishable from a player who never set one.

    Still a boolean, deliberately: a flag beside a name is either published
    or it is not, and database.md does not list it among the audience-valued
    settings.
    """

    show_statistics: bool = DEFAULT_SHOW_STATISTICS
    """Whether the profile reports the aggregate match record — games,
    wins, losses, draws, win rate.

    **Ratings are deliberately not covered by this flag**, and it is not an
    oversight. UP-5: "profile visibility never hides *rated results* from
    the opponent of those results", and a rating is the platform's public
    statement of strength. The record of *how* a player got there is
    discovery, which privacy governs; the number itself is not.

    Still a boolean for a related reason: UP-5 means a friends-only match
    record is a control the platform could not honour, since the opponents
    who produced those results are entitled to see them whether or not they
    are friends.
    """

    last_seen: VisibilityLevel = DEFAULT_LAST_SEEN
    """Who may see when the player was last online.

    `NOBODY` by default — the one setting of the five that is closed out of
    the box, because it is the only one that is a *timestamp of a person's
    behaviour* rather than a fact about their account. "Online now" is
    coarse and momentary; "last seen 03:14" published for months is a sleep
    schedule, a timezone and a work pattern, and it is inferred rather than
    declared.
    """

    online_status: VisibilityLevel = DEFAULT_ONLINE_STATUS
    """Who may see that the player is online right now.

    `EVERYONE` by default: a challenge to a player who is not there is a
    challenge that expires, so hiding presence by default would make every
    profile useless for matchmaking.

    The setting database.md §491 names outright (`online_status_to`), and
    the reason this task widened the type rather than waiting.
    """

    activity: VisibilityLevel = DEFAULT_ACTIVITY
    """Who may see recent activity — the match history and activity feed a
    profile page will show.

    Stored and published ahead of the thing it governs, as `online_status`
    was before presence existed: the release that adds a match list to the
    profile must not also be the release that decides who may read it.
    """

    def updated(
        self,
        *,
        show_country: bool | None = None,
        show_statistics: bool | None = None,
        last_seen: VisibilityLevel | None = None,
        online_status: VisibilityLevel | None = None,
        activity: VisibilityLevel | None = None,
    ) -> "PrivacySettings":
        """A copy with the named settings replaced; `None` leaves one alone.

        Keyword-only, because five positional arguments — two of them
        booleans — is a call nobody can read and a transposition nobody can
        see. `dataclasses.replace` with a filtered `**kwargs` would be
        shorter and would type as `Any` at every call site; the explicit
        signature is what makes a misspelled setting a type error rather
        than a silently ignored key.

        `None` still means *unchanged* rather than *cleared*, and the
        argument for it survived the type change intact: not one field here
        is nullable. A boolean is `True` or `False` and a `VisibilityLevel`
        is one of three members, so an account always has an answer and
        `None` is free to mean "leave it alone" without colliding with a
        real value. `app.core.sentinels.UNSET` remains what a genuinely
        nullable field needs.
        """
        return PrivacySettings(
            show_country=self.show_country if show_country is None else show_country,
            show_statistics=self.show_statistics if show_statistics is None else show_statistics,
            last_seen=self.last_seen if last_seen is None else last_seen,
            online_status=self.online_status if online_status is None else online_status,
            activity=self.activity if activity is None else activity,
        )

    def permits_last_seen(self, viewer: ViewerRelationship) -> bool:
        """Whether `viewer` may see the last-seen timestamp.

        Three named methods rather than callers reaching for
        `settings.last_seen.permits(viewer)` directly. The indirection is
        one line each and buys the property that matters: a consumer never
        holds a `VisibilityLevel` it could compare against a member by hand,
        so `is VisibilityLevel.EVERYONE` cannot appear at a call site and
        quietly stop honouring `FRIENDS`.
        """
        return self.last_seen.permits(viewer)

    def permits_online_status(self, viewer: ViewerRelationship) -> bool:
        """Whether `viewer` may see the online indicator. See
        `permits_last_seen`."""
        return self.online_status.permits(viewer)

    def permits_activity(self, viewer: ViewerRelationship) -> bool:
        """Whether `viewer` may see recent activity. See
        `permits_last_seen`."""
        return self.activity.permits(viewer)
