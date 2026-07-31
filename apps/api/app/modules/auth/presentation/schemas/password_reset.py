"""Wire schemas for the password-reset endpoints — A64-011.7.

Two request bodies and no response schema, because neither endpoint has a
body to return: both reply `204 No Content`. See the router on why that is
the honest code for each of them.
"""

from pydantic import Field, SecretStr

from app.core.dto import BaseRequestDTO
from app.modules.auth.presentation.schemas.register import PasswordField
from app.modules.users.presentation.schemas.user import EmailField


class ForgotPasswordRequest(BaseRequestDTO):
    """The `POST /auth/password/forgot` body.

    Takes an address rather than requiring a bearer token, because the
    person who needs this cannot sign in — requiring authentication would
    make the endpoint useless to exactly its intended caller.

    `EmailField` reuses `users`' validator, so the address is normalised
    identically to registration and login. A malformed address is a
    field-level 422 and reveals nothing: an address that cannot be valid
    cannot belong to anyone.

    Inherits `extra="forbid"`, so a client sending `user_id` alongside the
    address is rejected rather than having it ignored — which matters here
    more than on most bodies, since a `user_id` a caller could supply is
    the shape of a request that resets somebody else's password.
    """

    email: EmailField = Field(description="The address the account was registered with.")

    model_config = {
        "extra": "forbid",
        "json_schema_extra": {"examples": [{"email": "player@example.com"}]},
    }


class ResetPasswordRequest(BaseRequestDTO):
    """The `POST /auth/password/reset` body.

    The token travels in the body rather than a query string, even though
    the *link* the person clicks carries it as a query parameter. The link
    points at a frontend route; that page reads the parameter, collects the
    new password, and posts both here. Query strings land in access logs,
    proxy logs and browser history — survivable for a one-hour single-use
    token in the user's own browser, and not survivable in the API tier's
    logs, where a token that replaces passwords would sit in plaintext
    beside the account it belongs to.

    **`password` reuses `PasswordField` from the registration schema**,
    which is the point rather than a convenience. It is a `SecretStr`
    (keeping the value out of reprs, out of the generated OpenAPI example
    and out of FastAPI's validation error payloads) and it runs the same
    `validate_password` the service and the domain run. A separate password
    type here is how a reset endpoint ends up accepting a password
    registration would have refused — the platform would then have two
    policies, and the weaker one would be reachable by anyone with an
    inbox.
    """

    token: str = Field(
        min_length=1,
        max_length=512,
        description="The `token` query parameter from the password reset link.",
    )
    password: PasswordField = Field(description="The new password. Must meet the password policy.")

    model_config = {
        "extra": "forbid",
        "json_schema_extra": {
            "examples": [
                {
                    "token": "IqL9nQ8vXo2Zc7Rm4tYp1WkE6dGh0BsN3aFj5UvTxCe",
                    "password": "CorrectHorse1!",
                }
            ]
        },
    }

    def plaintext_password(self) -> str:
        """Unwraps the secret at the one point it has to be unwrapped.

        A named method rather than `payload.password.get_secret_value()`
        inline in the handler, so that the single place a plaintext
        password escapes its wrapper on this path is greppable — and so a
        second call site has to be written on purpose.
        """
        secret: SecretStr = self.password
        return secret.get_secret_value()
