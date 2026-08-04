"""`Registration` — one player's entry into one tournament. §4, §7.

## Withdrawal is a status, never a delete

A tournament's record is append-oriented: the bracket, the rounds and the
standings are all facts about what happened, and a registration that
vanished would make "who was in this tournament" unanswerable from the
record. So a withdrawal sets `WITHDRAWN` and the row stays.

That also gives capacity an unambiguous meaning: the count that matters is
of `REGISTERED` rows, so a withdrawal genuinely frees a slot while the
history of who took it survives.

## No re-registration after withdrawal

SPEC-TOURNAMENT §4 permits nothing of the sort, and this module does not
invent it. The unique key is `(tournament, player)` rather than
`(tournament, player, attempt)`, so a second entry is refused by the
database whatever the first one's status is — the restriction is
structural rather than a rule somebody could forget.

If it is ever wanted, it is a product decision plus a key change, and the
audit will find it stated here rather than assumed either way.

## No waitlist states

`PENDING`, `PROMOTED`, `RESERVE` are deliberately absent. §4 defers the
waitlist, and a state nothing sets is a state the next reader has to work
out the meaning of.
"""

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.modules.tournament.domain.exceptions import InvalidTournamentTransition


class RegistrationStatus(StrEnum):
    """Whether this entry still occupies a slot."""

    REGISTERED = "registered"
    WITHDRAWN = "withdrawn"


@dataclass(frozen=True, slots=True)
class Registration:
    """One player's entry. Frozen — withdrawal returns a new value."""

    tournament_id: UUID
    player_id: UUID
    registered_at: datetime

    status: RegistrationStatus = RegistrationStatus.REGISTERED
    withdrawn_at: datetime | None = None

    @property
    def occupies_a_slot(self) -> bool:
        """Whether this entry counts against capacity.

        The predicate the capacity check uses, on the entity rather than
        spelled as a status comparison at the call site — so a third status
        added later cannot leave the count and the entity disagreeing about
        what a slot is.
        """
        return self.status is RegistrationStatus.REGISTERED

    def withdrawn(self, at: datetime) -> "Registration":
        """This entry, withdrawn. Raises if it already is.

        A second withdrawal is refused rather than treated as idempotent:
        the two are indistinguishable to the caller, but `withdrawn_at`
        would move, and that instant is part of the tournament's record.
        """
        if not self.occupies_a_slot:
            raise InvalidTournamentTransition("this registration has already been withdrawn")
        return replace(self, status=RegistrationStatus.WITHDRAWN, withdrawn_at=at)


__all__ = ["Registration", "RegistrationStatus"]
