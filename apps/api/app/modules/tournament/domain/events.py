"""`tournament`'s domain events — SPEC-TOURNAMENT §7.

Declared here and **published by nobody yet**: A64-019.1 is the domain, and
wiring an outbox consumer before there is a use case to emit from would be
an entry point nothing reaches — the defect two audits already found twice.

Each carries what a consumer needs to act without reading anything back
(`services.md` §10.2), and nothing more. No bracket, no participant list, no
match: a `notifications` consumer wants to know a round is ready, not what
is in it.
"""

from dataclasses import dataclass
from typing import Any, ClassVar
from uuid import UUID

from app.platform.events import DomainEvent

TOURNAMENT_AGGREGATE = "tournament"


@dataclass(frozen=True)
class _TournamentEvent(DomainEvent):
    """The identity every event here shares.

    A base rather than six repetitions of two fields, and it carries no
    behaviour: `event_type` is each subclass's, because a shared one would
    make the type a runtime value and `DomainEvent`'s whole contract is that
    it is not.
    """

    aggregate_type: ClassVar[str] = TOURNAMENT_AGGREGATE

    tournament_id: UUID

    @property
    def aggregate_id(self) -> UUID:
        return self.tournament_id

    def payload(self) -> dict[str, Any]:
        return {"tournament_id": str(self.tournament_id)}


@dataclass(frozen=True)
class TournamentCreated(_TournamentEvent):
    event_type: ClassVar[str] = "tournament.created"

    name: str
    format: str
    capacity: int

    def payload(self) -> dict[str, Any]:
        return {
            **super().payload(),
            "name": self.name,
            "format": self.format,
            "capacity": self.capacity,
        }


@dataclass(frozen=True)
class RegistrationOpened(_TournamentEvent):
    event_type: ClassVar[str] = "tournament.registration_opened"


@dataclass(frozen=True)
class RegistrationClosed(_TournamentEvent):
    """The field is fixed. The bracket is built from exactly these players."""

    event_type: ClassVar[str] = "tournament.registration_closed"

    entrant_count: int

    def payload(self) -> dict[str, Any]:
        return {**super().payload(), "entrant_count": self.entrant_count}


@dataclass(frozen=True)
class TournamentStarted(_TournamentEvent):
    event_type: ClassVar[str] = "tournament.started"


@dataclass(frozen=True)
class RoundPublished(_TournamentEvent):
    """Pairings are readable and immutable from here — §6."""

    event_type: ClassVar[str] = "tournament.round_published"

    round_number: int

    def payload(self) -> dict[str, Any]:
        return {**super().payload(), "round_number": self.round_number}


@dataclass(frozen=True)
class RoundCompleted(_TournamentEvent):
    event_type: ClassVar[str] = "tournament.round_completed"

    round_number: int

    def payload(self) -> dict[str, Any]:
        return {**super().payload(), "round_number": self.round_number}


@dataclass(frozen=True)
class TournamentCompleted(_TournamentEvent):
    event_type: ClassVar[str] = "tournament.completed"

    winner_id: UUID

    def payload(self) -> dict[str, Any]:
        return {**super().payload(), "winner_id": str(self.winner_id)}


@dataclass(frozen=True)
class TournamentCancelled(_TournamentEvent):
    event_type: ClassVar[str] = "tournament.cancelled"


__all__ = [
    "TOURNAMENT_AGGREGATE",
    "RegistrationClosed",
    "RegistrationOpened",
    "RoundCompleted",
    "RoundPublished",
    "TournamentCancelled",
    "TournamentCompleted",
    "TournamentCreated",
    "TournamentStarted",
]
