"""The wire schema for AD-09's WebSocket ticket — A64-016.1.

One type, in `schemas/` rather than beside `IssuedWebSocketTicket`, for the
reason `tokens.py` records: the domain value is a plain frozen dataclass
precisely so it cannot become a `response_model` by accident, and the
boundary needs a type that is visibly meant to be serialised.
"""

from datetime import datetime

from pydantic import Field

from app.core.dto import BaseResponseDTO
from app.modules.auth.domain.tickets import IssuedWebSocketTicket


class WebSocketTicketRead(BaseResponseDTO):
    """A freshly minted, single-use ticket.

    Deliberately **not** shaped like `TokenPair`. That schema follows RFC
    6749 because every OAuth-aware client already knows its field names;
    this is not an OAuth artefact, nothing else speaks its protocol, and
    borrowing `expires_in` would suggest a refresh flow that does not exist
    — a spent ticket is not refreshed, it is replaced.
    """

    ticket: str = Field(
        description=(
            "Present as the `ticket` query parameter on `GET /ws`. "
            "Single-use: a second connection needs a second ticket."
        ),
    )
    expires_at: datetime = Field(
        description=(
            "When redemption stops working. Absolute rather than a duration, "
            "so a client that was backgrounded between minting and connecting "
            "can tell that its ticket went stale without attempting a socket."
        ),
    )

    @classmethod
    def of(cls, issued: IssuedWebSocketTicket) -> "WebSocketTicketRead":
        return cls(ticket=issued.value, expires_at=issued.expires_at)


__all__ = ["WebSocketTicketRead"]
