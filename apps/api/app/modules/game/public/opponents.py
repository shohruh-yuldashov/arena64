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

## What "recent" means today, and what it will mean

**The most recent match a player was in that is no longer awaiting
acceptance** — active, cancelled or expired. Not "the previous *completed*
opponent", which is what QT-3 actually asks for, because no match on this
platform can complete yet: gameplay, results and termination are later
tasks, and a match reaches `ACTIVE` and stays there.

That is a real limitation and is recorded rather than hidden. Its effect is
in the safe direction: it excludes *more* pairs than QT-3 requires — a
pairing that was declined counts as "recently played" — which costs an
occasional slightly-wider search and never produces the failure QT-3
exists to prevent. When `MatchRecord` gains a result, the predicate
narrows to completed matches and nothing above this port changes.
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
