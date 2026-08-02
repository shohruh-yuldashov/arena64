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
