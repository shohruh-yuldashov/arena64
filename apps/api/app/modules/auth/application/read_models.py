"""The shape a session *read* returns — A64-030.5C.

Separate from `domain/sessions.py` for the reason
`notifications/application/read_models.py` gives: none of this is a domain
concept. `UserSession` is one row of a rotation chain and knows nothing
about how a list of devices is presented; this is what SE-2's screen needs
and is assembled by a query rather than by an aggregate.

## Why a device is a token family and not a session row

This is the substantive decision in the whole feature, so it is stated
where the shape is defined.

`SessionService.rotate_refresh_token` does not update a row. It revokes
the presented session and **inserts a successor** that inherits
`token_family` and `expires_at`. A browser refreshing every fifteen
minutes therefore produces a new `user_sessions` row four times an hour,
each one live for fifteen minutes and revoked thereafter.

So the row is not the device. Listing live rows would give a list whose
identifiers change four times an hour — a "Revoke" button whose target
stops existing while the page is open, and a "current device" marker that
moves to a different row on every refresh, which §3 names as exactly the
thing that must not happen.

The **family** is the device. It is minted once, at sign-in, is inherited
by every successor, and is what `revoke_family` already revokes as a unit
for reuse detection. `domain-model.md` §6.2's "a browser, a phone — that
can be listed and individually revoked" is the family; the row is one
rotation of its credential.

That is also why `signed_in_at` is the family's earliest `created_at`
rather than the live row's. The live row was created at the last refresh,
so reporting its `created_at` would tell a player that the laptop they
signed in on three weeks ago first appeared four minutes ago.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class SessionDeviceSummary:
    """One device on a player's session list.

    Carries no credential and no credential-derived value: there is no
    `refresh_token_hash` here, and there is deliberately no field for one.
    `SessionService.list_user_sessions` returns `UserSession` entities that
    do carry the hash — safe inside the module, and never the thing a
    route serialises.
    """

    token_family: UUID
    """The device's stable identity, and what a revoke names.

    Survives every rotation, so a client that read this list an hour ago
    can still revoke the device it saw.
    """

    signed_in_at: datetime
    """When this device first signed in — the family's earliest row."""

    last_used_at: datetime
    """When its credential was last exchanged. Moves on every refresh,
    which for a browser open in a tab is every fifteen minutes, so it is
    "last active" in the sense a player means it."""

    user_agent: str | None
    """The raw stored header, for `parse_user_agent` to label.

    Unparsed here on purpose: the application layer has no business
    knowing what a `User-Agent` is, and the label is localised by whoever
    renders it.
    """


__all__ = ["SessionDeviceSummary"]
