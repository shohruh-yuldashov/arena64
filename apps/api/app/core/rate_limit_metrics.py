"""What the limiter publishes about itself — A64-028.6 §8.

## The gap this closes

A64-028.1 filed P1-7: `RateLimitSettings.fail_open` is `True`, with a
written rationale that is correct — failing closed would turn a limiter
outage into a total authentication outage, and Argon2id, `locked_until`
and 256-bit reset tokens remain in place. The finding was never the
trade-off. It was that **while Redis is unreachable every rate limit on the
platform is bypassed and nothing signals it except one log line**, so the
abuse window is as long as the outage and nobody is told it opened.

A log line is not a signal. These are.

`UNAVAILABLE` is the one an alert fires on: a non-zero rate means the
platform is running its six authentication endpoints with no abuse
prevention, which is a page rather than a ticket.

## Cardinality

`rule` is a rule **name** — a closed set of about fifteen constants
declared in the `rate_limits.py` modules, fixed at import. Never a subject,
never an address, never an email: the whole point of `subject_digest` is
that the limiter itself does not keep those, and a metric label would put
them back.
"""

from enum import StrEnum
from typing import Final

#: Every decision the limiter reached, by rule and outcome.
DECISIONS: Final = "rate_limit.decisions_total"

#: The limiter could not reach Redis. **The P1-7 metric.**
UNAVAILABLE: Final = "rate_limit.unavailable_total"

#: How long the Lua script took, including the network hop.
LATENCY: Final = "rate_limit.decision_duration_seconds"


class Decision(StrEnum):
    ALLOWED = "allowed"
    REFUSED = "refused"


class Availability(StrEnum):
    """What the limiter did when it could not reach its store.

    The two are opposite risks and an operator's response differs: open is
    an abuse window that closes when Redis returns, closed is an
    authentication outage that closes when Redis returns. Counting them
    together would hide which one this deployment chose.
    """

    FAILED_OPEN = "failed_open"
    FAILED_CLOSED = "failed_closed"


__all__ = ["DECISIONS", "LATENCY", "UNAVAILABLE", "Availability", "Decision"]
