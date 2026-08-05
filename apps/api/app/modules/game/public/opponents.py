"""Who these players have just played — A64-015.4 §11.

QT-3's rematch guard, published as a read. `matchmaking` declared
`RecentOpponentProvider` in A64-015.3 and shipped `NoRecentOpponents`
against it, with the reason recorded: `game` had a `Match` aggregate and no
table, so there was no history to read. There is one now, and this is the
port that serves it.

## Why the port is declared twice, and why that is not duplication

`matchmaking.application.ports.RecentOpponentProvider` states what the
*pairing scan* needs; this states what `game` is prepared to answer. They
have the same shape — deliberately, so the implementation below satisfies
the consumer's port structurally and the composition root wires one object
with no adapter in between — and they are owned by different modules for
the reason AD-06 gives: a port belongs to the layer that needs it, and a
published surface belongs to the module that publishes it. Collapsing them
would mean `matchmaking`'s scan depending on `game` even in the tests that
have no `game` at all.

## What "recent" means

**The most recent match a player actually sat down to** — `active` or
`completed`. An offer that expired unanswered, one somebody declined, and
one still awaiting an answer are all excluded, because in none of them did
the two players play.

This was wider until A64-020.5A, and the reasoning it replaced is worth
keeping because it was wrong in an instructive way. The old text argued that
counting declined and expired offers "excludes *more* pairs than QT-3
requires ... which costs an occasional slightly-wider search and never
produces the failure QT-3 exists to prevent."

The second half held; the first did not. The exclusion is not a *wider
search* — it is **permanent**, because this reader returns each player's
single most recent opponent with no time window. Two players whose one offer
lapsed became each other's most recent opponent forever, so the guard vetoed
every future pairing between them and neither could ever meet the other
again. On a thin pool that is two accounts that can never play, and it was
found by an end-to-end flow that paired the same two players twice.

Narrowing also makes the port say what QT-3 says. "Who did these players
just play" has one honest answer, and a match nobody entered is not part of
it.
"""

from collections.abc import Mapping, Sequence
from typing import Protocol
from uuid import UUID


class RecentOpponentReader(Protocol):
    """`game`'s answer to "who did these players just play".

    A read rather than a command, and the only one this package publishes:
    it hands out player identifiers the caller already has, and nothing
    about the matches they came from.
    """

    async def recent_opponents_among(
        self, player_ids: Sequence[UUID]
    ) -> Mapping[UUID, frozenset[UUID]]:
        """For each of `player_ids`, which **others in the same batch**
        they most recently played.

        Batch and symmetric, for the reasons
        `friends.public.PairingExclusions` is both: a per-candidate form
        would be an N+1 inside a scan that runs several times a second, and
        "they played me" and "I played them" are the same game.

        Restricted to the batch on purpose. The caller is a pairing scan
        deciding which of *these* tickets may meet, so an opponent who is
        not in the pool is not an exclusion — it is a row nobody will ever
        compare against.

        **Never raises.** An unreadable history must degrade to "no
        exclusions" rather than stop pairing: a rematch is a
        disappointment, and an empty pool is an outage.
        """
        ...


__all__ = ["RecentOpponentReader"]
