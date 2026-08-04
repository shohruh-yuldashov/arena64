"""Telling a tournament that somebody turned up — SPEC-TOURNAMENT §6e.

A64-019.5H makes tournament matches **system-activated**: nobody is asked to
accept a fixture they entered a tournament to play, so the match is created
already `ACTIVE` and there is no acceptance handshake to miss. That removes
the only signal the platform had for "this player is engaged", and replaces
the question with a better one — *did they turn up?*

The gateway is the only component that knows. It is where a socket proves an
identity and joins a match's room, and joining that room is exactly what
"turning up" means for a live game.

## Why the gateway may hold this, and why it is one method

R-7 keeps the gateway free of domain logic and every port it holds is the
narrowest thing that answers one question. This one **records a fact and
returns nothing it can act on**: a transport tier that was compromised could
mark attendance and could not advance a bracket, read a tournament, or learn
that one exists.

It is deliberately not an event. An outbox round trip would put the relay's
interval between a player arriving and the tournament knowing, and the
no-show deadline is measured in minutes — a policy that could eliminate
somebody because their attendance was still queued is worse than a write on
the join path.

## What it costs on the join path

One guarded `UPDATE`, no read, for **every** room join including the ones no
tournament owns — a match this module does not know matches no row and the
statement changes nothing. That is the price of not putting a
`pairing_attempt` lookup in front of every join, and it is why the port takes
a `match_id` rather than anything that would have to be resolved first.
"""

from datetime import datetime
from typing import Protocol
from uuid import UUID


class TournamentAttendance(Protocol):
    """`tournament`'s one inbound command from the transport tier."""

    async def mark_present(self, match_id: UUID, player_id: UUID, *, at: datetime) -> bool:
        """Records that `player_id` reached `match_id`. Returns whether this
        call was the one that did.

        **Idempotent**, and the first arrival is the one kept: §6e's rule is
        that a transient disconnect after somebody has turned up is not a
        no-show, so a reconnect must not be able to move the instant and a
        dropped socket must not be able to clear it.

        `False` for a match no tournament owns, for a player who is not in
        it, and for a reconnect — three different things that all mean
        "nothing changed", and none of which a caller acts on differently.
        A gateway does not branch on this; it exists so the write is
        observable in a test and countable in a metric.

        **Never raises for an unknown match.** The gateway calls this on
        every join, and a refusal would make joining an ordinary queue game
        depend on a module that has nothing to do with it.
        """
        ...


__all__ = ["TournamentAttendance"]
