"""What the gateway counts — A64-016.1 §9.

Three counters and one observation, all through `platform.metrics`. Nothing
new is built here: A64-015.6 established that counters aggregate and
observations pass through, and the gateway is exactly the caller that
arrangement was built for — a connection tier emits per-socket events, and
one log record per socket on a fleet holding 40,000 of them is the volume
problem `AggregatingMetrics` exists to solve.

## Labels are bounded, and here it is a security rule as well as a cost one

A64-015.5 §9's cardinality argument applies unchanged: a label whose domain
is unbounded is one time series per value. On this tier there is a second
reason, and A64-016.1 §9 states it directly — **no player ids, no ticket ids,
no connection ids**. A ticket id in a metric label is a credential in a
system with broader read access than the store it came from, and a player id
in a label is a record of when somebody was connected, which is the sleep
schedule `show_last_seen` exists to withhold.

So every label below comes from a closed `StrEnum`, and the whole live series
count for this module is under twenty.

## What is deliberately not a metric

**Active connections is not a gauge.** `MetricsRecorder` publishes no gauge
at all, for the reason it records: a gauge is read at scrape time, which
needs the exporter to call into the process. The count is available as
`ConnectionRegistry.active_count` — a `ZCARD`, which is a better answer than
a cached number because it is true across the fleet rather than per process.
What is counted here instead is the *transitions*, from which a count is
derivable and a history is not.
"""

from enum import StrEnum
from typing import Final

#: Sockets that completed the handshake and were registered — §9's
#: "connection accepted".
CONNECTIONS_ACCEPTED: Final = "gateway.connections_accepted_total"

#: Handshakes refused before any connection was registered — §9's
#: "authentication rejected". Counted separately from a disconnect because
#: a refused handshake never became a connection: folding the two would make
#: "how many sockets did we hold" unanswerable.
CONNECTIONS_REJECTED: Final = "gateway.connections_rejected_total"

#: Connections that ended, by how — §9's "disconnect reason".
CONNECTIONS_CLOSED: Final = "gateway.connections_closed_total"

#: How long a connection lasted, in seconds.
#:
#: An **observation**, so it keeps its distribution — and the distribution is
#: the whole content. A mean session length on this tier is meaningless: the
#: shape is bimodal by construction (a tab closed immediately versus a game
#: played for an hour), and the number a mean produces describes neither
#: population. It is also low frequency — one per socket closed — so passing
#: it straight through costs nothing.
CONNECTION_DURATION: Final = "gateway.connection_duration_seconds"


#: Room joins that were admitted — §11's "room join success".
ROOM_JOINS: Final = "gateway.room_joins_total"

#: Room joins that were refused, by why — §11's "room join rejection".
ROOM_JOIN_REJECTIONS: Final = "gateway.room_join_rejections_total"

#: Connections detached from a room, by how — §11's "room leave".
ROOM_LEAVES: Final = "gateway.room_leaves_total"

#: Rooms this connection's join found in each state — §11's "active rooms".
#:
#: Counted as a **transition into a state** rather than published as a gauge,
#: for the reason `CONNECTIONS_ACCEPTED` is: `MetricsRecorder` has no gauge,
#: because a gauge is read at scrape time and needs the exporter to call into
#: the process. `rate(room_states{state=ready})` is the question an operator
#: actually has — how often does a room complete — and it is answerable from
#: a counter where "how many rooms exist right now" is not.
ROOM_STATES: Final = "gateway.room_states_total"

#: Recipient connections resolved, by whether this node holds them — §11's
#: "local versus remote route resolution".
#:
#: **Never labelled by node** (§11). One series per node is a cardinality
#: that grows with the fleet, and a node identifier in a metric is internal
#: topology in a system with broader read access than the registry. The
#: ratio is the operational question: a rising remote share is what says a
#: cross-node transport is now actually needed.
ROUTE_RESOLUTIONS: Final = "gateway.route_resolutions_total"


#: Move frames received — §15's "move submissions". Counted before any
#: validation, so `submissions - accepted - rejected` is the number that
#: were deduplicated by `request_id`.
MOVE_SUBMISSIONS: Final = "gateway.move_submissions_total"

#: Moves the game applied — §15's "accepted moves".
MOVES_ACCEPTED: Final = "gateway.moves_accepted_total"

#: Moves refused, by bounded category — §15's "rejected moves", "stale-state
#: conflicts" and "rate-limit rejections", which are three labels on one
#: counter rather than three counters: they are mutually exclusive outcomes
#: of one operation, and splitting them would make "what fraction of moves
#: are refused" a sum across metrics.
MOVES_REJECTED: Final = "gateway.moves_rejected_total"

#: Frames written to sockets this node holds — §15's "local deliveries".
LOCAL_DELIVERIES: Final = "gateway.local_deliveries_total"

