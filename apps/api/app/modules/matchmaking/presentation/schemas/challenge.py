"""The friend challenge HTTP surface — A64-022.2 §4, §5.

## What a client may send, and what it may not

Four fields on create: `recipient_id`, `time_control_id`, `variant`, `rated`.
That is the complete accepted surface, and everything §4 forbids is absent by
construction rather than by validation:

    challenger_id      the session says who this is. There is no field to
                       fill, so there is nothing to check
    status             the aggregate owns it
    created_at         the platform's clock (AD-07), never the caller's
    expires_at         derived from `created_at`, in one place
    created_match_id   written by A64-022.3 in the transaction that creates
                       the match; a `CHECK` refuses it on any other row
    raw clock numbers  a `TimeControlId` names an entry in a catalogue the
                       server owns — a client cannot invent "3+2"

`extra="forbid"`, so a body carrying `challenger_id` is a `422` rather than a
silently ignored field. A client that thought it was challenging on somebody
else's behalf should be told it was not.

## What the response carries

The challenge's own facts, plus the **other party's public profile** —
composed through `profiles`' batch directory, so it obeys exactly the privacy
rules `GET /profiles/{username}` does. No email, no block state, no private
field, and nothing this module decided about visibility on its own.
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.core.dto import BaseResponseDTO
from app.modules.game.public import ProductVariant
from app.modules.matchmaking.domain.challenge import Challenge, ChallengeStatus
from app.modules.profiles.presentation.schemas import ProfileResponse
from app.modules.reference.public import TimeControlId


class CreateChallengeRequest(BaseModel):
    """One player inviting one friend."""

    model_config = ConfigDict(extra="forbid")

    recipient_id: Annotated[
        UUID,
        Field(
            description=(
                "Who to invite. Must be a friend you have not blocked and who has "
                "not blocked you — the server decides both, from the social graph, "
                "and answers the same way for either."
            ),
            examples=["019fb9ea-0a0c-7cec-9c5f-402727c31a96"],
        ),
    ]
    time_control_id: Annotated[
        TimeControlId,
        Field(
            description=(
                "Which clock, by its stable code. Validated against the **active** "
                "catalogue, so a control that has been retired is refused for a new "
                "challenge while older ones remain readable."
            ),
            examples=["blitz_3_2"],
        ),
    ]
    variant: Annotated[
        ProductVariant,
        Field(description="Which rule set. One value today.", examples=["russian_8x8"]),
    ] = ProductVariant.RUSSIAN_8X8
    rated: Annotated[
        bool,
        Field(
            description=(
                "Whether you are **asking** for this to count towards ratings. Not "
                "an agreement: Arena64 requires both players to consent before a "
                "direct game affects ratings, and the recipient's half of that "
                "happens at acceptance. Nothing can currently grant it, because "
                "acceptance is not built yet."
            ),
        ),
    ] = False


class ChallengeResponse(BaseResponseDTO):
    """One challenge, with the other party's public profile."""

    id: Annotated[
        UUID,
        Field(
            description=(
                "The challenge identifier. Pass it to the decline and cancel "
                "endpoints — it is the only handle a client needs, and it is scoped "
                "to the two players involved."
            ),
            examples=["019fb9ea-1b2c-7def-8a45-90ab12cd34ef"],
        ),
    ]
    status: Annotated[
        ChallengeStatus,
        Field(
            description=(
                "Where the challenge is in its lifecycle. Both list endpoints return "
                "only live `pending` challenges; the other values appear in the "
                "response to the action that produced them. `accepted` is declared "
                "and unreachable until acceptance is built."
            ),
            examples=["pending"],
        ),
    ]
    player: Annotated[
        ProfileResponse,
        Field(
            description=(
                "The **other** party — the challenger on an incoming challenge, the "
                "recipient on an outgoing one. Identical in shape and in privacy "
                "behaviour to `GET /profiles/{username}`."
            ),
        ),
    ]
    time_control_id: Annotated[TimeControlId, Field(examples=["blitz_3_2"])]
    variant: Annotated[ProductVariant, Field(examples=["russian_8x8"])]
    rated: bool

    created_at: Annotated[datetime, Field(description="When the challenge was sent, UTC.")]
    expires_at: Annotated[
        datetime,
        Field(
            description=(
                "When it stops being answerable, UTC — twenty-four hours after it "
                "was sent. Server-authoritative: a challenge past this cannot be "
                "declined however the clock on a device reads."
            ),
        ),
    ]
    responded_at: Annotated[
        datetime | None,
        Field(
            default=None,
            description=(
                "When it was answered, UTC. `null` while it is `pending` — the two "
                "always agree, because a database CHECK enforces the pairing."
            ),
        ),
    ] = None
    created_match_id: Annotated[
        UUID | None,
        Field(
            default=None,
            description=(
                "The match acceptance produced. Always `null` today: acceptance is "
                "not built, and a client can never supply this."
            ),
        ),
    ] = None

    @classmethod
    def of(cls, challenge: Challenge, player: ProfileResponse) -> "ChallengeResponse":
        """The one place a stored challenge becomes a response.

        A named constructor rather than `model_validate`, and that is the
        safety property: `from_attributes` would copy any field somebody
        later added to the aggregate, where this can only serialise what it
        names.
        """
        return cls(
            id=challenge.id,
            status=challenge.status,
            player=player,
            time_control_id=challenge.time_control_id,
            variant=challenge.variant,
            rated=challenge.rated,
            created_at=challenge.created_at,
            expires_at=challenge.expires_at,
            responded_at=challenge.responded_at,
            created_match_id=challenge.created_match_id,
        )


__all__ = ["ChallengeResponse", "CreateChallengeRequest"]
