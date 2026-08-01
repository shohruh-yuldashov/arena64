"""`VisibilityLevel` and `ViewerRelationship` — who may see a thing, and who
is asking.

Framework-free (architecture.md §8). Two closed sets and one function
relating them, which is the whole audience model: a setting names an
audience, a request carries a relationship, and `permits` decides.

## Why the booleans had to go

A64-012.4 shipped five `show_*` booleans, and they were correct for a
platform with no social graph — with nobody to be a friend of, "everyone or
nobody" is the complete set of answers.

A64-013.2 introduces friend requests, so the graph is arriving, and
database.md §491 has always specified the real shape:
`challenge_from`, `direct_message_from` and `online_status_to` as
`users.audience` with `everyone | friends | nobody`. A boolean cannot
express the middle value, and — this is the part that decided the timing —
**adding it later is a migration of live rows plus a change to every read
path**, while adding it now is a migration of rows that mean exactly what
the boolean meant.

Doing it before the friend graph exists is what keeps `true -> EVERYONE,
false -> NOBODY` a lossless conversion. After friendships exist, somebody
has to decide what an existing `true` *should* have meant.

## Why only three settings changed

`show_country` and `show_statistics` are still booleans, and that is the
brief's "apply this only where future friend-based visibility requires it"
rather than an oversight:

    country       a self-declared profile field, not a social signal. A
                  flag beside a name is either published or not; "my
                  friends may see which country I am in" is not a control
                  anybody has asked for, and database.md does not list it.
    statistics    UP-5 keeps rated results visible to the opponents whose
                  results produced them, so a friends-only match record
                  would be a control the platform could not honour. If it
                  ever becomes audience-valued, that is a product decision
                  with an argument, not a mechanical widening.

The three that changed — `online_status`, `last_seen`, `activity` — are
exactly the ones a friend list gates, and `online_status` is the one
database.md names outright.
"""

from enum import StrEnum


class ViewerRelationship(StrEnum):
    """What the person reading a profile is to the person it describes.

    Computed per request by whatever composes a public view, never stored.
    A relationship is a fact about a *pair*, and storing it on either side
    is the mistake `Friendship` avoids by being its own aggregate
    (domain-model.md §8.2).

    **Two members today, and a third and fourth are already spoken for.**
    `BLOCKED` arrives with A64-013.5 and is the reason this is an enum
    rather than a boolean `is_friend`: BL-2 makes blocking suppress things
    a stranger can still see, so it is not "less than a friend", it is a
    different answer. `SELF` is not here because the two endpoints that
    serve an account holder their own data bypass privacy entirely rather
    than passing a relationship — see `ProfileService.get_own_statistics`.
    """

    STRANGER = "stranger"
    """No relationship. The default, and what every anonymous viewer is."""

    FRIEND = "friend"
    """An active, mutual friendship (domain-model.md FS-1).

    Produced since A64-013.3, which folds an accepted request into a
    `Friendship` and wires the graph into profile composition.
    """

    BLOCKED = "blocked"
    """Either player has blocked the other — A64-013.5.

    **Not "less than a stranger", and that is why this is an enum rather
    than a boolean `is_friend`.** BL-2 makes a block suppress things a
    stranger can still see: presence, direct challenges, direct messages,
    matchmaking pairing. It is a different answer, not a smaller one, and
    `permits` treats it as such by checking the *relationship* before the
    level.

    **Symmetric in effect, asymmetric in origin.** A block is one-directional
    — BL-1: "asymmetric and one-directional; the blocked player is never
    told" — but the *visibility* consequence runs both ways, because a
    blocker who kept seeing the person they blocked would have gained
    nothing. So this value is produced when either party has blocked the
    other, and neither can tell which.

    That indistinguishability is the point. A blocked player sees the same
    thing they would see from somebody with restrictive privacy settings,
    which is what stops "am I blocked" from being answerable — and a visible
    block is an invitation to retaliate from a second account.
    """


class VisibilityLevel(StrEnum):
    """Which audience a profile field is published to.

    A `StrEnum` so the stored value, the wire value and the Python member
    are one string — the same choice `BoardTheme` makes, and for the same
    reason: a mapping table between three spellings of one concept is three
    places for it to drift.

    Ordered widest-first in the source, because that is the order a settings
    screen renders them and the order `database.md`'s `users.audience`
    declares. The order carries no semantics — there is deliberately no
    comparison operator, because "friends is more than nobody" is only true
    for *this* field and would be false for a future `hide_from` setting.
    """

    EVERYONE = "everyone"
    """Any caller, authenticated or not. Byte-for-byte what `true` meant."""

    FRIENDS = "friends"
    """Only players with an active friendship.

    **Unreachable from the API until A64-013.3**, and settable through it
    from this release: the column accepts it, the domain accepts it, and
    `permits` evaluates it correctly. What does not exist yet is anything
    that answers "is this viewer a friend" with `FRIEND`, so a player who
    selects this today is choosing something that currently behaves as
    `NOBODY` — which is the safe direction, and is documented on the wire
    rather than silently rejected.
    """

    NOBODY = "nobody"
    """No other player. Byte-for-byte what `false` meant."""

    def permits(self, relationship: ViewerRelationship) -> bool:
        """Whether a viewer in `relationship` may see the field.

        The single place the audience model is *applied*. Every read path
        that gates a field calls this rather than comparing members, which
        is why A64-013.5 added `BLOCKED` here and nowhere else — the whole
        reason this is a method on the value object rather than an
        `if level is EVERYONE` at each call site.

        **The relationship is checked before the level**, and the ordering
        is the feature rather than an optimisation: a block outranks every
        setting, including `EVERYONE`. A player who set a field public and
        then blocked somebody has not made it public *to them*, and a
        version of this method that checked the level first would publish it
        anyway — silently, to the one person it was withheld from.

        Total by construction: every combination of the two enums has an
        answer, so there is no state in which a caller has to invent one.
        """
        if relationship is ViewerRelationship.BLOCKED:
            return False
        if self is VisibilityLevel.EVERYONE:
            return True
        if self is VisibilityLevel.FRIENDS:
            return relationship is ViewerRelationship.FRIEND
        return False

    @classmethod
    def of(cls, *, visible: bool) -> "VisibilityLevel":
        """The boolean form, widened.

        `true -> EVERYONE`, `false -> NOBODY` — the conversion the migration
        applies to every existing row, and the same one the API applies when
        a client sends the legacy boolean field. Named rather than written
        inline at both sites so the two cannot disagree, which would be a
        setting that means one thing in the database and another on the
        wire.
        """
        return cls.EVERYONE if visible else cls.NOBODY

    @property
    def is_public(self) -> bool:
        """Whether this level is what the legacy boolean called `true`.

        The inverse of `of`, and what the deprecated boolean fields on the
        API are rendered from. `FRIENDS` reads as `false` here, which is
        the honest answer to the question the boolean asks — *may anybody
        see this* — and is the reason the boolean cannot be the writable
        form once `FRIENDS` is reachable.
        """
        return self is VisibilityLevel.EVERYONE