#: Forwarding requests published to other nodes — §15's "remote-node
#: publishes". **One per node, not per connection**, which is what the
#: counter is for: a rate that tracks connections rather than nodes is the
#: symptom of a publisher that bypassed the routing plan.
REMOTE_PUBLISHES: Final = "gateway.remote_publishes_total"

#: Forwarding requests that could not be published — §15's "remote delivery
#: failures". Separate from `REMOTE_PUBLISHES` rather than a label on it,
#: because a failure is not an attempt that happened to end differently: an
#: alert fires on this rate alone.
REMOTE_PUBLISH_FAILURES: Final = "gateway.remote_publish_failures_total"


class MoveRejection(StrEnum):
    """Why a move was not applied.

    Closed and small, and every member maps to exactly one wire code — see
    `app/gateway/moves.py`. A category per *failure mode* rather than per
    exception type, so a new exception in `game` that means the same thing
    to a client does not become a new time series.
    """

    NOT_IN_ROOM = "not_in_room"
    NOT_A_PARTICIPANT = "not_a_participant"
    NOT_YOUR_TURN = "not_your_turn"
    ILLEGAL_MOVE = "illegal_move"
    STALE_STATE = "stale_state"
    """§15's "stale-state conflicts". A rising rate means players are
    genuinely racing — two clients submitting against the same ply — which
    on a two-player game means one of them has a broken turn indicator."""

    MATCH_NOT_ACTIVE = "match_not_active"
    RATE_LIMITED = "rate_limited"
    """§15's "rate-limit rejections". Counted here rather than on its own
    counter because from the client's side it is one of the ways a move can
    be refused, and an operator comparing it against the others is asking
    the right question."""

    MALFORMED = "malformed"
    """The frame decoded but its payload was not a move — no `match_id`, no
    `path`, or a `path` that is not a list of strings."""

    INTERNAL = "internal"
    """Something failed that the client cannot act on. Always accompanied
    by an `ERROR` log carrying the exception; this exists so the *rate* is
    visible without parsing logs."""


class RouteLocality(StrEnum):
    """Whether a recipient connection is held by this process."""

    LOCAL = "local"
    """Deliverable now, by writing to a socket this node holds."""

    REMOTE = "remote"
    """Held elsewhere. Undeliverable until A64-016.3's transport exists —
    which is what makes this counter the measure of how much that transport
    is worth."""


class RoomRejectionReason(StrEnum):
    """Why a socket was refused a room.

    Two members, matching the two error codes a client can act on. It is
    deliberately **not** three: "no such match" and "not your match" are one
    reason here for the same reason they are one code on the wire — a metric
    that separated them would let anybody with a dashboard confirm that a
    match id exists.
    """

    NOT_A_PARTICIPANT = "not_a_participant"
    """The match does not exist, or this player is not in it."""

    ROOM_UNAVAILABLE = "room_unavailable"
    """The player is a participant and the match is not in a state that has
    a room yet."""


class RoomLeaveReason(StrEnum):
    """How a connection left a room."""

    CLIENT = "client"
    """An explicit `room.leave`."""

    DISCONNECT = "disconnect"
    """The socket closed. The common case by a wide margin — most players
    close the tab rather than leaving politely."""


class RejectionReason(StrEnum):
    """Why a handshake never became a connection.

    Two members, and the coarseness matches `GatewayErrorCode`'s: the metric
    must not distinguish an expired ticket from a replayed one, because a
    dashboard that could would be a dashboard an attacker could read timing
    off. What an operator actually needs from this is "are handshakes
    failing", and both members answer it.
    """

    INVALID_TICKET = "invalid_ticket"
    """Nothing redeemable was presented. The expected steady-state value is
    a trickle of scanners; a spike is either an outage in the ticket store
    or somebody probing."""

    REGISTRATION_FAILED = "registration_failed"
    """The ticket was good and the registry could not be written. An
    infrastructure failure rather than a client one, and the distinction is
    why these are two members rather than one."""


class CloseReason(StrEnum):
    """How a connection ended.

    The one label an operator reads first during an incident, because the
    difference between `client` and `heartbeat_timeout` is the difference
    between "users are leaving" and "we are dropping them".
    """

    CLIENT = "client"
    """The peer closed, or the socket went away. The ordinary ending."""

    HEARTBEAT_TIMEOUT = "heartbeat_timeout"
    """Nothing arrived within the receive deadline — §9's "heartbeat
    timeout". A rising rate here means clients that believe they are
    connected and are not, which is invisible from the client side."""

    SERVER_ERROR = "server_error"
    """The lifecycle raised. Always accompanied by an `ERROR` log line
    carrying the exception; this exists so the *rate* is visible without
    parsing logs."""


__all__ = [
    "CONNECTIONS_ACCEPTED",
    "CONNECTIONS_CLOSED",
    "CONNECTIONS_REJECTED",
    "CONNECTION_DURATION",
    "CloseReason",
    "RejectionReason",
]
