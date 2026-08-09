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

    CLOCK_EXPIRED = "clock_expired"
    """The mover had already flagged when the frame arrived. A rising rate
    against a flat `moves_accepted_total` means players are running out of
    time, which is a game outcome rather than a defect — it is here so an
    operator can tell that apart from a platform delay."""

    MALFORMED = "malformed"
    """The frame decoded but its payload was not a move — no `match_id`, no
    `path`, or a `path` that is not a list of strings."""

    INTERNAL = "internal"
    """Something failed that the client cannot act on. Always accompanied
    by an `ERROR` log carrying the exception; this exists so the *rate* is
    visible without parsing logs."""


#: Reconnect attempts, by how they were answered — A64-016.6.
RESUMES: Final = "gateway.resumes_total"


class ResumeOutcome(StrEnum):
    """How a reconnect was answered.

    The distribution is the operational question, and it is why these are
    five labels on one counter rather than five counters: `incremental`
    should dominate, `snapshot` should be rare, and a rising
    `resync_required` means the event buffer is too small for the
    disconnection lengths this deployment actually sees.
    """

    CURRENT = "current"
    """The client had missed nothing. The fast path, and the common one for
    a socket that dropped and returned within a second."""

    INCREMENTAL = "incremental"
    """The buffer proved it held the whole gap. What should dominate."""

    SNAPSHOT = "snapshot"
    """The client asked to start over, or was too far behind."""

    RESYNC_REQUIRED = "resync_required"
    """The gap could not be proven complete. A rising rate is the signal to
    lengthen the buffer rather than a defect."""

    NOT_A_PARTICIPANT = "not_a_participant"
    """Refused. Covers an unknown match too — the two are one answer."""


#: Spectator joins that were admitted — §7.
SPECTATOR_JOINS: Final = "gateway.spectator_joins_total"

#: Spectator joins refused, by bounded reason — §7.
SPECTATOR_REJECTIONS: Final = "gateway.spectator_rejections_total"

#: Spectators detached, by how — §7's "active spectator count", counted as
#: transitions for the reason every other count on this tier is: there is no
#: gauge, because a gauge is read at scrape time and needs the exporter to
#: call into the process.
SPECTATOR_LEAVES: Final = "gateway.spectator_leaves_total"

#: Frames a spectator could not be sent — §7's "spectator delivery
#: failures". Separate from the participants' `LOCAL_DELIVERIES`, because a
#: spectator that missed a frame resynchronises and a participant that
#: missed one is mid-game.
SPECTATOR_DELIVERY_FAILURES: Final = "gateway.spectator_delivery_failures_total"


class SpectatorLeaveReason(StrEnum):
    """How a spectator stopped watching."""

    CLIENT = "client"
    DISCONNECT = "disconnect"


#: Frames delivered to a local socket on behalf of another node — A64-016.8.
#:
#: The counter that would have made A64-016.5's gap visible. `REMOTE_PUBLISHES`
#: says a frame was handed to the bus; this says one came off it and reached a
#: socket. A deployment where the first rises and the second stays at zero is
#: a fleet whose nodes cannot talk to each other, which is exactly the state
#: this platform shipped in until the forwarding loop existed.
FORWARDED_FRAMES: Final = "gateway.forwarded_frames_total"

#: Forwarded frames that reached nobody — the connection had closed, or the
#: entry could not be parsed. Not an error on its own: see `ForwardingRun`.
FORWARDING_FAILURES: Final = "gateway.forwarding_failures_total"


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


#: Participant commands received, labelled by command — A64-020.5C-pre.
#:
#: One counter with a `command` label rather than four counters, because
#: every question anybody asks of these is comparative: how often is a game
#: resigned versus drawn by agreement, and how many offers are declined.
#: Four series would make each of those a query joining four names.
GAME_COMMANDS: Final = "gateway.game_commands_total"

#: Participant commands refused, by bounded reason.
GAME_COMMANDS_REJECTED: Final = "gateway.game_commands_rejected_total"


class GameCommandRejection(StrEnum):
    """Why a participant command was not applied.

    Closed and small, like `MoveRejection` and for the same reason: a
    category per *failure mode* rather than per exception type, so a new
    exception in `game` that means the same thing to a client does not
    become a new time series.
    """

    NOT_IN_ROOM = "not_in_room"
    NOT_A_PARTICIPANT = "not_a_participant"
    MATCH_NOT_ACTIVE = "match_not_active"
    DRAW_OFFER_ALREADY_PENDING = "draw_offer_already_pending"
    DRAW_OFFER_NOT_PENDING = "draw_offer_not_pending"
    DRAW_OFFER_NOT_RECIPIENT = "draw_offer_not_recipient"
    DRAW_OFFER_NOT_ALLOWED_YET = "draw_offer_not_allowed_yet"
    """The spam rule fired — §3. Worth its own series: a rising rate says
    the rule is stricter than players expect, which is a product signal
    rather than an incident."""

    STALE_STATE = "stale_state"
    MALFORMED = "malformed"
    RATE_LIMITED = "rate_limited"
    INTERNAL = "internal"


