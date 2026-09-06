"""One closed schema per event — analytics.md §39.

**The boundary that makes `properties` safe.** The column is `jsonb`, so
anything that reaches it is stored; what keeps arbitrary data out is that
nothing constructs an `AnalyticsEvent` without passing through here first.

Three rules, and each closes a different hole:

    unknown keys are rejected   not ignored. A client that sends `email`
                                gets a `422`, not a silently dropped field
                                and a `200` that teaches it to keep trying
    required keys are required  a missing dimension is an error, not a
                                `NULL` that shows up as an unexplained
                                bucket in a `GROUP BY`
    values are typed            enums against this module's vocabularies,
                                integers with ranges, and the three
                                attribution strings against a pattern

Pydantic with `extra="forbid"` does all three, and it is what the platform's
request schemas already use — one validation vocabulary rather than two.

## Why the client schemas are separate from the projection schemas

A projection is written by this repository from a payload the platform
itself produced. A client event arrives from a browser. The first can be
trusted to supply a `match_id`; the second cannot be trusted to supply
anything, and the difference shows in what each schema allows: no client
schema has a field the server does not either bound or ignore.
"""

from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field

from app.modules.analytics.domain import properties as p
from app.platform.analytics import EventName


class PropertySchema(BaseModel):
    """The base every event's properties extend.

    `extra="forbid"` is the whole point: an unknown key is a validation
    error rather than a stored one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


# --- client events -----------------------------------------------------------
#
# Bounded to the smallest thing that answers a metric. `landing_viewed` has
# three optional strings and no more, because M1 counts visitors and M2
# segments them by campaign; a fourth field would be a field no metric reads.

#: `^[a-z0-9_-]{1,64}$`, applied to all three attribution values — §17.
#: Lowercased and bounded rather than free: a campaign name is a label, and
#: an unbounded one is a high-cardinality string in a `GROUP BY`.
Utm = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_-]+$")]


class LandingViewed(PropertySchema):
    utm_source: Utm | None = None
    utm_medium: Utm | None = None
    utm_campaign: Utm | None = None


class RegisterCtaClicked(PropertySchema):
    placement: p.CtaPlacement


class PublicTournamentViewed(PropertySchema):
    tournament_id: str = Field(max_length=36)
    status: p.TournamentStatus


class ShareClicked(PropertySchema):
    surface: p.ShareSurface
    mechanism: p.ShareMechanism


# --- backend projections -----------------------------------------------------


class NoProperties(PropertySchema):
    """`user_registered`, `friend_request_sent`, `friendship_created`,
    `tournament_withdrawn`.

    Deliberately empty. The event's value is that it happened, to whom and
    when — all three are envelope fields, and a property here would be a
    dimension no metric asks for.
    """


class EmailVerified(PropertySchema):
    #: M5 reads the rate; this makes "how long did it take" answerable
    #: without joining back to `user_registered`.
    hours_since_registration: int = Field(ge=0, le=24 * 400)


class QueueJoined(PropertySchema):
    """F-B's third stage — the server accepting a queue ticket.

    `speed_class` is **optional, and was not** — A64-028.5A §25.

    A ticket carries a variant and a queue type; it does not carry a time
    control, so `_queue_ticket_enqueued` has never had a speed class to
    put here and its own docstring said this schema made the field
    optional. It did not. Every queue join therefore produced an outbox
    entry that failed validation, retried five times and was abandoned:
    the `queue_joined` stage of the funnel was empty in every environment
    that has ever run, and the poisoned rows accumulated permanently.
    `QueueLeft` beside it already had the correct shape, which is what
    made the mismatch invisible — the pair looked consistent in review.

    The field stays declared rather than deleted because matchmaking still
    owes it additively (A64-027.1 §49); when a ticket learns its time
    control this becomes populated rather than reintroduced.
    """

    variant: p.Variant
    rated: bool
    queue_type: p.QueueType
    speed_class: p.SpeedClass | None = None


class QueueLeft(PropertySchema):
    """A queue attempt that ended without a pairing — M7b.

    `queue_type` and `rated` are here so the metric's numerator and its
    denominator segment identically: M7b is `queue_left / (queue_left +
    match_found)`, and `match_found` carries both. A rate whose two halves
    are filtered by different dimensions is the defect §34 names.

    `speed_class` is still absent: a ticket carries a variant and a queue
    type, not a time control.
    """

    reason: p.QueueExit
    #: The server's own measurement. Bounded above by a day: a longer wait
    #: is a stuck ticket, which is an incident rather than a data point.
    waited_ms: int = Field(ge=0, le=86_400_000)
    variant: p.Variant | None = None
    queue_type: p.QueueType | None = None
    rated: bool | None = None
    speed_class: p.SpeedClass | None = None


class MatchFound(PropertySchema):
    match_id: str = Field(max_length=36)
    variant: p.Variant
    queue_type: p.QueueType
    #: `matchmaking.players_paired.waited_for_seconds`, in milliseconds —
    #: M7's only input, and the reason M7 is authoritative (§35).
    waited_ms: int = Field(ge=0, le=86_400_000)
    rated: bool


class MatchOfferResolved(PropertySchema):
    match_id: str = Field(max_length=36)
    resolution: p.OfferResolution


class MatchStarted(PropertySchema):
    """Not projected yet — see `TournamentEntered` for the same reason.

    `game.match_activated` carries `variant` and `rated` and no seats, so
    there is no player to attribute a seat row to. §19 records the additive
    fields; until they exist this schema is the contract A64-027.2 wrote
    and nothing writes through it.
    """

    match_id: str = Field(max_length=36)
    variant: p.Variant
    speed_class: p.SpeedClass | None = None
    rated: bool
    origin: p.MatchOrigin | None = None


class MatchCompleted(PropertySchema):
    """The one event `game.match_completed` already carries almost whole.

    `speed_class`, `duration_ms` and `origin` are **optional here and
    absent in practice**, because the domain event does not carry them —
    analytics.md §19 records all three as additive fields the `game` module
    owes, and a projection may not read a match back to fill them in.

    Optional rather than required, because the alternative is a projection
    that fails validation forever on an event the platform emits correctly:
    a poison row for a dimension nobody has added yet. The cost is that
    M11–M14's `speed_class` segmentation is unavailable until the field
    lands, which analytics.md states rather than leaves to be discovered.
    """

    match_id: str = Field(max_length=36)
    variant: p.Variant
    speed_class: p.SpeedClass | None = None
    rated: bool
    outcome: p.Outcome
    termination_reason: p.TerminationReason
    #: Absent for a draw and for an abort. Never absent for a win — the
    #: pairing is `MatchResult`'s invariant and a database `CHECK`.
    winner_side: p.WinnerSide | None = None
    ply_count: int = Field(ge=0, le=10_000)
    duration_ms: int | None = Field(default=None, ge=0, le=30 * 86_400_000)
    origin: p.MatchOrigin | None = None


class RatingChanged(PropertySchema):
    match_id: str = Field(max_length=36)
    variant: p.Variant
    speed_class: p.SpeedClass
    rating_before: float
    rating_after: float
    is_provisional: bool


class TournamentEntered(PropertySchema):
    """`tournament.player_registered` carries a player and a **name**.

    The name is dropped (§14). The format, variant, speed class and
    capacity live on the tournament rather than on the registration, and a
    projection may not read the tournament back — so they are optional and
    currently absent, which analytics.md §19 records as additive fields the
    `tournament` module owes.

    M15 counts participation and needs none of them; only its `format`
    dimension waits.
    """

    tournament_id: str = Field(max_length=36)
    format: p.TournamentFormat | None = None
    variant: p.Variant | None = None
    speed_class: p.SpeedClass | None = None
    rated: bool | None = None
    capacity: int | None = Field(default=None, ge=2, le=1024)


class TournamentCompleted(PropertySchema):
    tournament_id: str = Field(max_length=36)
    format: p.TournamentFormat | None = None
    entrant_count: int | None = Field(default=None, ge=0, le=1024)


class ChallengeSent(PropertySchema):
    """A friend challenge was sent — F-B's third activity signal.

    `speed_class` is **optional, and was not** — A64-030.4B.1, and the same
    defect P1-11 fixed for `QueueJoined` one schema up. The pair were written
    together, `QueueJoined` was corrected, and this one was missed for
    exactly the reason that made the first invisible: read beside its
    neighbours it looked consistent.

    `matchmaking.friend_challenge_created` carries a `time_control_id`, not a
    speed class, and `_challenge_created`'s own docstring said this schema
    made the field optional. It did not. Every friend challenge therefore
    produced an outbox entry that failed validation, retried five times and
    was abandoned — `challenge_sent` was empty in every environment that has
    ever run, and §30's third activity signal with it.

    **Not derived here.** The `TimeControlId` → `SpeedClass` mapping is a
    column of the `reference` catalogue, reachable only through the async
    `TimeControlCatalogue` read port, and it is a column precisely so it can
    change without a deploy. A projection is a pure synchronous function
    over one payload; hard-coding a second mapping beside the authoritative
    one would be a table that silently disagrees with the database the first
    time an operator edits a row.

    The field stays declared rather than deleted because matchmaking still
    owes it additively (A64-027.1 §49); when a challenge event carries its
    time control this becomes populated rather than reintroduced.
    """

    variant: p.Variant
    rated: bool
    speed_class: p.SpeedClass | None = None


class ChallengeResolved(PropertySchema):
    resolution: p.ChallengeResolution
    #: Present only when the challenge became a game.
    match_id: str | None = Field(default=None, max_length=36)


#: Every event's schema, keyed by name. Totality over `EventName` is asserted
#: by a test — an event with no schema is one whose properties nothing
#: validates, which is the hole this module exists to close.
SCHEMAS: Final[dict[EventName, type[PropertySchema]]] = {
    EventName.LANDING_VIEWED: LandingViewed,
    EventName.REGISTER_CTA_CLICKED: RegisterCtaClicked,
    EventName.PUBLIC_TOURNAMENT_VIEWED: PublicTournamentViewed,
    EventName.SHARE_CLICKED: ShareClicked,
    EventName.USER_REGISTERED: NoProperties,
    EventName.EMAIL_VERIFIED: EmailVerified,
    EventName.QUEUE_JOINED: QueueJoined,
    EventName.QUEUE_LEFT: QueueLeft,
    EventName.MATCH_FOUND: MatchFound,
    EventName.MATCH_OFFER_RESOLVED: MatchOfferResolved,
    EventName.MATCH_STARTED: MatchStarted,
    EventName.MATCH_COMPLETED: MatchCompleted,
    EventName.RATING_CHANGED: RatingChanged,
    EventName.TOURNAMENT_ENTERED: TournamentEntered,
    EventName.TOURNAMENT_WITHDRAWN: NoProperties,
    EventName.TOURNAMENT_COMPLETED: TournamentCompleted,
    EventName.FRIEND_REQUEST_SENT: NoProperties,
    EventName.FRIENDSHIP_CREATED: NoProperties,
    EventName.CHALLENGE_SENT: ChallengeSent,
    EventName.CHALLENGE_RESOLVED: ChallengeResolved,
}


def schema_for(name: EventName) -> type[PropertySchema]:
    """The schema for one event. Raises `KeyError` for an unregistered name,
    which is the failure worth having at startup rather than at ingestion."""
    return SCHEMAS[name]
