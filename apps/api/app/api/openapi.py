"""Documenting a route's failure modes — the interface-layer companion to
`app.api.exception_handlers`.

That module decides which status an exception becomes. This one is how a
route *declares* the statuses it can return, so the generated schema says
so and a generated client has a branch for each. The pair is the whole
story: an endpoint whose handler can 404 but whose decorator does not
mention 404 is documented wrongly, and nothing catches it — the code still
works, the docs are still valid, and only a client integrator finds out.

## Why this is a module and not four copies

Every router on the platform declared its own `type _Responses = dict[int |
str, dict[str, Any]]` — four identical aliases (A64-012.8 removed them) —
and then repeated `"model": ErrorResponse` at roughly fifteen call sites.
The alias is pure duplication. The repetition is worse than duplication,
because **omitting it is silent**: FastAPI falls back to documenting
`{"detail": ...}`, which is a shape this platform never returns from
anywhere, and the generated docs then describe an error body that does not
exist.

`error_response` below makes that omission unexpressible.

## What is deliberately *not* shared here

The descriptions. It is tempting to hoist a single `UNAUTHORIZED` constant,
and the four routers' 401 texts are proof of why not: `auth` says "the
credential was missing, malformed, expired or revoked" because it is
talking to a caller *presenting* a credential, while `profiles` and
`avatars` say "no access token was presented" because they are talking to a
caller who is meant to already hold one. Those read almost the same and
answer different questions, which is exactly CLAUDE.md §2.7's "shared
abstractions that only accidentally coincide". A shared constant would make
one of the two wrong, and the wrongness would be invisible in review.

So this module shares the *mechanism* and leaves the *meaning* with the
endpoint that has it.
"""

from typing import Any

from app.api.exception_handlers import ErrorResponse

#: FastAPI's own annotation for the `responses=` argument on a route
#: decorator.
#:
#: Spelled once rather than inferred, because `dict[int, dict[str, object]]`
#: is what Python infers from a literal and it is **not assignable** to what
#: FastAPI declares — every router that wrote the literal inline needed this
#: alias to type-check, which is why there were four of them.
type Responses = dict[int | str, dict[str, Any]]


def error_response(status: int, description: str) -> Responses:
    """One documented failure mode, carrying the platform's error shape.

    `ErrorResponse` is the only body an Arena64 error ever takes
    (`app.api.exception_handlers`), so binding it here means a route cannot
    document a failure *without* it. That is the point: the alternative is a
    dict literal whose `"model"` key somebody forgets, producing docs that
    promise FastAPI's default `{"detail": ...}` — a shape no endpoint on
    this platform has ever returned.

    Returns a fresh dict each call, so a caller merging several with `**`
    cannot mutate a shared one.

    Merge rather than nest at the call site::

        responses={**_NOT_FOUND, **_UNPROCESSABLE}

    which is what FastAPI expects and what every router here already does.
    """
    return {status: {"description": description, "model": ErrorResponse}}
