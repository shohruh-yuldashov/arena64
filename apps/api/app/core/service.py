"""The service-layer contracts — services.md §3.

Arena64 does not have one service shape; it has three, because its write
paths have genuinely different transaction models (services.md §3.1). These
three marker types let a module declare *which* kind a given service is,
without this layer inventing a method name services.md never specified —
"one public method, one intent" (services.md §3) constrains that a service
has exactly one public entry point, not what it is called.
"""

from typing import Protocol


class CommandService(Protocol):
    """One PostgreSQL transaction, owned by the service itself, returning a
    domain result or a typed error.

    Example from services.md §3.1: `friends.AcceptFriendRequest`.
    """


class QueryService(Protocol):
    """No transaction, or a read-only session. Returns a read DTO.

    Example from services.md §3.1: `leaderboard.GetTopPlayers`.
    """


class RealtimeCommandService(Protocol):
    """A Redis compare-and-set, plus a write-behind durable append — never a
    PostgreSQL transaction wrapping the whole operation (services.md §3.1,
    §9.2). Named separately so a reviewer sees immediately which contract a
    service operates under, rather than assuming the default `CommandService`
    shape and putting PostgreSQL latency inside a hot-path budget.

    Example from services.md §3.1: `game.SubmitMove`.
    """
