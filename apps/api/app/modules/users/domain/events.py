"""The two presence events — A64-013.7.

Owned by `users`, which owns `Presence` (domain-model.md §299). Emitted on
**edges only**: a player who refreshes a token every thirty seconds is
online once, not once per refresh, and a consumer that had to de-duplicate
that would be doing the producer's job with less information.

## `occurred_at` is the transition, not the observation

The instant is when the player's state changed as far as the platform can
tell — the moment of the sign-in, of the sign-out. For an offline edge that
is the sign-out; for the *lapse* edge (a presence window expiring with
nobody watching) there is no event at all, because nothing observes it. That
gap is real and is recorded in A64-013.7's recommendations rather than
papered over here: an event claiming a player went offline at a moment
nobody witnessed would be a fabrication.

## Why these carry no audience

The recipients of a presence notification are the subject's friends minus
whoever is blocked, resolved at **delivery**. Putting them in the payload
would freeze a social graph that changes between enqueue and delivery, and
the whole reason A64-013.7 forbids trusting enqueue-time state is that the
block placed in between is exactly the one that matters.
"""

from dataclasses import dataclass
from typing import Any, ClassVar
from uuid import UUID

from app.platform.events import DomainEvent


@dataclass(frozen=True)
class PresenceOnline(DomainEvent):
    """A player became present — the `offline -> online` edge.

    Produced by `PresenceNotificationService`, which compares the record it
    is about to write against the one already there. A sign-in by a player
    who was already online produces nothing.
    """

    event_type: ClassVar[str] = "users.presence_online"
    aggregate_type: ClassVar[str] = "player"

    player_id: UUID

    @property
    def aggregate_id(self) -> UUID:
        return self.player_id

    def payload(self) -> dict[str, Any]:
        # No `session_id` and no `device_type`, though both are recorded in
        # Redis. Neither reaches any response schema (A64-012.7: never
        # expose internal session identifiers), and an event payload is one
        # `SELECT` away from an operator's terminal — so the field that must
        # never be published does not go in the durable log either.
        return {"player_id": str(self.player_id)}


@dataclass(frozen=True)
class PresenceOffline(DomainEvent):
    """A player stopped being present — the `online -> offline` edge.

    Emitted when every session is revoked, which is the only *observed*
    departure the platform has. See this module's docstring on the lapse
    edge that is deliberately not an event.
    """

    event_type: ClassVar[str] = "users.presence_offline"
    aggregate_type: ClassVar[str] = "player"

    player_id: UUID

    @property
    def aggregate_id(self) -> UUID:
        return self.player_id

    def payload(self) -> dict[str, Any]:
        return {"player_id": str(self.player_id)}
