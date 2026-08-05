"""The acceptance handshake, as `matchmaking` may reach it — A64-015.4.

architecture.md §7 draws one inbound edge into `game` and labels it
"creates match". Acceptance is the second half of that same edge: a pairing
is not finished when the row is written, it is finished when both players
have answered, and the endpoints that collect those answers live under
`/matchmaking` because a player who has not accepted yet is still, as far
as the product is concerned, being matched.

## Why the aggregate is still not published

R-3 has not moved. `MatchRecord` stays private and this package publishes a
**view** plus three commands — accept, decline, read your own. A consumer
holding the record could activate a match nobody accepted; what it actually
needs is narrower and completely expressible: *this player says yes to this
match.*

`PendingMatchView` is therefore a projection rather than the record. It
carries what a client must render and nothing a client must not know: no
`pairing_id`, no queue ticket ids, no `settled_at`, no opponent's
acceptance *instant* — only whether they have answered. Those are pairing
internals, and A64-015.4 §7 forbids exposing them.

## Every method takes the acting player

There is no `side` parameter anywhere. The side is derived from
`player_id`, which the route reads from the access token, so "accepting on
the opponent's behalf" is not something this contract can express — the
same design `/friends` and `/profile` use, and the same reason: an
ownership rule is strongest when the alternative cannot be typed.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.modules.engine import PlayerSide
from app.modules.game.domain.exceptions import (
    AcceptanceWindowClosed,
    MatchNotFound,
    MatchNotPending,
    NotAMatchParticipant,
)
from app.modules.game.domain.match_record import MatchRecordStatus
from app.modules.game.domain.variants import ProductVariant
from app.modules.game.public.matches import MatchTimeControl


@dataclass(frozen=True, slots=True)
class PendingMatchView:
    """One match, as one of its two participants sees it.

    **Asymmetric by construction.** `you_accepted` and
    `opponent_accepted` are named from the reader's seat rather than by
    side, so a route cannot render the wrong half by picking the wrong
    field — the mistake a `light_accepted`/`dark_accepted` pair invites
    every time somebody forgets which side the caller is on.
    """

    match_id: UUID
    status: MatchRecordStatus
    """Where the handshake stands. `pending_acceptance` while it is open,
    and the state it settled into once it is not — a client that polls
    after declining sees `cancelled` rather than a `404`."""

    your_side: PlayerSide
    opponent_player_id: UUID
    """DM-06's opaque identifier. Resolving it to a handle is `users`'
    job, and the route composes that separately — see
    `matchmaking.presentation.schemas.matches` on why the preview is not
    on this type."""

    variant: ProductVariant
    rated: bool

    time_control: MatchTimeControl | None
    """The clock this match is played under, or `None` if it is untimed —
    A64-020.5A-pre §10.

    Read off the **stored match**, never resolved from the catalogue: the
    acceptance dialog must show what these two players were actually paired
    into, and a catalogue lookup would show what that identifier means now.

    `None` for a tournament fixture, which is untimed today — see
    `game.public.matches` on why that is a gap rather than a policy.
    """

    speed_class: str | None
    """Which ladder this match moves, from **your own seat's** snapshot.

    A `str` rather than `rating`'s enum, for the reason `SeatRating.speed_class`
    is one: `game` must not import `rating` (R-4's one-way chain), so the
    vocabulary crosses as a primitive and the reader re-types it.

    Read from the seat rather than derived from `time_control`, because the
    seat is where it was *recorded* — deriving it would be `game` owning a
    duration-to-speed rule, which is precisely what `reference` exists to
    stop. `None` for a match created without seat snapshots.
    """

    acceptance_deadline: datetime
    """When an unanswered match stops being offered. An instant rather
    than a countdown, exactly as `QueueTicketResponse.expires_at` is: a
    client rendering "0:14" from a duration drifts against the server
    that is the only authority on when the offer actually dies."""

    you_accepted: bool
    opponent_accepted: bool

    created_at: datetime


class MatchAcceptanceUseCase(Protocol):
    """`game`'s side of the acceptance handshake.

    Three methods, and each is the narrowest thing that can be called a
    port: two commands that take an actor and a match, and one read scoped
    to the actor.
    """

    async def pending_match(self, player_id: UUID) -> PendingMatchView | None:
        """The match this player is in **right now** — one they have been
        offered and not answered, or one that has started.

        `None` rather than raising when there is none — a player who is
        neither being offered a match nor playing one is the ordinary case,
        and every caller branches on it.

        ## `active` is included, and why — A64-020.5A

        Acceptance is bilateral, so one player answers **first** and the
        match activates on the *other* one's request. The first acceptor's
        own response therefore reads `pending_acceptance`, and while this
        returned pending matches alone their next poll answered `None` —
        leaving the player who agreed soonest as the only one unable to
        learn their game had begun.

        Callers must therefore **branch on `status`** rather than treating
        a value as an offer. A lobby shows an acceptance dialog for
        `pending_acceptance` and hands off to the game for `active`; the
        realtime notifier pushes the first and skips the second, because an
        offer delivered over a game already in progress is worse than one
        delivered late.

        **No time window.** A match leaves this read by being played to an
        end, declined or expired — transitions the domain already makes.

        **At most one, by construction.** QT-1 gives a player one live queue
        ticket, a ticket produces at most one match, and a match holds its
        player from the offer until it ends — so "your current match" is
        singular without needing a rule of its own.
        """
        ...

    async def accept(self, *, player_id: UUID, match_id: UUID) -> PendingMatchView:
        """Records this player's acceptance, activating the match if it is
        the second one.

        **Idempotent for a repeat from the same player.** A client
        retrying after a dropped response gets the same view back rather
        than a `409`, because the outcome it asked for is already true.

        Raises `MatchNotFound` for an unknown match **and for one this
        player is not in** — the two must be indistinguishable, or live
        match identifiers become enumerable by status code.
        `MatchNotPending` once the handshake is over,
        `AcceptanceWindowClosed` past the deadline.
        """
        ...

    async def decline(self, *, player_id: UUID, match_id: UUID) -> PendingMatchView:
        """Records this player's refusal and cancels the match.

        One decline ends it, whatever the other side did. The refusals are
        `accept`'s, for the same reasons.

        **Not idempotent in the same way**: a second decline from the same
        player raises `MatchNotPending`, because by then the match is
        `cancelled` and there is nothing left to refuse. A client that
        needs to be safe against its own retry reads the view, which
        already says `cancelled`.
        """
        ...


class MatchAcceptanceExpiryUseCase(Protocol):
    """Expiring the pairings nobody answered — A64-015.4 §9.

    A **fourth capability rather than a fourth method** on the port above,
    and the split is the one every port pair on this platform makes: what
    differs is who may do it. A route holds `MatchAcceptanceUseCase` and
    therefore cannot expire anybody's match; the reconciliation task holds
    this and cannot accept on anybody's behalf.
    """

    async def expire_overdue(self, *, limit: int) -> Sequence[UUID]:
        """Expires up to `limit` pending matches whose window has closed,
        and returns their ids.

        **Safe under concurrent workers** and bounded, by the same
        `SELECT ... FOR UPDATE SKIP LOCKED` the outbox relay and the queue
        sweep already use — A64-015.4 §14 forbids inventing a mechanism,
        and this is the platform's proven one.

        Never raises for "nothing to do": an empty sequence is the
        overwhelmingly common answer and is not a failure.
        """
        ...


__all__ = [
    "AcceptanceWindowClosed",
    "MatchAcceptanceExpiryUseCase",
    "MatchAcceptanceUseCase",
    "MatchNotFound",
    "MatchNotPending",
    "MatchRecordStatus",
    "NotAMatchParticipant",
    "PendingMatchView",
]
