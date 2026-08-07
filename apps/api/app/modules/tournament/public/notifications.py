"""What a notification consumer may ask a tournament — A64-021.4 §6.

`notifications` learned in A64-021.4 that tournaments produce facts worth
telling people about. Three of them — a registration, a published round, a
completed tournament — name a tournament that the consumer then has to turn
into *who to tell* and *what to call it*.

## Why this is a port here rather than a repository there

`tournament-internals-are-private` forbids `notifications` from importing
this module's repositories, and it should: `SeedRepository` can assign
seeds, `StandingRepository` can record standings, and a consumer that held
either could change a bracket while composing a sentence about one.

So the published surface is **two reads and nothing else**. A compromised or
merely careless consumer holding this can learn who is in a tournament and
what they placed; it cannot enter anybody, withdraw anybody, pair anybody or
finish anything.

## Batch, for the reason every fan-out port on this platform is

A64-019's capacity is 128 entrants (`specs/tournament.md` §2). A per-player
read at that size is 128 round trips on a relay tick, which is the N+1
CLAUDE.md §10.4 names — and it is the one that only appears in production,
because a two-player test tournament makes it invisible.

`participants_of` answers one tournament's whole field in one statement, and
`results_of` answers one tournament's whole standings in one. Neither takes
a player, so neither can be called in a loop by accident.

## What is deliberately not here

The bracket, the pairings, the schedule, the seeds. A notification says
*that a round was paired*, and the tournament page says what the pairings
are — putting a bracket behind this port would make a consumer that renders
one line able to read a tournament's entire state.

No write of any kind. `TournamentAttendance` is this module's one inbound
command and it is deliberately in its own file, so a reader and a writer
never arrive through one import.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class TournamentAudience:
    """One tournament's name and the players a notification about it reaches.

    The name travels with the audience because both come from the same
    read's neighbourhood and a consumer needs both for every notification —
    two calls would be two round trips for one sentence.

    A **snapshot** of the name, like every other name this platform stores
    on a notification: the row records what the tournament was called when
    the thing happened.
    """

    tournament_id: UUID
    name: str
    participant_ids: frozenset[UUID]
    """Every player with a **live** registration. Withdrawn entries are
    excluded by the read, which is what makes "do not notify somebody who
    left" a property of the query rather than a filter a consumer must
    remember to apply."""


@dataclass(frozen=True, slots=True)
class TournamentResults:
    """One finished tournament's final placements.

    `final_rank_by_player` is the whole point: a completion notification
    that said only "the tournament ended" would repeat what the tournament
    page already shows, and "you finished 5th" is the fact the recipient
    does not otherwise have.

    Ranks are **as recorded** — ties share a rank and the sequence may have
    gaps. A consumer must not renumber them; `specs/tournament.md` §6f
    defines what they mean and a client that densified them would be
    reporting a placement nobody was awarded.
    """

    tournament_id: UUID
    name: str
    winner_id: UUID | None
    final_rank_by_player: Mapping[UUID, int]

    @property
    def participant_ids(self) -> frozenset[UUID]:
        """Everybody with a durable standing — §17's recipient rule.

        A standing, not a registration: a player who withdrew before the
        field was fixed has no result to be told about, and telling them
        they placed nowhere in a tournament they left is worse than silence.
        """
        return frozenset(self.final_rank_by_player)


class TournamentNotificationReader(Protocol):
    """`tournament`'s published read surface for notification fan-out."""

    async def audience_of(self, tournament_id: UUID) -> TournamentAudience | None:
        """This tournament's name and live entrants, in one read.

        `None` when no such tournament exists. A consumer reacting to an
        event whose tournament has since been deleted must skip it rather
        than fail forever — the event is a fact about a thing that is gone,
        and no retry will bring it back.
        """
        ...

    async def results_of(self, tournament_id: UUID) -> TournamentResults | None:
        """This tournament's final placements, in one read.

        `None` when the tournament does not exist **or** has no standings
        yet. The second case is a genuine transient: a consumer that ran
        before the completion transaction was visible should retry, and the
        caller decides that rather than this port guessing.
        """
        ...

    async def names_of(self, tournament_ids: Sequence[UUID]) -> Mapping[UUID, str]:
        """Several tournaments' names, in one read.

        For the batch a relay tick carries: one tick can hold registrations
        for a dozen tournaments, and a per-event name lookup would put a
        query between every notification and the next.
        """
        ...


__all__ = [
    "TournamentAudience",
    "TournamentNotificationReader",
    "TournamentResults",
]