#: Quick messages received from clients, by what became of them —
#: A64-023.1 §10.
#:
#: **One counter with a bounded `outcome` label**, not one per outcome: the
#: only question anybody asks of these is comparative — what share of sends
#: are refused, and which refusal dominates. Seven series in total, which is
#: the whole observability budget this feature gets.
#:
#: **No message identifier, no player, no match** (§10). The catalogue is
#: closed and small, so a `message` label would be *technically* bounded —
#: and it is still absent, because "which quick messages get used" is a
#: product question that multiplies every series here by six to answer, and
#: nothing operational needs it. If it is ever wanted it is a product
#: analytics event, not a gateway counter.
QUICK_MESSAGES: Final = "gateway.quick_messages_total"


class QuickMessageOutcome(StrEnum):
    """What happened to one quick-message frame.

    A category per *failure mode* rather than per exception type, the same
    rule `MoveRejection` and `GameCommandRejection` keep.
    """

    SENT = "sent"
    """Accepted and handed to the fan-out. **Not** "delivered" — delivery
    is best effort by design (§5), and counting it as success here would
    make this counter disagree with `LOCAL_DELIVERIES` for a reason that is
    not a defect."""

    REJECTED_INVALID = "rejected_invalid"
    """The frame named no match, or named something that is not in the
    catalogue. One label for both, because both mean the same thing to an
    operator: a client sending frames this server cannot use. A rising rate
    against a release is a **catalogue skew** — a client built for a
    different set of entries — which is the one thing this series is
    actually watched for."""

    REJECTED_NOT_IN_ROOM = "rejected_not_in_room"
    """The connection had not joined the match's room. The client's own
    sequencing mistake, and the label a spectator's send lands in — a
    viewer is in the audience, never in the room."""

    REJECTED_NOT_PARTICIPANT = "rejected_not_participant"
    """No such match, or this player is not in it. **One label for both**,
    for the reason `RoomRejectionReason` gives: a metric that separated
    them would let anybody with a dashboard confirm that a match id
    exists."""

    REJECTED_TERMINAL = "rejected_terminal"
    """The match is not being played. Overwhelmingly a player pressing a
    button on a game that has just ended, which is why it is worth
    distinguishing from the refusals that indicate a defect."""

    RATE_LIMITED = "rate_limited"
    """The connection's burst or sustained window is full. A rising rate is
    a **product** signal before it is an abuse one: it may mean the limits
    are tighter than ordinary play, and the two are told apart by whether
    it concentrates on few connections or spreads across many."""

    INTERNAL = "internal"
    """Something failed that the client cannot act on. Always accompanied
    by an `ERROR` log carrying the exception."""


#: Match offers this node attempted to put on a socket — A64-020.5D §4.
#:
#: One counter with a bounded `outcome` label rather than four names,
#: because every question asked of these is comparative: how many offers
#: reached somebody, and what share found nobody connected. A rising
#: `no_connection` share is the number that says whether push is worth
#: having at all, and it is meaningless without the total beside it.
#:
#: **No player, match, connection or pool label** — §4. Each of those is
#: unbounded, and an unbounded label is a metric that takes the process
#: down rather than one that answers a question.
MATCH_OFFER_PUSHES: Final = "gateway.match_offer_pushes_total"

#: One counter per announced notification — A64-021.2 §9.
#:
#: Labelled by the same three outcomes match offers use, so an operator
#: reads one dashboard for "did the push reach anybody" rather than two that
#: answer the question differently.
NOTIFICATION_PUSHES: Final = "gateway.notification_pushes_total"


class MatchOfferOutcome(StrEnum):
    """What happened to one pushed match offer."""

    LOCAL = "local"
    """Delivered to a socket on this node."""

    REMOTE = "remote"
    """Forwarded to another node's bus stream. Delivery there is that
    node's forwarder's, and its own failures are already counted by
    `REMOTE_PUBLISH_FAILURES`."""

    NO_CONNECTION = "no_connection"
    """Nobody was connected. **Not a failure** — it is the ordinary state
    of a player who queued and closed the tab, and the durable read is what
    tells them when they come back (§3)."""

    FAILED = "failed"
    """The publish raised. Counted rather than propagated: a delivery
    failure must not fail the relay tick that carried it."""


class NotificationPushOutcome(StrEnum):
    """What happened to one announced notification — A64-021.2 §9.

    The same four members `MatchOfferOutcome` has, and a separate enum
    rather than a shared one: they label different counters, and a shared
    enum would make widening one push's outcome set widen the other's.
    Sharing the *values* is what keeps a dashboard readable.
    """

    LOCAL = "local"
    """Delivered to a socket on this node."""

    REMOTE = "remote"
    """Forwarded to another node's bus stream — A64-021.2 §7."""

    NO_CONNECTION = "no_connection"
    """Nobody was connected. **Not a failure** — it is the ordinary state
    of a player who is not looking at the app, and the durable read is what
    tells them when they come back (§6)."""

    FAILED = "failed"
    """The publish raised. Counted rather than propagated: an announcement
    must not fail the relay tick that carried it, and the row it announces
    is already committed."""


__all__ = [
    "CONNECTIONS_ACCEPTED",
    "MATCH_OFFER_PUSHES",
    "NOTIFICATION_PUSHES",
    "MatchOfferOutcome",
    "NotificationPushOutcome",
    "CONNECTIONS_CLOSED",
    "CONNECTIONS_REJECTED",
    "CONNECTION_DURATION",
    "GAME_COMMANDS",
    "GAME_COMMANDS_REJECTED",
    "QUICK_MESSAGES",
    "CloseReason",
    "GameCommandRejection",
    "QuickMessageOutcome",
    "RejectionReason",
]
