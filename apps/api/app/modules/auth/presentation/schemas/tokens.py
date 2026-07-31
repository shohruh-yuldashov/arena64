"""Wire schemas for the token endpoints — A64-011.5.

These are the *only* Pydantic types on the platform that carry credential
material, and they exist precisely so that the internal ones do not.
`IssuedAccessToken` and `IssuedRefreshToken` are plain frozen dataclasses
for a documented reason — a Pydantic model is one keystroke from being a
FastAPI `response_model` — so the boundary needs a type that is
deliberately, visibly *meant* to be serialised. Copying across here is
that decision, made once, in the file whose whole job is the wire.

Reused rather than redeclared per endpoint: `TokenPair` is returned
identically by `POST /auth/login` and `POST /auth/refresh`, so a client's
token-handling code is written once and OpenAPI shows one schema instead
of two that must be kept in step.

Deliberately only two types. `GET /auth/me` returns
`users.public.UserRead` — the shape `POST /auth/register` already returns
— rather than an `AuthenticatedUserProfile` that would wrap it for no
gain, and there is no `SessionSummary` because nothing lists sessions
yet. Both were written and removed: a response schema with no endpoint is
the speculative generality CLAUDE.md §1 rule 7 warns about, and it is the
duplication this task asks to avoid rather than an example of avoiding it.
"""

from pydantic import Field

from app.core.dto import BaseRequestDTO, BaseResponseDTO
from app.modules.auth.application.services import BEARER_SCHEME, IssuedAccessToken


class TokenPair(BaseResponseDTO):
    """What a successful sign-in or refresh returns.

    The field names are RFC 6749 §5.1's (`access_token`, `token_type`,
    `expires_in`) rather than this platform's usual style, on purpose:
    every OAuth-aware HTTP client, SDK and debugging tool already knows
    them, and inventing `accessToken`/`ttl` would buy consistency with our
    own naming at the cost of interoperability with everything else.

    `expires_in` is the **access** token's remaining seconds, not the
    refresh token's. That is what the RFC specifies and what a client
    schedules its refresh against; the refresh token's own 30-day window
    is deliberately not published, because a client cannot act on it —
    when the refresh fails it must sign in again regardless.
    """

    access_token: str = Field(
        description="A short-lived JWT. Send it as `Authorization: Bearer <token>`.",
    )
    refresh_token: str = Field(
        description=(
            "An opaque, single-use token for `POST /auth/refresh`. Store it "
            "where scripts cannot reach it — it is rotated on every use, and "
            "presenting a rotated one revokes the entire session chain."
        ),
    )
    token_type: str = Field(
        default=BEARER_SCHEME,
        description="Always `Bearer` — RFC 6750's scheme name.",
    )
    expires_in: int = Field(
        description="Seconds until the **access** token expires, not the refresh token.",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIwMTlm"
                    "YjllYS0wYTBjLTdjZWMtOWM1Zi00MDI3MjdjMzFhOTYiLCJ0eXBlIjoiYWNjZXNzIn0."
                    "signature",
                    "refresh_token": "IqL9nQ8vXo2Zc7Rm4tYp1WkE6dGh0BsN3aFj5UvTxCe",
                    "token_type": "Bearer",
                    "expires_in": 900,
                }
            ]
        }
    }

    @classmethod
    def of(cls, access: IssuedAccessToken, refresh_token: str) -> "TokenPair":
        """Assembles the pair from the two services' results.

        A classmethod rather than inline construction at both call sites,
        so `login` and `refresh` cannot drift into returning subtly
        different shapes — which is exactly what "avoid duplicated
        response objects" is asking for.
        """
        return cls(
            access_token=access.token,
            refresh_token=refresh_token,
            token_type=access.token_type,
            expires_in=access.expires_in_seconds,
        )


class RefreshRequest(BaseRequestDTO):
    """The `POST /auth/refresh` body.

    The refresh token travels in the body rather than an `Authorization`
    header, because it is not a bearer credential for *this* API — it is
    a credential redeemable at exactly one endpoint. Putting it in the
    header would invite every interceptor and logging middleware written
    for access tokens to treat it as one.

    Never in a query string: those land in access logs, proxy logs and
    browser history, and unlike an access token this one is good for
    thirty days.

    Inherits `extra="forbid"` from `BaseRequestDTO`, so a client sending
    `user_id` alongside its token is rejected rather than ignored.
    """

    refresh_token: str = Field(
        min_length=1,
        max_length=512,
        description="The refresh token returned by `POST /auth/login` or a previous refresh.",
    )

    model_config = {
        "extra": "forbid",
        "json_schema_extra": {
            "examples": [{"refresh_token": "IqL9nQ8vXo2Zc7Rm4tYp1WkE6dGh0BsN3aFj5UvTxCe"}]
        },
    }
