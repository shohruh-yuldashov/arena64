"""This module's typed failures, built on the platform hierarchy in
`app.core.exceptions` — never a parallel one.

Inheriting from the existing tree is what makes these work end to end with
no per-module wiring: `app/api/exception_handlers.py` maps by walking an
exception's MRO, so `InvalidMatchTransition(ConflictError)` already returns
`409` without this module registering a handler.

## One type, deliberately

Every failure this task can produce is the same failure: the match is not
in a state where the requested transition is defined. Starting a match
that is already running, moving before it starts or after it ends,
resigning a finished game, aborting a completed one — a caller's recourse
is identical in all of them, which is to re-read the match and stop.

Splitting them by transition would produce six classes nothing branches
on. If a caller ever does need to tell "already finished" from "never
started" — a client reconciling a stale view, most likely — that is the
change that earns the second type, and the message already carries the
distinction for a human reading a log.

## Why `ConflictError` and not `PreconditionFailedError`

`409` rather than `412`. The request was well formed and the caller's view
of the match was simply stale, which is what a conflict means; a `412` says
a stated precondition failed, and none was stated. It is also the status
the rest of the platform returns for exactly this shape — see
`FriendRequestAlreadyResolved`.

## No new wire code

Per the rule in `app.core.error_codes.ErrorCode`: a class exists for every
distinct failure because server code branches on the type, but a new
*code* is added only where a client must behave differently and the status
plus the endpoint cannot tell it apart. `game` has no endpoint yet, so
this rides the generic `conflict` code and the task that gives it one can
judge otherwise.
"""

from app.core.exceptions import ConflictError, DomainError


class InvalidMatchTransition(ConflictError):
    """The match is not in a state where this transition is defined.

    The message names the status the match is actually in and the
    transition that was attempted — the two facts an operator reading a
    log at 3am needs, and neither of which identifies a player.
    """


class ReplayError(DomainError):
    """A stored game could not be reconstructed — A64-014.8.

    Root of the replay failures, and deliberately its own family rather
    than a set of transitions under `InvalidMatchTransition`. The two mean
    different things to whoever is reading: a lifecycle refusal is a caller
    asking for something the match cannot do *now*, and one of these is a
    **record that cannot be true** — a log with a gap, a position the rules
    do not produce, a version this build cannot honour.

    One is a stale view. The other is data integrity, and `replay` and
    `fairplay` will want to page on it.
    """


class UnsupportedEngineVersion(ReplayError):
    """The game was played under rules this build cannot reproduce.

    Refused rather than replayed under the current rules. AD-15 exists
    because "replaying a 2025 game under the new engine could yield a
    different outcome than the one that was rated and displayed" — quietly
    substituting today's rules is the exact failure it names, and it would
    produce a plausible, wrong, permanent-looking answer.
    """


class MalformedMoveLog(ReplayError):
    """The log is not a contiguous sequence of plies from 1 — MT-5.

    "A gap makes the game unreplayable, which invalidates the result, the
    analysis, and the fair-play record simultaneously." Checked before a
    single move is applied, so a truncated or duplicated log is refused as
    a whole rather than half-replayed.
    """


class CorruptMoveLog(ReplayError):
    """A recorded move is not one the rules allow at the point it appears.

    Raised when the engine refuses a move the log claims was played, or
    when the log continues past the ply that ended the game. The original
    refusal is chained as the cause, because *which* rule refused it is the
    diagnostic — and in a replay it means the record is wrong rather than
    that a player was told no.
    """


class PositionHashMismatch(ReplayError):
    """A recorded position and the one the rules produce disagree.

    The most valuable failure here. It is caught on the ply that caused it
    rather than at the end, so it names the move whose semantics changed —
    which is the difference between "a rules fix moved this game" and "this
    game is wrong somewhere".
    """


class ReplayResultMismatch(ReplayError):
    """The reconstructed match ended differently from the record.

    Only raised when a replay states an expected result. A game that ended
    by a draw rule this build no longer applies fails here rather than
    being reported as a different game.
    """


__all__ = [
    "CorruptMoveLog",
    "InvalidMatchTransition",
    "MalformedMoveLog",
    "PositionHashMismatch",
    "ReplayError",
    "ReplayResultMismatch",
    "UnsupportedEngineVersion",
]
