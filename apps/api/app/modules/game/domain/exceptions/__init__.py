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

from app.core.exceptions import (
    ConflictError,
    DomainError,
    NotFoundError,
    RuleViolationError,
)


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


class MatchNotFound(NotFoundError):
    """No match with that identifier is visible to this caller — A64-015.4.

    `404`, and it is deliberately the **same** answer a caller gets for a
    match that exists and is somebody else's: `MatchAcceptanceService`
    translates `NotAMatchParticipant` into this before it reaches a route.
    A distinct status would let anybody enumerate live match identifiers by
    the difference between `403` and `404`, which is exactly the disclosure
    `GET /profiles/{username}` already refuses to make for deactivated
    accounts.
    """


class NotAMatchParticipant(NotFoundError):
    """The caller is not one of the match's two players.

    Raised by `MatchRecord.side_of`, and a `NotFoundError` rather than a
    `PermissionDeniedError` for the reason above: "that match is not
    yours" and "there is no such match" must be indistinguishable on the
    wire. It keeps its own class because *server* code branches on it —
    the acceptance service logs it differently from a genuinely unknown id
    (CLAUDE.md §9.7: operators get the detail, callers get the safe
    answer).
    """


class MatchNotPending(ConflictError):
    """The match is no longer awaiting acceptance — A64-015.4 §6.

    `409`: the request was well formed and the caller's view was stale.
    Covers every way the handshake can already be over — the opponent
    declined, the window expired, or both sides already accepted and the
    match is active.

    One type for all four, and the caller's recourse is identical in every
    case: re-read the pending match and stop. Splitting them would tell a
    declining player whether their opponent had already declined, which is
    a fact about somebody else's behaviour.
    """


class AcceptanceWindowClosed(ConflictError):
    """The answer arrived after the acceptance deadline.

    Distinct from `MatchNotPending` because the row is still *pending* when
    this fires — the reconciler simply has not reached it yet. A caller
    cannot tell the two apart on the wire (both are `409`), and that is
    intentional; the distinction exists so the service does not have to
    depend on a background job having run before it can refuse a late
    answer.
    """


class MatchNotActive(ConflictError):
    """A move was submitted for a match that is not being played —
    A64-016.3.

    `409`: the request was well formed and the caller's view was stale.
    Covers every state that is not `active` — still in acceptance,
    declined, expired, finished — deliberately as **one** failure, because
    a client's response to all of them is identical (stop sending moves)
    and distinguishing them tells a prober how far a match got.

    A `ConflictError` rather than a `NotFoundError` because by the time
    this can fire the caller has already been proven a participant, so
    there is nothing left to withhold — which is exactly the line
    `NotAMatchParticipant` draws from the other side.
    """


class NotYourTurn(ConflictError):
    """The submitting player does not own the side to move — A64-016.3.

    Distinct from `IllegalMoveSubmitted` even though both mean "not now":
    a client that played out of turn has a **synchronisation** problem and
    should resynchronise, while one that played an illegal move has a
    **rules** problem and should not. Collapsing them would make the first
    look like a bug in the client's own move generator, which is the one
    place a client is entitled to trust itself (AD-14 — two
    implementations, one corpus).
    """


class IllegalMoveSubmitted(RuleViolationError):
    """The path is not a legal move in the current position — A64-016.3.

    A `RuleViolationError`, unlike everything else here, because it is the
    one failure that is genuinely about the *rules* rather than about
    state or identity — and `ErrorCode.RULE_VIOLATION` is what a client
    should log loudly, since under AD-14 its own engine agreed the move was
    legal and one of the two is wrong.

    One type for every way a path can be wrong: no piece there, not that
    player's piece, not a legal step, or a mandatory capture available
    elsewhere. The engine knows which and says so in the server's logs; the
    client is told the move was illegal, because a client that could
    enumerate *why* has a rules oracle and does not need one — it has the
    same engine.
    """


class ClockExpired(ConflictError):
    """The mover's flag had already fallen when their frame arrived —
    A64-016.5 §4, §7.

    Distinct from `NotYourTurn` and from `IllegalMoveSubmitted`, because it
    is neither a synchronisation problem nor a rules problem: the move was
    legal and it was that player's turn, and they were simply too late.

    Raised **before** the engine is consulted, deliberately. A move from a
    player whose time had run out when the gateway received it is not a
    legal move that loses — it is a move that never happened, and telling
    them whether it would have been legal is a rules oracle for a position
    they are no longer entitled to play in.

    The comparison is against `received_at`, never against the instant the
    transaction ran. See `ClockState.has_flagged` for why the boundary is
    strict.
    """


class StaleMatchState(ConflictError):
    """The match moved on between reading its state and writing the result
    — A64-016.3.

    The optimistic-concurrency failure. A client should **retry** rather
    than treat it as a rejection: nothing about the move was wrong, another
    writer simply got there first.

    Not retried inside the service, deliberately. In practice the other
    writer is the opponent, so by the time a retry lands it is no longer
    this player's turn and they receive `NotYourTurn` — which is the
    correct answer, and a silent internal retry would turn it into a
    confusing one.
    """


__all__ = [
    "AcceptanceWindowClosed",
    "ClockExpired",
    "CorruptMoveLog",
    "IllegalMoveSubmitted",
    "InvalidMatchTransition",
    "MalformedMoveLog",
    "MatchNotActive",
    "MatchNotFound",
    "MatchNotPending",
    "NotAMatchParticipant",
    "NotYourTurn",
    "PositionHashMismatch",
    "ReplayError",
    "StaleMatchState",
    "ReplayResultMismatch",
    "UnsupportedEngineVersion",
]
