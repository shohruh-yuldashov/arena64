"""Who is in a match — `game`'s answer to the gateway's only question.
A64-016.2 §7.

The gateway has to decide whether a socket may join a match's routing scope,
and §7 states the two halves of that: "Only actual Match participants may
join the room. Resolve participants through `game.public`." Nothing published
before this task could answer it. `PendingMatchView` is scoped to the
*acceptance handshake* and to one reader's seat; `PairingSettlement` answers
"did these tickets produce a match". Neither says "is this player in match X",
and a room is opened for a match that has already been accepted.

## Why a new port rather than a method on `MatchAcceptanceUseCase`

The split every port pair on this platform makes: what differs is the
**capability**. A route holding `MatchAcceptanceUseCase` can accept and
decline on a player's behalf; the gateway must not be able to do either. It
needs one read and gets exactly one read — so a transport tier that was
compromised could enumerate nothing and change nothing.

## Why the roster is not seat-relative

`PendingMatchView` is deliberately asymmetric (`you_accepted`,
`opponent_accepted`) because a route renders it for one person. This is the
opposite case: the gateway is not a participant, it routes *between* them,
and a view named from one seat would make "which of these two is the caller"
a question the router has to answer before it can use the answer.

So the roster names both sides by side and offers `includes` — the only
question the gateway actually asks.
"""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.modules.game.domain.match_record import MatchRecordStatus


@dataclass(frozen=True, slots=True)
class MatchRoster:
    """The two players in one match, and where the match stands.

    Primitive-only: two identifiers and an enum. Nothing here is a `game`
    domain object, so the gateway holds the answer without holding a
    `MatchRecord` — which is what keeps R-7's "the gateway contains no
    domain logic" true of the *data* as well as the code.
    """

    match_id: UUID
    light_player_id: UUID
    dark_player_id: UUID
    """DM-06's opaque identifiers, named by side rather than by seat — see
    this module's docstring on why a router must not receive a view built
    from one player's perspective."""

    status: MatchRecordStatus
    """Where the match is in its life.

    Published rather than reduced to a boolean, because the gateway's rule
    and `game`'s state machine must not drift: today a room opens for an
    `active` match, and if a later task opens one during
    `pending_acceptance` so both players can watch the handshake, that is a
    decision made at the gateway against this field rather than a change to
    what `game` is willing to say.
    """

    def includes(self, player_id: UUID) -> bool:
        """Whether this player is one of the two.

        The whole of what a membership check needs, and it is a method
        rather than a caller-side `in` so that a match which later grows a
        third seat — a spectator-of-record, an arbiter — changes here
        instead of at every call site that assumed two.
        """
        return player_id in (self.light_player_id, self.dark_player_id)


class MatchRosterReader(Protocol):
    """`game`'s answer to "who is in this match".

    One method. A port that could also list a player's matches would be a
    port the gateway could enumerate history with, and it has no reason to.
    """

    async def roster_of(self, match_id: UUID) -> MatchRoster | None:
        """The two players in `match_id`, or `None` if there is no such
        match.

        **`None` rather than raising**, and rather than a distinguishable
        "not found" versus "not yours": the caller is deciding whether to
        admit a socket, and a reader that reported the difference between
        an unknown match and one the caller is not in would make live match
        identifiers enumerable by response — which is exactly the reasoning
        `MatchAcceptanceUseCase.accept` gives for collapsing both into
        `MatchNotFound`.

        Does **not** lock. Two players joining a room at the same instant
        are not competing for anything — unlike `accept`, where the second
        must wait and see what the first wrote — so a `FOR UPDATE` here
        would serialise every join on one match for no invariant.
        """
        ...


__all__ = ["MatchRoster", "MatchRosterReader"]
