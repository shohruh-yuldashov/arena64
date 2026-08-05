"""`PendingMatchOffer` — one match, addressed to one of its two players,
ready to be pushed.

Framework-free (architecture.md §8): no HTTP, no ORM, no clock. It is a
value the realtime consumer builds and the sink delivers, and it exists so
that "what a connected player is told about a pending match" is one type
with one definition rather than a dictionary assembled at a call site.

## Why this is not `game.public.PendingMatchView`

They carry almost the same fields and they are not the same thing, which is
the kind of near-duplication worth arguing rather than collapsing.

`PendingMatchView` is `game`'s answer to *"what is this player's pending
match"* — a projection of `MatchRecord`, valid at the instant it was read,
with the opponent as an opaque `player_id` because `game` cannot resolve one
(DM-06). It is what the polling endpoint returns.

`PendingMatchOffer` is `matchmaking`'s **delivery**: it is addressed (it
knows its `recipient_id`, which the view does not, because the view *is* the
recipient's), it carries the opponent as a rendered preview or as nothing at
all, and it is the thing a transport puts on a socket. Collapsing them would
put a `users`-rendered handle on a `game` type, which is the boundary R-1
exists to keep.

The overlap is real and is the price of the boundary. What it buys is that
`game` never learns a username and the gateway never learns a `MatchRecord`.

## The opponent may be absent, and that is a delivery decision

`opponent` is `None` when the platform will not render the other player to
this recipient — a deactivated account, or a block created between the
pairing and the delivery. The **match is still delivered**, because it still
exists and still has to be answered; what is withheld is the name.

That asymmetry is the point. Withholding the offer would leave a player
holding a match they cannot see, which the acceptance deadline would then
expire against them. Withholding the *name* costs them a face on a card.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.modules.game.public import (
    MatchRecordStatus,
    MatchTimeControl,
    PlayerSide,
    ProductVariant,
)


@dataclass(frozen=True, slots=True)
class OpponentPreview:
    """The other player, as much of them as this recipient may see.

    The three fields `users.public.UserSummary` calls "what a list, a search
    result, or a future match card needs". This is that match card.

    Deliberately no avatar: rendering one needs the storage provider and the
    privacy-gated composition `profiles` owns, and duplicating that here
    would be a second, ungated renderer of a player's identity. A client
    that wants the full picture reads `GET /profiles/{username}`.
    """

    player_id: UUID
    username: str
    display_name: str | None


@dataclass(frozen=True, slots=True)
class PendingMatchOffer:
    """A match awaiting an answer, addressed to one participant."""

    recipient_id: UUID
    """Who is being told. **Not** on `PendingMatchView` — see this module's
    docstring on why an addressed delivery is a different type from an
    answer to a question the recipient asked."""

    match_id: UUID
    status: MatchRecordStatus
    """Always `pending_acceptance` at delivery — the consumer refuses to
    build an offer for anything else (§6). Carried anyway so the payload a
    client receives has the same shape as the one it polls, and a reconnect
    cannot tell the two apart."""

    your_side: PlayerSide
    opponent: OpponentPreview | None
    """`None` when the opponent may not be rendered to this recipient. See
    this module's docstring: the offer is still delivered."""

    variant: ProductVariant
    rated: bool
    """Whether finishing this match moves a rating. The **queue mode** a
    client renders as ranked or casual."""

    time_control: MatchTimeControl | None
    speed_class: str | None
    """How much time each side gets, and which rating a result would move.
    `None` for an untimed match — A64-020.5D §2.

    Carried so a pushed offer renders the same card the polled one does. A
    client that had to fetch the control separately would show a match card
    with a blank clock for one round trip, which is the flicker §16 asks to
    remove.
    """

    acceptance_deadline: datetime
    """When the offer stops being honoured. An instant rather than a
    countdown, so a slow socket cannot make a client's timer wrong — the
    same rule `QueueTicketResponse.expires_at` follows."""

    you_accepted: bool
    opponent_accepted: bool
    created_at: datetime

    def __post_init__(self) -> None:
        if self.acceptance_deadline <= self.created_at:
            raise ValueError("an offer cannot close before it opens")
        if self.opponent is not None and self.opponent.player_id == self.recipient_id:
            # A recipient rendered as their own opponent would be a
            # side-resolution bug that reaches a socket looking plausible.
            raise ValueError("a player cannot be their own opponent")

    def remaining_seconds(self, at: datetime) -> float:
        """How long is left to answer, floored at zero.

        On the offer rather than computed by the sink, because every
        transport will want it and a transport computing it from two
        timestamps is a transport that can get the sign wrong.
        """
        return max((self.acceptance_deadline - at).total_seconds(), 0.0)


__all__ = ["OpponentPreview", "PendingMatchOffer"]
