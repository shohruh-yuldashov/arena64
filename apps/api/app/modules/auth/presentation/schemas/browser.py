"""What a browser receives when a session starts or rotates — A64-020.2.

One type, and its shape is the security decision this whole surface exists
for: **there is no `refresh_token` field, and there will not be one.**

`TokenPair` carries both credentials because a native client can store both
safely. A browser cannot: every place a page can put a string —
`localStorage`, `sessionStorage`, a readable cookie — is readable by any
script that reaches that page, and a thirty-day credential in reach of a
script is a thirty-day credential in reach of an injection.

So the refresh token goes to the `Set-Cookie` header, `HttpOnly`, and the
body carries only what the page genuinely needs to hold in memory: a short
access token, when it expires, and who is signed in.
"""

from pydantic import Field

from app.core.dto import BaseResponseDTO
from app.modules.auth.application.services import BEARER_SCHEME, IssuedAccessToken
from app.modules.users.public import UserRead


class BrowserSession(BaseResponseDTO):
    """The signed-in state, as a page holds it.

    `expires_in` is the **access** token's remaining seconds, so a client
    can schedule a refresh rather than waiting for a `401`. The refresh
    token's own window is deliberately not published: the page cannot read
    the cookie, cannot act on the number, and telling it would only invite
    a client to build logic on a value it has no way to verify.

    `user` is `users.public.UserRead` — the same shape `GET /auth/me` and
    `POST /auth/register` already return. Returning it here saves a second
    round trip on every page load, and reusing the type means a browser and
    a native client cannot end up with different ideas of what a user is.
    """

    access_token: str = Field(
        description="A short-lived JWT. Hold it in memory; never persist it.",
    )
    token_type: str = Field(
        default=BEARER_SCHEME,
        description="Always `Bearer` — RFC 6750's scheme name.",
    )
    expires_in: int = Field(
        description="Seconds until the **access** token expires.",
    )
    user: UserRead = Field(
        description="The signed-in account, so a client needs no second call.",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIwMTlm"
                    "YjllYS0wYTBjLTdjZWMtOWM1Zi00MDI3MjdjMzFhOTYiLCJ0eXBlIjoiYWNjZXNzIn0."
                    "signature",
                    "token_type": "Bearer",
                    "expires_in": 900,
                    "user": {
                        "id": "019fb9ea-0a0c-7cec-9c5f-402727c31a96",
                        "username": "player_one",
                        "email": "player@example.com",
                        "is_active": True,
                        "is_verified": False,
                    },
                }
            ]
        }
    }

    @classmethod
    def of(cls, access: IssuedAccessToken, user: UserRead) -> "BrowserSession":
        """Assembles the response from the access token and the account.

        A classmethod for `TokenPair.of`'s reason: four call sites build
        this, and inline construction is how two of them drift into
        returning subtly different shapes.
        """
        return cls(
            access_token=access.token,
            token_type=access.token_type,
            expires_in=access.expires_in_seconds,
            user=user,
        )


__all__ = ["BrowserSession"]
