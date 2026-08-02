"""The WebSocket ticket — AD-09's credential. A64-016.1.

`TokenType` has said for nine slices that the ticket "is the one plausible
future member, and it is named here rather than declared … It arrives with
its issuer." This is the issuer, and the ticket turned out **not** to be a
JWT — for exactly the reason A64-011.4's refresh tokens turned out not to be
one either.

## Why opaque and stored, not signed

AD-09 requires the ticket to be **single-use**. A signed token cannot be
single-use: verification is a pure function of the token and the key, so the
second presentation verifies exactly as well as the first. Making a JWT
single-use means a server-side record of what has been spent — at which
point the signature is doing no work the record is not already doing, and
the platform has two mechanisms where one would do.

So the ticket is what `database.md` DB-24 calls a *ticket value*: 256 bits
from a CSPRNG, hashed with SHA-256, and stored under its digest.
`OpaqueTokenService` already generates and hashes exactly this, and named
this as its fourth consumer before it existed. Redemption is a single
`GETDEL`, which is atomic — the concurrency requirement solved by the
storage engine rather than by a check the second presentation would also
pass.

## What it is bound to

A player, and the access token that asked for it. Nothing else: a ticket
that carried a match, a channel or a permission would be an authorization
decision made thirty seconds before it is used, which is thirty seconds in
which it can become wrong. It answers "who is this" and the gateway asks
everything else at the moment it matters.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class IssuedWebSocketTicket:
    """A freshly minted ticket, as its requester receives it.

    Carries the **plaintext**, which is the one moment on this platform it
    exists in memory outside a client — the store holds a digest and can
    never reproduce this. That asymmetry is the whole of DB-24, and it is
    why this type is separate from `RedeemedTicket`: a redemption gives back
    an identity, never a credential.
    """

    value: str
    """The opaque secret the client presents on connect. **Never logged**
    (services.md §8.5) and never persisted in this form."""

    expires_at: datetime
    """When redemption stops working.

    Returned to the client so it can decide whether to reuse a ticket it
    already has or ask for another — without it, a client that backgrounded
    a tab has no way to know its ticket went stale except by failing to
    connect, which costs a round trip and looks like an outage.
    """


@dataclass(frozen=True, slots=True)
class RedeemedTicket:
    """What a spent ticket proves.

    Deliberately **not** an `AuthenticatedUser`. That type carries token
    facts — `token_id`, `issued_at`, `expires_at` — which describe an access
    token's window, and a socket outlives that window by design: a
    connection held for an hour would otherwise appear to be authenticated
    by a credential that expired forty-five minutes ago.

    What a redemption establishes is narrower and true for as long as it
    matters: *this socket belongs to this player*.
    """

    player_id: UUID
    """Whose connection this is. The same identifier every other module
    already speaks (DM-06), so the gateway passes it on without
    translation."""

    session_id: UUID | None
    """The `auth` session the ticket was minted from, when there was one.

    Recorded rather than enforced, and carried through to the presence
    record for the reason `PresenceService.mark_online` states: a live
    challenge is delivered to a *connection*, and this is the value that
    will route it. `None` is legal because the ticket is bound to a player
    first — a caller holding a valid access token has proven identity
    whether or not the session behind it is still resolvable.
    """


__all__ = ["IssuedWebSocketTicket", "RedeemedTicket"]
