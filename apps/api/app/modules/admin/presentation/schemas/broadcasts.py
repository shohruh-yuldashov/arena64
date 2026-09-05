"""The broadcast API's wire types — A64-027A §16, §18, §20.

Validation lives here rather than in the service, because this is the
boundary CLAUDE.md §2.4 names: everything past it is trusted, so everything
that arrives must be checked at it.

## What is validated, and what it prevents

    length          a title or body over the domain's bound is refused
                    before a row is attempted, with a message naming the
                    field rather than a database error
    emptiness       whitespace is not content
    control chars   a body carrying `\\u0000` or a terminal escape is
                    refused: it renders as nothing in a browser and as
                    something in a log
    audience shape  a named audience without recipients, or a platform-wide
                    one with them, is a contradiction rather than a default

There is deliberately **no** `html`, `markdown`, `url`, `image` or
`action_url` field. §16 permits an action and an image only if the domain
supports them, and it does not: `NavigationTargetType` is a closed set of
internal destinations precisely so that no administrator can write a link
into a notification, and adding a URL here would undo that in one line.
"""

import re
from datetime import datetime
from typing import Annotated, Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.modules.notifications.domain.broadcast import (
    MAX_BODY_LENGTH,
    MAX_NAMED_RECIPIENTS,
    MAX_TITLE_LENGTH,
    Broadcast,
    BroadcastAudience,
)

#: Everything in the C0 range except tab and newline, plus C1 and the
#: bidirectional overrides. Newlines are legitimate in a body; a `RLO`
#: character is how a message is made to read differently from what it says.
_FORBIDDEN_CHARS: Final = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f‪-‮⁦-⁩]")


def _clean(value: str, field: str) -> str:
    if _FORBIDDEN_CHARS.search(value):
        raise ValueError(f"{field} contains control characters")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field} cannot be empty")
    return stripped


class BroadcastCreateRequest(BaseModel):
    """What the composer submits."""

    model_config = ConfigDict(extra="forbid")

    title: Annotated[str, Field(min_length=1, max_length=MAX_TITLE_LENGTH)]
    body: Annotated[str, Field(min_length=1, max_length=MAX_BODY_LENGTH)]

    #: Which language it was written in. Not a translation key — nothing
    #: translates this text; a client marks it with `lang` so a screen
    #: reader pronounces it correctly.
    locale: Annotated[str, Field(min_length=2, max_length=8)]

    audience: BroadcastAudience

    recipients: Annotated[list[UUID], Field(max_length=MAX_NAMED_RECIPIENTS)] = []

    #: Minted by the client, once per composition. Two submissions of one
    #: form carry one key and produce one broadcast — §18's protection
    #: against the double-click that reaches every inbox twice.
    idempotency_key: Annotated[str, Field(min_length=8, max_length=64)]

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        return _clean(value, "title")

    @field_validator("body")
    @classmethod
    def _body(cls, value: str) -> str:
        return _clean(value, "body")

    @model_validator(mode="after")
    def _audience_matches_recipients(self) -> "BroadcastCreateRequest":
        """A contradiction is refused rather than resolved.

        Silently ignoring a recipient list on a platform-wide send is how an
        administrator who picked three people reaches everybody.
        """
        if self.audience is BroadcastAudience.SPECIFIC_PLAYERS and not self.recipients:
            raise ValueError("a named audience needs at least one recipient")
        if self.audience is BroadcastAudience.ALL_PLAYERS and self.recipients:
            raise ValueError("a platform-wide broadcast carries no recipient list")
        return self


class BroadcastResponse(BaseModel):
    """One broadcast, as the console reads it.

    **No recipient identities.** A `SPECIFIC_PLAYERS` broadcast reports how
    many it named, not whom: §20 forbids exposing recipient PII in the
    history, and a console that listed them would be a way of asking "who
    did somebody message" from a screen everybody with the role can open.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    title: str
    body: str
    locale: str
    audience: str
    channel: str
    status: str
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    #: How many accounts the audience resolved to. `None` until the worker
    #: has counted — never a zero standing in for "not counted yet".
    audience_size: int | None

    #: Rows written. Lower than `audience_size` by the number of players who
    #: have muted the category, which is a suppression rather than a failure.
    delivered: int

    named_recipients: int
    failure_reason: str | None

    @classmethod
    def of(cls, broadcast: Broadcast) -> "BroadcastResponse":
        return cls(
            id=broadcast.id,
            title=broadcast.title,
            body=broadcast.body,
            locale=broadcast.locale,
            audience=broadcast.audience.value,
            channel=broadcast.channel.value,
            status=broadcast.status.value,
            created_at=broadcast.created_at,
            started_at=broadcast.started_at,
            completed_at=broadcast.completed_at,
            audience_size=broadcast.audience_size,
            delivered=broadcast.delivered,
            named_recipients=len(broadcast.recipients),
            failure_reason=broadcast.failure_reason,
        )


class BroadcastPageResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    items: list[BroadcastResponse]


class AudienceSizeResponse(BaseModel):
    """How many accounts an audience currently reaches.

    Computed server-side. §14: the console must not estimate the number an
    administrator reads immediately before sending to everybody.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    audience: str
    size: int


__all__ = [
    "AudienceSizeResponse",
    "BroadcastCreateRequest",
    "BroadcastPageResponse",
    "BroadcastResponse",
]
