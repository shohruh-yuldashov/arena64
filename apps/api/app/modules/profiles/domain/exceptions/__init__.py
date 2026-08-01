"""This module's typed failures, on the platform hierarchy in
`app.core.exceptions` — never a parallel one.

Inheriting from the existing tree is what makes these work end to end with
no per-module wiring: `app/api/exception_handlers.py` maps by walking an
exception's MRO, so `ProfileNotFound(NotFoundError)` already returns `404`
without `profiles` registering a handler.

Exactly one type, which is the whole failure surface of a read-only
endpoint. There is deliberately no `ProfileHidden`, no `AccountDeactivated`
and no `ProfileUnavailable`: each would be a way for the response to say
*why* nothing was returned, and the one design property this endpoint has
is that it never does. An exception with no raiser reads as "this case is
handled" to whoever adds the next endpoint — `auth` deleted an
`EmailAlreadyVerified` for exactly that reason.
"""

from typing import ClassVar

from app.core.error_codes import ErrorCode
from app.core.exceptions import NotFoundError


class ProfileNotFound(NotFoundError):
    """No visible profile for that username — 404.

    Raised identically whether the username was never registered or belongs
    to a deactivated account. Same status, same code, same message, same
    elapsed work — the caller has nothing to branch on.

    **A 404 here is not the membership oracle it is elsewhere.** `auth`
    goes to some length to avoid 404s on credential paths — `SessionNotFound`
    is deliberately a 401 — because there, confirming that a token or an
    address exists is a disclosure. A public profile is the opposite case:
    the endpoint's entire purpose is to tell anyone who asks whether a
    username belongs to a player, and the answer is already visible from
    every leaderboard, match record and chat message that player appears
    in. Withholding it would protect nothing and would break every client's
    "is this handle taken" affordance.

    What is withheld is *why* the answer is no, which is the part that
    would distinguish a free username from a withdrawn account.

    Carries the platform's generic `not_found` code rather than earning a
    `profile_not_found` of its own — the rule in
    `app.core.error_codes.ErrorCode`: a client cannot act differently on
    this than on any other 404, and the endpoint it came from already says
    what was not found.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.NOT_FOUND


__all__ = ["ProfileNotFound"]
