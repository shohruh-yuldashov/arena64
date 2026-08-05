"""`PublicProfile` — the composed view, assembled from three contexts.

Framework-free (architecture.md §8). Holds no logic beyond being the one
place that says what a public profile *consists of*: identity from `users`,
ratings from a rating system, counts from a statistics system.

## Why the composed view is a domain type and not just a response schema

The response schema in `presentation/schemas/` is the wire format and will
change when the API version does. This is the thing being rendered, and it
is what `ProfileService` returns — so a second consumer (a server-rendered
page under AD-24, a gateway pushing a profile card over a socket) composes
the same object rather than reaching for the three sources itself and
assembling a subtly different one.

It also means the composition is testable without HTTP.
"""

from dataclasses import dataclass
from datetime import datetime

from app.modules.profiles.domain.ratings import PlayerRatings
from app.modules.statistics.public import PlayerStatistics
from app.modules.users.public import PublicUserProfile, RelationshipState


@dataclass(frozen=True, slots=True)
class PublicProfile:
    """One player, as everyone else sees them.

    `identity` is `users`' published DTO carried whole rather than
    unpacked into fields here. Two reasons, and the second is the one that
    matters: unpacking would mean this module re-declaring `username`,
    `display_name`, `avatar`, `country`, `bio` and `created_at`, so
    every field `users` adds would need adding twice — and, worse, a field
    `users` *removes* would keep working here until something noticed. The
    published DTO is the contract; holding it whole is what makes it one.

    The first reason is narrower and still worth stating: `PublicUserProfile`
    has no `email` field, so nothing this module can do — including a
    careless `model_dump()` — can publish one.
    """

    identity: PublicUserProfile
    ratings: PlayerRatings
    """Always present, for every player, whatever their privacy settings.

    Not an oversight and not a gap in A64-012.4: `show_statistics` covers
    the *record* — games, wins, losses, draws — and a rating is a different
    thing. It is what pairing is computed from, what every leaderboard
    already publishes, and what UP-5 keeps visible to the opponents whose
    results produced it. A player who could hide their rating while still
    accepting rated games would be sandbagging with the platform's help.
    See `users.domain.privacy.PrivacySettings.show_statistics`.
    """

    statistics: PlayerStatistics | None
    """The aggregate match record, or `None` when this player has hidden it.

    **`None` rather than zeroes.** A64-012.4 requires a hidden field to be
    null and forbids a placeholder, and zeroes would be the worst possible
    placeholder here — indistinguishable from a genuine beginner, which
    turns a privacy setting into a lie about a player's experience and
    misleads exactly the opponent deciding whether to accept a challenge.

    Nullable rather than absent so the field stays in the contract for
    every player. A client renders "not shown" from a `null`; it cannot
    render anything sensible from a key that sometimes exists.
    """

    relationship: RelationshipState | None = None
    """What the **authenticated viewer** may do about this player —
    A64-020.4.

    `None` in exactly two cases, and neither is `NONE`:

        anonymous viewer    there is no viewer to have a relationship with,
                            and `NONE` would claim a signed-in stranger
        own profile         nobody is their own friend; a client must render
                            no social actions rather than "add friend"

    `NONE` therefore means something specific — *signed in, and no
    relationship* — which is what a client renders "Add friend" from. A
    single value covering both absence and emptiness would put the decision
    of which is which into every consumer.

    **One-directional.** `BLOCKED` means this viewer blocked this player.
    A block placed *on* the viewer is never expressible here — see
    `RelationshipState`.

    Not gated by any privacy setting, and it is not a leak: it is a fact
    about the *viewer's own* actions, which they already know. It says
    nothing about the player being read that the player could hide.
    """

    last_seen: datetime | None = None
    """When this player was last observed online, or `None` when the
    platform has nothing to report.

    Read from `users.public.PresenceProvider` since A64-012.7. Before that
    it was declared and permanently `None`, because presence is `users`-owned
    and lives in Redis with a TTL — "online" is true only while a socket is
    open (domain-model.md §141, §299) — and there was no presence store to
    read from.

    It is still never faked from anything else stored, and the near misses
    are worth keeping on the record because each is wrong in a different way.
    `users.updated_at` is when a row was written, which for most accounts is
    registration day. A session's `last_used_at` is when a refresh token was
    exchanged, which happens on a timer rather than when a person is present,
    and would publish the activity of a background tab.

    ## Every reason for `None` is the same `None`

    A64-012.4 added the control before the data, and `show_last_seen` is the
    one privacy flag that defaults to *off*. So the common case for this
    field is a player who has opted out — and the response must not let a
    caller tell that apart from a player nobody has observed, a presence
    window that has expired, or a presence store that was unreachable
    (A64-012.7). All four are this one value, and `ProfileService` is where
    they converge.
    """

    is_online: bool | None = None
    """Whether the player is here right now, or `None` when unknown.

    Three states rather than two, and the third is the one that carries the
    privacy requirement:

        True    a socket was open when presence was last written
        False   the platform saw this player *leave*, recently enough that
                the record has not yet expired — so `last_seen` beside it,
                if visible, is meaningful
        None    nothing is known, or nothing may be said

    `None` covers a player who has hidden their presence, a player nobody
    has ever observed, a presence window that has lapsed, and a presence
    store that could not be reached. A64-012.7 requires exactly that
    conflation: reporting *that* presence is hidden answers the question
    hiding it exists to decline, and it is the same argument
    `PublicUserProfile.country` records for a hidden country.

    **Governed by `show_online_status`, not by `show_last_seen`.** The two
    are separate flags with separate defaults — "online now" is coarse and
    momentary, while a published `last_seen` is a sleep schedule — so a
    player may show one and hide the other, and `ProfileService` gates them
    independently from one read.
    """
