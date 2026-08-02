"""The two measurements `game` takes of the acceptance handshake —
A64-015.5 §7.

Published rather than private, and that is the unusual part worth arguing.
Metric *names* are normally an implementation detail; these are not, because
the thing they exist to inform is a `matchmaking` setting
(`MATCHMAKING_RESERVATION_TTL_SECONDS`) owned by another module. A name only
`game` knew would be a dashboard `matchmaking`'s operator cannot find, and a
tuning process nobody can follow.

So the names and their label sets are contract, and
`specs/matchmaking.md` §11.4 documents the process that reads them.

## Namespaced by emitter, like every event type

`game.*`, because `MatchAcceptanceService` is what takes the measurement,
and an operator filtering by producer filters on the prefix. That is the
same rule `DomainEvent.event_type` follows and the reason
`game.match_created` is not `matchmaking.match_created` even though a
pairing caused it.

## Why these two and not more

They are the pair that answers one question: *is thirty seconds right?*

    the histogram   how long people actually take
    the counter     how often each ending happens

Neither is sufficient alone, and the failure that makes both necessary is
concrete: a high `expired` share looks identical whether the window is too
short for people who are trying, or exactly long enough for people who are
not there. The histogram's tail is what separates them — see the tuning
process in `matchmaking.application.metrics`.
"""

from enum import StrEnum

#: How long, from a match being created, until something answered it.
#: Seconds as a float, like every duration on this platform.
MATCH_ANSWER_LATENCY = "game.match_answer_latency_seconds"

#: How each pairing's handshake ended.
#:
#: One increment per **match**, never per player: two acceptances are one
#: `both_accepted`. That is what makes this counter's total comparable with
#: the number of matches created, which is the ratio the tuning process
#: reads.
MATCH_OUTCOMES = "game.match_outcomes_total"


class AnswerLatency(StrEnum):
    """The four moments worth timing from a match's creation.

    `FIRST_RESPONSE` and `BOTH_ACCEPTED` are deliberately separate even
    though the second implies a first: the gap between them is *the
    opponent's* thinking time, which is the number that says whether the
    window is generous to one player at the other's expense.
    """

    FIRST_RESPONSE = "first_response"
    """The earlier of the two answers, whatever it was. Recorded once per
    match, so its count is the number of matches somebody engaged with."""

    BOTH_ACCEPTED = "both_accepted"
    """The activation. Only recorded when the match actually starts."""

    DECLINED = "declined"
    """A decision. Timed because a decline at twenty-eight seconds and one
    at two mean different things about the offer."""

    EXPIRED = "expired"
    """An absence. Always the window's own width — measured to the deadline
    rather than to the instant the sweep noticed, so the tail of this series
    describes players rather than the scheduler."""


class MatchOutcome(StrEnum):
    """How a handshake ended. Mutually exclusive, and exhaustive over
    `MatchRecordStatus`'s three settled states."""

    BOTH_ACCEPTED = "both_accepted"
    DECLINED = "declined"
    EXPIRED = "expired"


__all__ = ["MATCH_ANSWER_LATENCY", "MATCH_OUTCOMES", "AnswerLatency", "MatchOutcome"]
