"""Login wire schemas.

Much thinner than `register.py`, and the asymmetry is intentional. A
registration schema enforces the full password policy at the boundary,
because telling someone their *new* password is too weak is the whole
point of the interaction. A login schema must not: the policy has changed
before and will change again, and a password that no longer satisfies
today's rules is still the correct password for an account created under
yesterday's. Rejecting it at the boundary would lock those people out
with a 422 that says "weak password" about a credential that is
demonstrably theirs.

So the only bound here is a length ceiling, and that is a resource guard
rather than a policy: it stops a multi-megabyte body from being
materialised and fed to a deliberately memory-hungry hash function.
"""

from typing import Annotated

from pydantic import Field, SecretStr

from app.core.dto import BaseRequestDTO
from app.modules.auth.domain.validators import PASSWORD_MAX_LENGTH
from app.modules.users.presentation.schemas.user import EmailField

LoginPasswordField = Annotated[
    SecretStr,
    # No `min_length`, and no policy validator — see this module's
    # docstring. An empty password is rejected by `min_length=1` only
    # because a blank submission is a client bug worth naming, not a
    # credential worth spending 20ms of Argon2 on.
    Field(min_length=1, max_length=PASSWORD_MAX_LENGTH),
]


class LoginRequest(BaseRequestDTO):
    """The `POST /auth/login` body.

    `email` reuses `users`' `EmailField`, so the address is trimmed and
    folded by exactly the validator registration used — "  Alice@Example.COM "
    and "alice@example.com" are the same account, which they must be or
    people simply cannot log in from a phone keyboard that capitalises.

    A malformed address is rejected here with a field-level 422, which is
    the right feedback for a form and reveals nothing: an address that
    cannot be valid cannot belong to anyone. Every other caller is covered
    by `AuthenticationService`, which routes the same failure into the
    generic `InvalidCredentials` path.

    `password` is a `SecretStr` — see `RegisterRequest` on why that
    matters at this layer specifically.

    Inherits `extra="forbid"` from `BaseRequestDTO`. A client sending
    `is_active` or `user_id` alongside its credentials is rejected rather
    than silently ignored.
    """

    email: EmailField
    password: LoginPasswordField

    model_config = {
        "extra": "forbid",
        "json_schema_extra": {
            # A64-011.9: the only two body schemas on the module without an
            # example were this one and `RegisterRequest` — the two most
            # likely to be the first thing anyone tries in the docs page.
            #
            # The example password satisfies the registration policy even
            # though *this* endpoint does not enforce it, so that someone
            # working through the docs top to bottom can register with it
            # and then sign in with it. An example that registration would
            # reject is an example that teaches the wrong shape.
            "examples": [{"email": "player@example.com", "password": "CorrectHorse1!"}]
        },
    }
