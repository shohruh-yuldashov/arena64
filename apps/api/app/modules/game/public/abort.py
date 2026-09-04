"""Closing a match that will never be played — A64-025.13A §36.

The published counterpart to `MatchCreationUseCase`. A context that can ask
`game` to *start* a match had no way to ask it to *stop* one, and the
consequence was not theoretical.

## The defect this closes

A tournament fixture is system-activated: nobody accepts it, so `game`'s
acceptance expiry never claims it, and it carries no time control, so the
clock adjudicator has no deadline to flag. `TournamentNoShowService` is what
was meant to end one — its composition root says so in as many words:

    Tournament matches are system-activated, so `game`'s acceptance expiry
    never claims one and nothing else would ever end a fixture nobody turned
    up for. This is what does.

It did not. It recorded the *attempt's* outcome and advanced the bracket, and
left the **match** `active` forever, because there was no port through which
it could do anything else. `GET /matchmaking/matches/pending` reports an
active match and the lobby sends that player to it rather than to the queue
form — so **a player adjudicated a no-show could never queue again.** Four
such matches were found in one development database (A64-025.13 §35.7).

## Abort, not a result — and this is the whole design

A64-019.5H wrote, in the test that pinned this behaviour, *"the `game` match
is untouched: nothing invented a result."* That instinct is right and it is
why the fix is not a win for the player who turned up.

These fixtures are **rated**. Recording `WIN` would move two Glicko-2 ratings
for a game nobody played and put it in both players' history. So the match
ends as `MatchOutcome.NONE` with `TerminationReason.ABORT`, which the
taxonomy defines for exactly this: *"an abort is a match that did not
happen"*, and MT-11 keeps it out of every rating and statistic.

The walkover is the **tournament's** record — an advanced bracket node with
`AdvancementReason.ADJUDICATION` — and it stays there, where a competitive
verdict that nobody played to belongs. `game` records only that the fixture
is over.

## Why a separate port from `commands`

`GameCommandUseCase` is a *participant's* channel: resign, offer, accept,
decline. Every one of them is authorised as "you are in this match". This is
not that. Nobody is asking; a sweep has concluded that a fixture will never
be played. Folding a system verdict into the participant enum would put it
one missing check away from a player-issued one.

## What it deliberately cannot do

It cannot reopen a match, change a recorded result, or abort one that has
ended. `AbortOutcome.ALREADY_SETTLED` is the honest answer for a game that
finished while the sweep held its claim — the real result wins, which is the
same rule `TournamentNoShowService` applies to a superseded attempt. MT-10's
permanence is not negotiable from here, and neither is auditing: a
moderator's adjudication of a *played* game is a different operation with
different authority, and `game.public.administration` records why it is not
reachable yet.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class AbortMatchRequest:
    """One system verdict that a match will not be played.

    A match id and nothing else. There is no `reason` parameter because
    there is one reason — `ABORT` — and a caller that could choose would be
    a caller that could record `RESIGNATION` for a game nobody resigned.
    """

    match_id: UUID


class AbortOutcome(StrEnum):
    """What happened, as a closed set the caller can branch on.

    Three values and no exception for the ordinary ones: a sweep asks about
    matches it may be wrong about, and "already finished" is a normal answer
    rather than a failure.
    """

    ABORTED = "aborted"
    """The match is now closed with no result, and `match.completed` was
    published so every consumer settles it."""

    ALREADY_SETTLED = "already_settled"
    """It had ended before this arrived. The recorded result stands."""

    NOT_FOUND = "not_found"
    """No such match. A caller holding a dangling id is a defect, and this
    reports it rather than raising past a sweep that must not stop."""


class MatchAbortUseCase(Protocol):
    """Closes an unplayable match. `game`'s published surface."""

    async def abort(self, request: AbortMatchRequest) -> AbortOutcome:
        """Ends `match_id` with no result and no rating effect.

        **Idempotent**, and it has to be: the no-show sweep re-claims an
        attempt whose worker died, so this is called again for a match it
        already closed. The second call reports `ALREADY_SETTLED` and writes
        nothing.
        """
        ...


__all__ = ["AbortMatchRequest", "AbortOutcome", "MatchAbortUseCase"]
