"""`GamePairingSettlements` — did these reserved tickets produce a match?

Implements `game.public.PairingReconciliationReader`, whose docstring
records why the question exists and why it is keyed on the ticket rather
than on the pairing. Nothing here re-argues it.

Four lines of body over a repository, and that is the whole point of the
port: the *fact* is a single indexed read, and what makes it worth
publishing is that `matchmaking` cannot perform it without importing a
`game` table.
"""

from collections.abc import Mapping, Sequence
from uuid import UUID

from app.modules.game.application.ports import MatchRecordRepository
from app.modules.game.domain.match_record import MatchRecord
from app.modules.game.public.reconciliation import PairingSettlement


class GamePairingSettlements:
    """The reconciler's read, over one session."""

    def __init__(self, matches: MatchRecordRepository) -> None:
        self._matches = matches

    async def settlements_for(self, ticket_ids: Sequence[UUID]) -> Mapping[UUID, PairingSettlement]:
        """The match each of `ticket_ids` produced, for those that produced
        one.

        A ticket with no entry produced no match — the ordinary answer for
        a reservation whose worker died before it called `game`, and the
        case whose action is "put this player back in the queue".

        **Unlike the reads beside it, this one propagates.** A recent
        opponent that cannot be read degrades to no exclusions, because the
        cost is a rematch. A settlement that cannot be read has no safe
        default at all: guessing "no match" releases a ticket whose player
        already has a game, and guessing "matched" strands one who does
        not. The reconciler's correct response is to fail the tick and try
        again, and it can only do that if this raises.
        """
        if not ticket_ids:
            return {}

        records = await self._matches.settlements_for(ticket_ids)
        wanted = set(ticket_ids)
        return {
            ticket_id: _settlement(record)
            for record in records
            # `queue_ticket_ids`, not `ticket_ids`: a match with no tickets
            # — a tournament's, a challenge's — contributes no keys rather
            # than a `None` one. A reconciler asks about tickets, so a match
            # that has none is simply not an answer to its question.
            for ticket_id in record.queue_ticket_ids()
            if ticket_id in wanted
        }


def _settlement(record: MatchRecord) -> PairingSettlement:
    return PairingSettlement(
        match_id=record.id, pairing_id=record.pairing_id, created_at=record.created_at
    )


__all__ = ["GamePairingSettlements"]
