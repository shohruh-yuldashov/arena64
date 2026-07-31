"""Who the caller is — the shape every authenticated route receives.

Published (BE-03) because this is the one thing every other module will
need from `auth`. A `game` route asking "whose move is this" and a
`friends` route asking "who is sending this request" both need an
identity and neither may reach into `auth`'s internals to get one.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    """A proven identity, built entirely from a verified token.

    **No database row was read to produce this.** That is the point of a
    stateless token and it is worth stating loudly, because the name
    invites the opposite assumption: this is not a `User`, it does not
    have a username, an email or a profile, and it is not a snapshot of
    one. It is the set of things a signature proved.

    The consequence a caller must understand: these facts were true when
    the token was *issued*, not necessarily now. Within the fifteen-minute
    access-token window an account can be deactivated, suspended
    (domain-model.md SE-3) or have its password changed (SE-1), and this
    object will not know. Anything whose correctness depends on the
    account's *current* state must read that state — this proves identity,
    nothing more.

    Frozen, and deliberately not Pydantic: nothing here should ever be
    returned to a client. A client that just presented a token learns
    nothing from being handed the platform's reading of it, and a type
    that can be a `response_model` eventually is one.
    """

    id: UUID
    """The account identifier — `sub`. The same `UserId` that
    `users.public` publishes and that DM-06 makes the only reference that
    crosses a context boundary, so a route can pass it straight to any
    module without translation."""

    token_id: UUID
    """`jti` — which token proved this, not which session.

    Carried so that a log line written by a route can be joined to the
    `access_token_issued` line that minted the credential. When A64-011.4
    adds revocation, this is also the value a denylist is keyed on.
    """

    issued_at: datetime
    expires_at: datetime
    """Both instants, so a route or a long-lived WebSocket connection can
    reason about how much of the window is left without decoding the token
    a second time."""
