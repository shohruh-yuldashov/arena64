"""Domain events, turned into analytics facts — analytics.md §19.

**Pure.** No session, no clock, no I/O: a function from an outbox entry to
the events it becomes. That is what makes fourteen projections testable
without a database, and it is why the consumer beside it is thirty lines
rather than three hundred.

## What a projection may read

The **payload and the envelope, and nothing else**. No match read, no
profile read, no rating read — the same rule `statistics`' consumer follows,
for the same reason: by the time a relay delivers an event the row it
describes may have been archived, and a query here would be a second answer
to a question the event already settled.

Where a payload lacks something the taxonomy wants, the projection **omits
it** rather than inventing it. `speed_class` on a match is the live example:
`game.match_activated` does not carry one today, so §19 records it as an
additive field the domain must add, and until it does those projections
carry what exists. A projection that guessed would put a wrong dimension in
a store nobody would think to distrust.
"""

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from app.config.environment import Environment
from app.modules.analytics.domain.event import AnalyticsEvent, seat_event_id
from app.modules.analytics.domain.schemas import schema_for
from app.modules.analytics.domain.subject import SubjectKey
from app.platform.analytics import EventName, Owner, spec_for
from app.platform.outbox import OutboxEntry


class ProjectionError(Exception):
    """A known event whose payload does not match its contract.

    Distinct from "an event this consumer ignores", which is not an error at
    all (§17): the relay hands every consumer every entry, and most of them
    are somebody else's. This is the other case — an event analytics *does*
    track, whose payload it cannot read. That is a defect somewhere, and it
    is reported rather than swallowed.
    """


class Projection:
    """One domain event type's translation into analytics events."""

    def __init__(
        self,
        *,
        source_type: str,
        supported_versions: frozenset[int],
        build: Callable[["ProjectionContext"], Sequence["PendingEvent"]],
    ) -> None:
        self.source_type = source_type
        self.supported_versions = supported_versions
        self.build = build


class PendingEvent:
    """An analytics event before identity resolution.

    Projections know *which player* an event belongs to; they do not know
    that player's `subject_key`, because resolving one is a database read
    and a projection does no I/O. The consumer resolves them in one batch.
    """

    __slots__ = ("event_id", "name", "player_id", "properties")

    def __init__(
        self,
        *,
        event_id: UUID,
        name: EventName,
        properties: dict[str, Any],
        player_id: UUID | None = None,
    ) -> None:
        self.event_id = event_id
        self.name = name
        self.properties = properties
        self.player_id = player_id


class ProjectionContext:
    """What a projection is given: one entry, already type-checked."""

    __slots__ = ("entry",)

    def __init__(self, entry: OutboxEntry) -> None:
        self.entry = entry

    @property
    def payload(self) -> dict[str, Any]:
        return self.entry.payload

    def require(self, key: str) -> Any:
        """A payload field this projection cannot proceed without.

        Raises `ProjectionError` rather than `KeyError` so the consumer can
        tell a contract failure from a bug in its own code.
        """
        if key not in self.entry.payload:
            raise ProjectionError(f"{self.entry.event_type} payload has no {key!r}")
        return self.entry.payload[key]

    def optional(self, key: str) -> Any | None:
        return self.entry.payload.get(key)


