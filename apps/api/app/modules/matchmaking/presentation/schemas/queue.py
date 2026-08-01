"""Wire schemas for the queue endpoints.

Pydantic models at the boundary, mapping to and from `QueueTicket`. Nothing
here is a domain type: the aggregate carries a `datetime` and an enum, and
what a client receives is the same information in the shapes JSON has.

## Why the response carries the deadline and not a countdown

`expires_at` is an instant; a `seconds_remaining` would be stale the moment
it was serialised, and a client rendering "9:58" from it would drift against
a server that is the only authority on when the ticket actually dies. The
same choice `Retry-After` deliberately does *not* make — see
`app/api/exception_handlers.py` — and the difference is that a retry hint is
advisory while a queue deadline is a fact the server will act on.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.modules.game.public import ProductVariant
from app.modules.matchmaking.domain.queue_pool import QueueType, Region
from app.modules.matchmaking.domain.queue_ticket import QueueSnapshot, QueueStatus, QueueTicket


class JoinQueueRequest(BaseModel):
    """What `POST /matchmaking/queue` accepts.

    **Three fields, and none of them is a rating.** QT-2 makes the rating
    snapshot the platform's to record, and a client-supplied one would be a
    self-reported skill level on the endpoint that decides who you play —
    the single most valuable field on the platform to lie about.

    Nor is there a `player_id`: the account comes from the access token, so
    queueing as somebody else is not something this API can express.

    `variant` is a `ProductVariant` and not the engine's `BoardVariant`, so
    the generated OpenAPI schema lists only variants a player may choose.
    The English 8x8 fixture the engine uses for its perft oracle
    (`specs/game-engine/audit.md` §9) is not an accepted value and does not
    appear in the document — a validator on a wider enum would have kept it
    out of responses and left it advertised as input.
    """

    model_config = ConfigDict(extra="forbid")

    variant: ProductVariant = Field(
        default=ProductVariant.RUSSIAN_8X8,
        description=(
            "Which rules to play under. Only the variants Arena64 offers are "
            "accepted — the engine can play others, and they are not on the menu."
        ),
    )

    queue_type: QueueType = Field(
        description=(
            "Which pool to wait in. `ranked` moves your rating when the match "
            "finishes; `casual` does not."
        )
    )

    region: Region = Field(
        default=Region.GLOBAL,
        description=(
            "Where to look for an opponent. `global` — the default — means "
            "anywhere, and is the right answer unless you would rather wait "
            "longer for a shorter round trip."
        ),
    )


class QueueTicketResponse(BaseModel):
    """One ticket, as its owner sees it.

    Every field is the player's own, so nothing here is gated by a privacy
    setting — unlike almost every other response on the platform. There is
    no endpoint that renders somebody *else's* ticket, and there is not
    meant to be: who is currently queueing is exactly the information that
    would let a player wait for a favourable pool.
    """

    model_config = ConfigDict(extra="forbid")

    ticket_id: UUID
    variant: ProductVariant
    queue_type: QueueType
    region: Region
    status: QueueStatus

    rating_snapshot: int = Field(
        description=(
            "The rating this ticket was entered with. Fixed at entry — a rating "
            "that changes while you wait does not move your place in the pool."
        )
    )

    entered_at: datetime
    expires_at: datetime = Field(
        description=(
            "When this ticket stops being honoured. An instant rather than a "
            "countdown, so a slow response cannot make a client's timer wrong."
        )
    )

    waiting: int = Field(
        description=(
            "How many players are currently waiting in the same pool, including "
            "you. A reading rather than a promise — it changes continuously, and "
            "it is not a position in a line."
        )
    )

    @classmethod
    def of(cls, ticket: QueueTicket, snapshot: QueueSnapshot) -> "QueueTicketResponse":
        """The wire view of a ticket, with its pool's depth beside it.

        The snapshot is passed in rather than read here: a schema that
        fetched would be a query in the serialisation layer, and the route
        is where the two reads belong so their cost is visible at the call
        site.
        """
        return cls(
            ticket_id=ticket.id,
            variant=ticket.pool.variant,
            queue_type=ticket.queue_type,
            region=ticket.region,
            status=ticket.status,
            rating_snapshot=ticket.rating_snapshot,
            entered_at=ticket.entered_at,
            expires_at=ticket.expires_at,
            waiting=snapshot.waiting,
        )
