"""The session list's wire shape — A64-030.5C, SE-2.

## What a row may say, and what it may not

The list is a **security** screen: its whole purpose is that a player can
look at it and answer "was that me?". Everything below serves that
question, and nothing below is a credential.

Absent by construction, not by filtering:

    refresh token       exists only in transit and in the client
    token hash          `SessionDeviceSummary` has no field for it
    access token / jti  nothing here is derived from a JWT
    session row id      the wire identity is the family — see below
    ip_address          see "Why no address" below

The named constructor is what enforces that. `model_validate` on a model
with `from_attributes` would copy any field a later edit added to the read
model; `of()` can only serialise the four fields it names.

## Why the identifier is a token family

Because a session row is not a device. Rotation revokes the presented row
and inserts a successor, four times an hour for an open browser, so a row
id read from this list is stale within minutes — a "Revoke" button whose
target no longer exists, and a "current device" marker that moves on every
refresh. The family is minted once at sign-in and inherited by every
successor. `application/read_models.py` carries the full argument.

`id` rather than `token_family` on the wire, because to the client this is
simply the identity of a row it may revoke. What it is made of is this
platform's business.

## Why no address

SE-2 wants "originating region" and the column stores an address, but an
address is not a region: it is precise personal data about the person
reading the screen and about nobody else who benefits from seeing it. A
list that says "Chrome on macOS, last active 2 hours ago" already answers
"was that me?" for the case that matters, and the failure mode of getting
this wrong — publishing a home address into a page, a screenshot, a
support ticket — is not recoverable.

The column is still populated, correctly and behind the trusted proxy
boundary (`presentation/device.py`), because an operator investigating an
incident needs it. It is simply not part of this contract. Adding it later
is additive; removing it after it has been rendered is not.
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import Field

from app.core.dto import BaseResponseDTO
from app.modules.auth.application.read_models import SessionDeviceSummary
from app.modules.auth.presentation.device import parse_user_agent


class SessionRead(BaseResponseDTO):
    """One device a player is signed in on."""

    id: Annotated[UUID, Field(examples=["019fb9ea-0a0c-7cec-9c5f-402727c31a96"])]
    """What a revoke names. Stable across credential rotation."""

    is_current: bool
    """Whether this is the device making the request.

    Decided from the refresh cookie, which is the only credential that
    names a device — an access token names an account. `false` on every
    row when the caller presents no cookie, which is honest: a client
    that cannot say which device it is should not be told.
    """

    browser: Annotated[str | None, Field(examples=["Chrome"])] = None
    """The browser family, or `null` when it could not be told.

    Presentation only, and never a security decision: a `User-Agent` is
    attacker-controlled. `null` is a real answer and is rendered as
    "unknown browser" rather than guessed at — see
    `presentation/device.py`.
    """

    platform: Annotated[str | None, Field(examples=["macOS"])] = None
    """The operating system family, or `null`. Same rules as `browser`."""

    signed_in_at: datetime
    """When this device first signed in — not when its credential was
    last rotated."""

    last_active_at: datetime
    """When it last exchanged its refresh token. For a browser left open
    that is within the last fifteen minutes."""

    @classmethod
    def of(cls, device: SessionDeviceSummary, *, current_family: UUID | None) -> "SessionRead":
        """The one place a stored device becomes a response.

        The user agent is parsed **here** rather than in the application
        layer, because a label is presentation and a service that knew
        what a `User-Agent` was could only be called over HTTP.
        """
        label = parse_user_agent(device.user_agent)
        return cls(
            id=device.token_family,
            is_current=current_family is not None and device.token_family == current_family,
            browser=label.browser,
            platform=label.platform,
            signed_in_at=device.signed_in_at,
            last_active_at=device.last_used_at,
        )


__all__ = ["SessionRead"]
