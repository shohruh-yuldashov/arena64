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
class UserRegistered(DomainEvent):
    """An account exists — A64-027.2 §11.

    The platform had no such event: registration created a row and told
    nobody. Analytics needs it as the head of every cohort and the last step
    of the acquisition funnel, and it is a **domain fact** rather than an
    analytics concern — "an account was created" is true whether or not
    anything measures it, and a consumer that wanted to send a welcome mail
    or seed a preference would subscribe to the same event.

    Carries the id and nothing else. Not the username, not the email: a
    payload is durable and readable by every consumer, and an address in one
    is personal data in a table with a different retention policy than the
    account it describes (services.md §8.5, and the same reason the
    registration log line carries only an id).
    """

    event_type: ClassVar[str] = "users.registered"
    aggregate_type: ClassVar[str] = "player"

    user_id: UUID

    @property
    def aggregate_id(self) -> UUID:
        return self.user_id

    def payload(self) -> dict[str, Any]:
        return {"user_id": str(self.user_id)}


@dataclass(frozen=True)
class EmailVerified(DomainEvent):
    """An account became usable — A64-027.2 §11.

    The transition AC-2 turns on: an unverified account cannot play rated
    matches, so this is the moment a registration becomes a player.

    `hours_since_registration` is on the payload rather than left to a join
    because a consumer reading this event should not have to read the
    account back — the rule every payload on this platform follows (AD-16),
    and here it is also what lets analytics answer "how long does
    verification take" without joining two event streams.
    """

    event_type: ClassVar[str] = "users.email_verified"
    aggregate_type: ClassVar[str] = "player"

    user_id: UUID
    hours_since_registration: int

    @property
    def aggregate_id(self) -> UUID:
        return self.user_id

    def payload(self) -> dict[str, Any]:
        return {
            "user_id": str(self.user_id),
            "hours_since_registration": self.hours_since_registration,
        }


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
