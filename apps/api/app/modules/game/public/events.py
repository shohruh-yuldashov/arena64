"""`game`'s durable events, published — A64-015.5.

Re-exports rather than redefinitions: the classes live in
`game.domain.events`, and a consumer imports them from here. That is the
shape `friends/public/` already uses for `PlayerBlocked` and
`FriendRequestAccepted`, and the reason it is a re-export rather than a
parallel set of DTOs is R-1's whole point — a published surface is a
*surface*, not a second copy of the thing behind it.

## Publishing events is what R-3 asks for, not an exception to it

R-3 says the modules that care about matches "**never call into `game` to
change anything**; they subscribe to its events". A subscriber that cannot
name the event it subscribes to has to match on a string literal, which is
the one spelling mistake nothing catches — `handles()` simply returns
`False` forever and the consumer is silently dead.

So the events are contract. The **aggregate** still is not: `MatchRecord`,
`MatchSeat` and the two status enums that describe transitions stay private,
and nothing on these payloads can advance a match.

## Who subscribes today

    matchmaking  `match_created`         -> realtime pending-match delivery
                 `match_declined`        -> the acceptance-failure policy
                 `match_acceptance_expired` -> the same policy, other half

`match_accepted_by_player` and `match_activated` have no subscriber yet and
are published anyway — the relay marks an unwanted entry published and
counts it separately, so an unsubscribed event costs one row. `rating` and
`statistics` are the consumers `match_activated` is waiting for.
"""

from app.modules.game.domain.events import (
    MATCH_AGGREGATE,
    MatchAcceptanceExpired,
    MatchAcceptedByPlayer,
    MatchActivated,
    MatchCreated,
    MatchDeclined,
)

__all__ = [
    "MATCH_AGGREGATE",
    "MatchAcceptanceExpired",
    "MatchAcceptedByPlayer",
    "MatchActivated",
    "MatchCreated",
    "MatchDeclined",
]
