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
from app.modules.profiles.domain.statistics import PlayerStatistics
from app.modules.users.public import PublicUserProfile


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

    last_seen: datetime | None = None
    """When this player was last observed online. **Always `None` today**,
    and `None` for anyone who has hidden it once it is not.

    A64-012.1 lists `last_seen` in the response and excludes "online
    status" from the scope, which is not a contradiction so much as a line:
    the field is part of the contract, and the presence tracking that would
    fill it is not this task's.

    A64-012.4 added the control before the data: `show_last_seen` is the
    one privacy flag that defaults to *off*, and `ProfileService` already
    refuses to fill this field for a player who has it off. So the release
    that ships presence tracking cannot be the release that publishes a
    sleep schedule — the enforcement is in place and untested only because
    there is nothing yet to enforce it against.

    It cannot be faked from anything already stored, and the near misses
    are worth naming because each is wrong in a different way.
    `users.updated_at` is when a row was written, which for most accounts
    is registration day. A session's `last_used_at` is when a refresh token
    was exchanged, which happens on a timer rather than when a person is
    present, and would publish the activity of a background tab.

    Presence is `users`-owned and lives in Redis with a TTL, because
    "online" is true only while a socket is open (domain-model.md §141,
    §299) — so this is filled in by whatever opens those sockets, and
    stays `None` until then. Declared now rather than added later so the
    field is in the contract from the first release and clients render it
    as "unknown" rather than gaining a key they did not expect.
    """