def _uuid(value: Any, field: str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (ValueError, AttributeError, TypeError) as error:
        raise ProjectionError(f"{field} is not a uuid: {value!r}") from error


# --- the projections ---------------------------------------------------------


def _user_registered(ctx: ProjectionContext) -> Sequence[PendingEvent]:
    return [
        PendingEvent(
            event_id=ctx.entry.id,
            name=EventName.USER_REGISTERED,
            properties={},
            player_id=_uuid(ctx.require("user_id"), "user_id"),
        )
    ]


def _email_verified(ctx: ProjectionContext) -> Sequence[PendingEvent]:
    return [
        PendingEvent(
            event_id=ctx.entry.id,
            name=EventName.EMAIL_VERIFIED,
            properties={"hours_since_registration": int(ctx.require("hours_since_registration"))},
            player_id=_uuid(ctx.require("user_id"), "user_id"),
        )
    ]


def _players_paired(ctx: ProjectionContext) -> Sequence[PendingEvent]:
    """One outbox row, **two** analytics rows — one per seat.

    Both carry the same `waited_ms`, which is the pair's wait rather than
    each player's: `players_paired` measures one pairing and the two tickets
    ended together. M7 reads the distribution over pairings, so counting it
    twice would double every bucket — which is why §35 defines the metric
    over `match_found` **seats** and the query says `DISTINCT match_id`
    where it means pairings.
    """
    waited_ms = int(float(ctx.require("waited_for_seconds")) * 1000)
    common = {
        "match_id": str(_uuid(ctx.require("match_id"), "match_id")),
        "variant": str(ctx.require("variant")),
        "queue_type": str(ctx.require("queue_type")),
        "waited_ms": max(waited_ms, 0),
        "rated": str(ctx.require("queue_type")) == "ranked",
    }
    return [
        PendingEvent(
            event_id=seat_event_id(ctx.entry.id, seat),
            name=EventName.MATCH_FOUND,
            properties=dict(common),
            player_id=_uuid(ctx.require(f"{seat}_player_id"), f"{seat}_player_id"),
        )
        for seat in ("light", "dark")
    ]


def _queue_ticket_enqueued(ctx: ProjectionContext) -> Sequence[PendingEvent]:
    """F-B's third stage, and **server-accepted** rather than clicked.

    A64-027.1 §6 draws the line this projection sits on: a button press is
    intent, and the fact is the server persisting a ticket. `queue_joined`
    is projected from the ticket event and never from the client, which is
    why the taxonomy owns it to the backend.

    `speed_class` is absent — the ticket carries a variant and a queue type
    and not a time control. The schema makes it optional and §49 records it
    as the additive field `matchmaking` owes.
    """
    return [
        PendingEvent(
            event_id=ctx.entry.id,
            name=EventName.QUEUE_JOINED,
            properties={
                "variant": str(ctx.require("variant")),
                "queue_type": str(ctx.require("queue_type")),
                "rated": str(ctx.require("queue_type")) == "ranked",
            },
            player_id=_uuid(ctx.require("player_id"), "player_id"),
        )
    ]


def _queue_exit(reason: str) -> Callable[[ProjectionContext], Sequence[PendingEvent]]:
    """A queue attempt that ended without a pairing — M7b's numerator.

    ## Why a matched ticket can never reach here

    §13 of A64-027.5 warns against counting "match found, therefore the
    queue entry was removed" as abandonment. It cannot happen: the pairing
    service publishes `PlayersPaired` and **no ticket event at all**, so the
    only producers of these two are the player leaving and the sweep
    finding an expired window. Structural rather than filtered — there is
    no row to exclude.

    ## Two reasons, kept apart

    `cancelled` is a decision and `expired` is a timeout. M7b's dimension
    list names `reason` for exactly that: a product whose queue is
    abandoned by choice has a different problem from one whose queue times
    out, and merging them hides which.

    Grain: **one queue attempt** — one ticket, one row. A player who joins,
    leaves and joins again produced two attempts, which is what the metric
    counts (§15).
    """

    def build(ctx: ProjectionContext) -> Sequence[PendingEvent]:
        queue_type = str(ctx.require("queue_type"))
        waited_seconds = float(ctx.require("waited_for_seconds"))
        if waited_seconds < 0:
            # §50: never silently clamped. A negative wait is impossible and
            # is a contract failure, so the entry is skipped and counted as
            # a rejection rather than entering a distribution as a zero.
            raise ProjectionError(f"{ctx.entry.event_type} has a negative wait")

        return [
            PendingEvent(
                event_id=ctx.entry.id,
                name=EventName.QUEUE_LEFT,
                properties={
                    "reason": reason,
                    "waited_ms": int(waited_seconds * 1000),
                    "variant": str(ctx.require("variant")),
                    "queue_type": queue_type,
                    "rated": queue_type == "ranked",
                },
                player_id=_uuid(ctx.require("player_id"), "player_id"),
            )
        ]

    return build


def _match_activated(ctx: ProjectionContext) -> Sequence[PendingEvent]:
    """F-B's fourth stage, per seat — and the join key activation needs.

    `match_completed` is entity-level by A64-027.1 §18: one game has two
    perspectives and attributing it to one seat would count the game for
    one player and lose it for the other. So the completion knows *what
    happened* and not *to whom*, and these rows are what supply the second
    half — a `(subject, match_id)` pair per seat.

    Activation is then the join, computed at query time rather than stored:
    the earliest completion whose match this player started. §44 of the
    task is explicit that a derived fact must not become a raw event.

    Two rows from one outbox row, with ids derived by `seat_event_id`, so a
    redelivery conflicts on both and stores neither twice.
    """
    common = {
        "match_id": str(_uuid(ctx.require("match_id"), "match_id")),
        "variant": str(ctx.require("variant")),
        "rated": bool(ctx.require("rated")),
    }
    seats: list[PendingEvent] = [
        PendingEvent(
            event_id=seat_event_id(ctx.entry.id, seat),
            name=EventName.MATCH_STARTED,
            properties=dict(common),
            player_id=_uuid(ctx.require(f"{seat}_player_id"), f"{seat}_player_id"),
        )
        for seat in ("light", "dark")
    ]

    # **And the offer's resolution.** A match activates when both players
    # accepted, so this event is `both_accepted` — M9's numerator — at
    # match grain. It carries no seat, which is why its id is the outbox
    # id itself and cannot collide with either derived seat id.
    return [
        *seats,
        PendingEvent(
            event_id=ctx.entry.id,
            name=EventName.MATCH_OFFER_RESOLVED,
            properties={"match_id": common["match_id"], "resolution": "both_accepted"},
        ),
    ]


def _offer_refused(resolution: str) -> Callable[[ProjectionContext], Sequence[PendingEvent]]:
    """An offer that did not become a match — M9's other outcomes.

    Grain: **one offer**, which is one match. Entity-identified, because a
    matchmaking offer is created by the pairing scan rather than by either
    player, and an expiry has no actor at all — see the registry's
    `Identity.ENTITY` on why A64-027.5 corrected this.

    `declined` and `expired` stay apart. §52: an expiry is a real product
    outcome — somebody was offered a game and never answered — and folding
    it into "declined" would report indifference as refusal.

    `MatchAcceptedByPlayer` is deliberately **not** a resolution: it is the
    partial state where one side has answered and the other has not.
    Projecting it would count an offer twice, once half-resolved.
    """

    def build(ctx: ProjectionContext) -> Sequence[PendingEvent]:
        return [
            PendingEvent(
                event_id=ctx.entry.id,
                name=EventName.MATCH_OFFER_RESOLVED,
                properties={
                    "match_id": str(_uuid(ctx.require("match_id"), "match_id")),
                    "resolution": resolution,
                },
            )
        ]

    return build


def _match_completed(ctx: ProjectionContext) -> Sequence[PendingEvent]:
    """Match-level: no seat, no actor.

    The taxonomy's `ENTITY` identity, and §18's reason — one game has two
    perspectives, and attributing it to one of them counts the game for one
    player and loses it for the other.
    """
    properties: dict[str, Any] = {
        "match_id": str(_uuid(ctx.require("match_id"), "match_id")),
        "variant": str(ctx.require("variant")),
        "rated": bool(ctx.require("rated")),
        "outcome": str(ctx.require("outcome")),
        "termination_reason": str(ctx.require("termination_reason")),
        "ply_count": int(ctx.require("ply_number")),
        "origin": str(ctx.require("origin")),
    }
    # Three fields the payload carries as `None` for a legitimate reason —
    # a draw has no winner, a correspondence match may have no speed class
    # — so they are omitted rather than stored as nulls a `GROUP BY` would
    # then have to explain.
    for payload_key, property_key in (("winner", "winner_side"), ("speed_class", "speed_class")):
        value = ctx.optional(payload_key)
        if value is not None:
            properties[property_key] = str(value)
    return [
        PendingEvent(event_id=ctx.entry.id, name=EventName.MATCH_COMPLETED, properties=properties)
    ]


def _rating_updated(ctx: ProjectionContext) -> Sequence[PendingEvent]:
    return [
        PendingEvent(
            event_id=ctx.entry.id,
            name=EventName.RATING_CHANGED,
            properties={
                "match_id": str(_uuid(ctx.require("match_id"), "match_id")),
                "variant": str(ctx.require("variant")),
                "speed_class": str(ctx.require("speed_class")),
                "rating_before": float(ctx.require("rating_before")),
                "rating_after": float(ctx.require("rating_after")),
                "is_provisional": bool(ctx.require("is_provisional")),
            },
            player_id=_uuid(ctx.require("player_id"), "player_id"),
        )
    ]


def _challenge_created(ctx: ProjectionContext) -> Sequence[PendingEvent]:
    """A friend challenge was sent — and the third of §30's activity signals.

    A64-027.1 §30 defines an active player as somebody who started a match,
    entered a tournament **or sent a challenge**. Without this projection
    that definition was two-thirds implementable, so DAU would have
    undercounted anybody whose day was a challenge.

    `speed_class` is absent: the event carries a `time_control_id`, and
    mapping one to a speed class is `rating`'s knowledge rather than a
    projection's. Optional in the schema, and recorded in §49.
    """
    return [
        PendingEvent(
            event_id=ctx.entry.id,
            name=EventName.CHALLENGE_SENT,
            properties={
                "variant": str(ctx.require("variant")),
                "rated": bool(ctx.require("rated")),
            },
            player_id=_uuid(ctx.require("challenger_id"), "challenger_id"),
        )
    ]


def _challenge_resolution(resolution: str) -> Callable[[ProjectionContext], Sequence[PendingEvent]]:
    """Four domain events folded into one analytics event — M17.

    ## Attributed to the **challenger**, not the resolver

    A64-027.1 §18 described this event's identity as "the resolver", and
    A64-027.4 corrects it. Two reasons, and the second is arithmetic:

        an expiry has no resolver     nobody acted; the window closed. An
                                      `ACTOR` event needs a subject, and
                                      inventing one would attribute an
                                      absence to a person
        M17 is a ratio               `accepted / challenge_sent`, and
                                      `challenge_sent` is the challenger's
                                      event. Attributing acceptance to the
                                      recipient would make the numerator and
                                      the denominator count different people

    So all four resolutions belong to the person whose challenge it was. The
    recipient's decision is still measured — it is what `resolution` says.
    """

    def build(ctx: ProjectionContext) -> Sequence[PendingEvent]:
        properties: dict[str, Any] = {"resolution": resolution}
        match_id = ctx.optional("match_id")
        if match_id is not None:
            properties["match_id"] = str(_uuid(match_id, "match_id"))
        return [
            PendingEvent(
                event_id=ctx.entry.id,
                name=EventName.CHALLENGE_RESOLVED,
                properties=properties,
                player_id=_uuid(ctx.require("challenger_id"), "challenger_id"),
            )
        ]

    return build


def _friend_request_sent(ctx: ProjectionContext) -> Sequence[PendingEvent]:
    return [
        PendingEvent(
            event_id=ctx.entry.id,
            name=EventName.FRIEND_REQUEST_SENT,
            properties={},
            player_id=_uuid(ctx.require("requester_id"), "requester_id"),
        )
    ]


def _friendship_created(ctx: ProjectionContext) -> Sequence[PendingEvent]:
    return [
        PendingEvent(
            event_id=ctx.entry.id,
            name=EventName.FRIENDSHIP_CREATED,
            properties={},
            player_id=_uuid(ctx.require("addressee_id"), "addressee_id"),
        )
    ]


def _tournament_registered(ctx: ProjectionContext) -> Sequence[PendingEvent]:
    """`tournament.player_registered` carries the tournament's **name**.

    Dropped here and never stored — §14: a name is an unbounded string that
    answers no product question `tournament_id` does not.
    """
    return [
        PendingEvent(
            event_id=ctx.entry.id,
            name=EventName.TOURNAMENT_ENTERED,
            properties={"tournament_id": str(ctx.entry.aggregate_id)},
            player_id=_uuid(ctx.require("player_id"), "player_id"),
        )
    ]


def _tournament_completed(ctx: ProjectionContext) -> Sequence[PendingEvent]:
    """`winner_id` is in the payload and is **not** projected.

    A winner is a person, the event is entity-level, and no metric in §29
    reads it. Storing an identifier because it was available is exactly
    what §24 forbids.
    """
    return [
        PendingEvent(
            event_id=ctx.entry.id,
            name=EventName.TOURNAMENT_COMPLETED,
            properties={"tournament_id": str(ctx.entry.aggregate_id)},
        )
    ]


#: Which domain event each analytics event is projected from — §19's table,
#: as code. A domain event absent from here is one analytics ignores, which
#: is most of them.
PROJECTIONS: Final[dict[str, Projection]] = {
    projection.source_type: projection
    for projection in (
        Projection(
            source_type="users.registered",
            supported_versions=frozenset({1}),
            build=_user_registered,
        ),
        Projection(
            source_type="users.email_verified",
            supported_versions=frozenset({1}),
            build=_email_verified,
        ),
        Projection(
            source_type="matchmaking.queue_ticket_enqueued",
            supported_versions=frozenset({1}),
            build=_queue_ticket_enqueued,
        ),
        Projection(
            source_type="game.match_activated",
            supported_versions=frozenset({1}),
            build=_match_activated,
        ),
        Projection(
            source_type="matchmaking.queue_ticket_cancelled",
            supported_versions=frozenset({1}),
            build=_queue_exit("cancelled"),
        ),
        Projection(
            source_type="matchmaking.queue_ticket_expired",
            supported_versions=frozenset({1}),
            build=_queue_exit("expired"),
        ),
        Projection(
            source_type="game.match_declined",
            supported_versions=frozenset({1}),
            build=_offer_refused("declined"),
        ),
        Projection(
            source_type="game.match_acceptance_expired",
            supported_versions=frozenset({1}),
            build=_offer_refused("expired"),
        ),
        Projection(
            source_type="matchmaking.players_paired",
            supported_versions=frozenset({1}),
            build=_players_paired,
        ),
        Projection(
            source_type="game.match_completed",
            supported_versions=frozenset({1}),
            build=_match_completed,
        ),
        Projection(
            source_type="rating.updated",
            supported_versions=frozenset({1}),
            build=_rating_updated,
        ),
        Projection(
            source_type="matchmaking.friend_challenge_created",
            supported_versions=frozenset({1}),
            build=_challenge_created,
        ),
        Projection(
            source_type="matchmaking.friend_challenge_accepted",
            supported_versions=frozenset({1}),
            build=_challenge_resolution("accepted"),
        ),
        Projection(
            source_type="matchmaking.friend_challenge_declined",
            supported_versions=frozenset({1}),
            build=_challenge_resolution("declined"),
        ),
        Projection(
            source_type="matchmaking.friend_challenge_cancelled",
            supported_versions=frozenset({1}),
            build=_challenge_resolution("cancelled"),
        ),
        Projection(
            source_type="matchmaking.friend_challenge_expired",
            supported_versions=frozenset({1}),
            build=_challenge_resolution("expired"),
        ),
        Projection(
            source_type="friends.friend_request_sent",
            supported_versions=frozenset({1}),
            build=_friend_request_sent,
        ),
        Projection(
            source_type="friends.friend_request_accepted",
            supported_versions=frozenset({1}),
            build=_friendship_created,
        ),
        Projection(
            source_type="tournament.player_registered",
            supported_versions=frozenset({1}),
            build=_tournament_registered,
        ),
        Projection(
            source_type="tournament.completed",
            supported_versions=frozenset({1}),
            build=_tournament_completed,
        ),
    )
}


def project(entry: OutboxEntry) -> Sequence[PendingEvent]:
    """The analytics events one outbox entry becomes.

    Empty for an event analytics does not track. Raises `ProjectionError`
    for one it tracks and cannot read — including an **unsupported
    version**, which §18 requires be distinguishable from a malformed
    payload: interpreting a v2 payload with v1's reader is how a silent
    wrong number gets stored.
    """
    projection = PROJECTIONS.get(entry.event_type)
    if projection is None:
        return ()

    if entry.event_version not in projection.supported_versions:
        raise ProjectionError(
            f"{entry.event_type} v{entry.event_version} is not a version analytics reads"
        )

    return projection.build(ProjectionContext(entry))


def finalise(
    pending: PendingEvent,
    *,
    subject_key: SubjectKey | None,
    occurred_at: datetime,
    received_at: datetime,
    environment: Environment,
    is_synthetic: bool,
    source_event_id: UUID,
) -> AnalyticsEvent:
    """Validates a pending event's properties and seals it.

    The schema runs **here**, on the projector's own output, rather than
    only on the collector's input. A projection is repository code and could
    still be wrong — a renamed enum member, a field that stopped being
    populated — and a wrong dimension in a store nobody distrusts is worse
    than a rejected event.
    """
    schema = schema_for(pending.name)
    validated = schema.model_validate(pending.properties)

    return AnalyticsEvent(
        event_id=pending.event_id,
        event_name=pending.name,
        event_version=spec_for(pending.name).version,
        occurred_at=occurred_at,
        received_at=received_at,
        source=Owner.BACKEND.value,
        environment=environment,
        subject_key=subject_key,
        is_synthetic=is_synthetic,
        properties=validated.model_dump(mode="json", exclude_none=True),
        source_event_id=source_event_id,
    )
