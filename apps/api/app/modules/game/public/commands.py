"""Resigning and agreeing a draw — `game`'s other live-play boundary.
A64-020.5C-pre §5, §14.

The same boundary `moves.py` draws, for the same reason. R-7: "the gateway
validates, authenticates, rate-limits, routes, and fans out. It never
decides whether a move is legal" — and it decides no more about a
resignation. So this is the whole of what a transport may ask: *this player
wants to do this thing to this match*. Whether they may, what it means, and
what it settles are `game`'s.

## Why one use case with four commands rather than four use cases

They share every step: lock the row, resolve the caller to a side, apply
one `MatchRecord` transition, write, settle, publish. Four services would
be four copies of that sequence differing by one method call, which is the
duplication CLAUDE.md §2.7 forbids — and the sequence is where the
atomicity lives, so four copies would be four places to get a transaction
boundary wrong.

They are still four *commands*: the protocol keeps them apart, the domain
keeps them apart, and the result says which one happened.

## Why the result carries the whole terminal state

`GameCommandResult` names the outcome, the reason and the winner rather
than only "it worked". The gateway has to fan out an authoritative event
and cannot compute those — it would have to know that resigning gives the
win to the opponent, which is the rule this module exists to keep on this
side of the boundary.

## No side in the request

Only `match_id` and the authenticated `player_id`. §1: "the client never
sends player_id or side as authority". The side is derived from the record
by `MatchRecord.side_of`, so a request naming a side is not something this
contract can express — the same structural guarantee `SubmitMoveRequest`
makes.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from app.modules.engine import PlayerSide
from app.modules.game.domain.exceptions import (
    DrawOfferAlreadyPending,
    DrawOfferNotAllowedYet,
    DrawOfferNotPending,
    DrawOfferNotRecipient,
    MatchNotActive,
    MatchNotFound,
    NotAMatchParticipant,
    StaleMatchState,
)
from app.modules.game.domain.result import MatchOutcome, TerminationReason


class GameCommand(StrEnum):
    """What a participant asked for.

    An enum rather than four methods on the port, because the transport
    routes four frames to one handler and a closed set is what lets that
    handler be a mapping instead of a chain of branches.
    """

    RESIGN = "resign"
    OFFER_DRAW = "offer_draw"
    ACCEPT_DRAW = "accept_draw"
    DECLINE_DRAW = "decline_draw"


@dataclass(frozen=True, slots=True)
class GameCommandRequest:
    """One participant's attempt at one command.

    Primitive-only, like `SubmitMoveRequest`: two identifiers, an enum and
    an optional instant. Nothing here is a domain object, so the gateway
    builds one from a decoded frame without importing `game.domain`.
    """

    match_id: UUID
    player_id: UUID
    """The **authenticated** player, from the socket's redeemed ticket.
    There is no side field and no winner field — see this module's
    docstring."""

    command: GameCommand
    received_at: datetime | None = None
    """When the frame arrived, if the transport timed it.

    Carried for the same reason `SubmitMoveRequest` carries one: the
    settlement instant should be when the player acted, not when the lock
    was finally granted. `None` lets the service use its own clock.
    """


@dataclass(frozen=True, slots=True)
class DrawOfferView:
    """A standing offer, as anything outside `game` may see it."""

    offered_by: PlayerSide
    offered_at_ply: int
    offered_at: datetime


@dataclass(frozen=True, slots=True)
class DrawAgreementView:
    """A match's whole draw agreement, as a transport may see it —
    A64-020.5D §11.

    The **facts**, not a viewer's permissions: which offer stands and which
    side may open a new one. Resolving that to "may *I* accept" is the
    transport's, because a resolution needs a viewer and this value is
    produced once per write rather than once per recipient.

    The same three facts `MatchSnapshot` carries, deliberately, so one
    projection serves the snapshot and the live frame — see
    `gateway.projections.draw_payload_for`.
    """

    offer: DrawOfferView | None
    may_offer_light: bool
    may_offer_dark: bool

    is_untouched: bool
    """Whether nobody has ever offered a draw in this match.

    The gateway skips the participant frame when this is true, so the
    overwhelmingly common game costs nothing (§22).

    **Not** "the agreement is currently quiet", which is the predicate this
    replaces and which was wrong in the one case that mattered: after a
    decline, the opponent's move is what restores the offerer's
    eligibility, and at that moment the agreement *is* quiet — so a
    "skip when quiet" rule suppressed exactly the frame that carried the
    good news. Found by the two-browser flow.

    A boolean rather than the thresholds themselves: §9 forbids publishing
    the cooldown bookkeeping, and a transport needs to know whether to send
    a frame, not the arithmetic behind it.
    """


@dataclass(frozen=True, slots=True)
class GameCommandResult:
    """What the command did, in terms a transport can serialise.

    Carries the terminal state when there is one, because the gateway fans
    out an authoritative event and must not derive an outcome — see this
    module's docstring.
    """

    match_id: UUID
    command: GameCommand
    acting_side: PlayerSide
    """Which side the authenticated player turned out to be. Derived by
    `game`, never supplied."""

    acting_player_id: UUID
    """Who acted. Echoed back so a transport addressing a per-seat frame
    can pair one player with one side without indexing a participant tuple
    — A64-020.5D §11."""

    ply: int
    """The match's ply, unchanged by every command here. Returned so a
    client can confirm that answering an offer did not move the board."""

    offer: DrawOfferView | None
    """The offer standing **after** the command: the new one for an offer,
    and `None` for an accept, a decline or a resignation."""

    outcome: MatchOutcome | None
    termination_reason: TerminationReason | None
    winner: PlayerSide | None
    """The result, when the command ended the match. `None` for an offer
    and a decline, which end nothing."""

    settled_at: datetime | None

    draw: DrawAgreementView
    """The agreement **after** this command, for the participant fan-out —
    §11. Both sides' facts, because both participants are told."""

    @property
    def is_terminal(self) -> bool:
        return self.outcome is not None


class GameCommandUseCase(Protocol):
    """`game`'s participant-command boundary — §5, §14.

    One method, taking a closed enum. A port with four methods would let a
    transport reach three of them while forgetting the fourth exists, and
    the whole point of the enum is that the routing table is checkable.
    """

    async def execute(self, request: GameCommandRequest) -> GameCommandResult:
        """Runs one command in one transaction.

        Raises, and the failures are part of the contract:

            MatchNotFound             no such match, or not this player's.
                                      One answer for both, so a client
                                      cannot enumerate live matches
            NotAMatchParticipant      reachable only for a match this
                                      caller can already see
            MatchNotActive            the match is not being played
            DrawOfferAlreadyPending   an offer already stands
            DrawOfferNotPending       nothing to accept or decline
            DrawOfferNotRecipient     the offerer tried to answer
                                      themselves
            DrawOfferNotAllowedYet    the spam rule — §3
            StaleMatchState           another writer got there first;
                                      retryable
        """
        ...


__all__ = [
    "DrawAgreementView",
    "DrawOfferAlreadyPending",
    "DrawOfferNotAllowedYet",
    "DrawOfferNotPending",
    "DrawOfferNotRecipient",
    "DrawOfferView",
    "GameCommand",
    "GameCommandRequest",
    "GameCommandResult",
    "GameCommandUseCase",
    "MatchNotActive",
    "MatchNotFound",
    "NotAMatchParticipant",
    "StaleMatchState",
]
