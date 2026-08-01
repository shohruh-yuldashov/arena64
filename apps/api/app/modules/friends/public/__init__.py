"""The **only** package other modules may import from `friends` — BE-03.

Everything else under `app.modules.friends` is private. The rule exists
because Python's import system will happily let `profiles` reach into
`friends.infrastructure.models` and query the table directly, and R-1
(architecture.md §7) forbids exactly that — but forbidding it in prose does
not stop it at the hundredth pull request. One named surface makes the rule
a single import-linter contract.

What is published, and why only this much:

  `SocialGraphReader`  answers "which of these players is this player
                       friends with" (A64-013.3) and "who can this player
                       not interact with" (A64-013.5), and can do nothing
                       else
  `PresenceAudience`   answers "who may be told this player's presence
                       changed" — friends minus blocked, resolved at
                       delivery (A64-013.7). Published so a fan-out can
                       live outside this module while BL-1's subtraction
                       stays inside it
  `PairingExclusions`  answers "among these candidates, which pairs must
                       never be formed" (A64-015.3). BL-2, published for
                       the pairing scan — the only place on the platform
                       that holds two players and has to decide
  the four social      `FriendRequestAccepted`, `FriendRemoved`,
  domain events        `PlayerBlocked`, `PlayerUnblocked` — the facts this
                       context makes durable through the outbox (AD-16).
                       Published because a consumer subscribes by importing
                       the class rather than by retyping its type string,
                       and because the payload's shape is this context's
                       contract in the way a DTO is

Deliberately **not** published: the `Friendship` and `Block` aggregates (a
consumer holding one could end a relationship or lift a block this module is
responsible for), the three repositories (R-1: reach a module through its
services, never its storage), `FriendRequest` and everything about it, and
every service.

## Why the published port is a *reader* and nothing more

Its one consumer is `profiles`, which needs to know whether a viewer is a
friend in order to evaluate `VisibilityLevel.FRIENDS`, and who they are
blocked from in order to enforce BL-2 — and that is all it needs. A port
that also modified either would hand the module serving the platform's
highest-volume public read the ability to rewrite the social graph.

The narrowing is the same one `users.public` makes twelve times over.
"""

from app.modules.friends.domain.events import (
    FriendRemoved,
    FriendRequestAccepted,
    PlayerBlocked,
    PlayerUnblocked,
)
from app.modules.friends.public.ports import (
    PairingExclusions,
    PresenceAudience,
    SocialGraphReader,
)

__all__ = [
    "FriendRemoved",
    "FriendRequestAccepted",
    "PairingExclusions",
    "PlayerBlocked",
    "PlayerUnblocked",
    "PresenceAudience",
    "SocialGraphReader",
]
