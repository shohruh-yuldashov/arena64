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

from app.core.exceptions import ConflictError


class InvalidMatchTransition(ConflictError):
    """The match is not in a state where this transition is defined.

    The message names the status the match is actually in and the
    transition that was attempted — the two facts an operator reading a
    log at 3am needs, and neither of which identifies a player.
    """


__all__ = ["InvalidMatchTransition"]
