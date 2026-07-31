"""Registration wire schemas.

Validation is reused, not re-stated: username and email go through
`users`' validators, the password through `auth`'s. A rule change lands in
one function and applies at the HTTP boundary, in the service, and in the
domain constructor at once.
"""

from typing import Annotated

from pydantic import AfterValidator, Field, SecretStr

from app.core.dto import BaseRequestDTO
from app.core.enums import Locale
from app.modules.auth.domain.validators import (
    PASSWORD_MAX_LENGTH,
    PASSWORD_MIN_LENGTH,
    validate_password,
)
from app.modules.users.presentation.schemas.user import (
    DisplayNameField,
    EmailField,
    TimezoneField,
    UsernameField,
)


def _validate_secret_password(value: SecretStr) -> SecretStr:
    """Applies the password policy without ever unwrapping the secret into
    anything that outlives this call.

    The plaintext is read, checked, and dropped; the `SecretStr` wrapper
    is what continues down the stack, so nothing between here and the
    hasher holds a bare `str` that a `repr` could print.
    """
    validate_password(value.get_secret_value())
    return value


PasswordField = Annotated[
    SecretStr,
    # A length bound *on the wire type* as well as inside
    # `validate_password`. This one is not redundant with it: Pydantic
    # enforces it while parsing, before `_validate_secret_password` runs,
    # so a multi-megabyte password body is rejected during parsing rather
    # than after being materialised and passed around.
    Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH),
    AfterValidator(_validate_secret_password),
]


class RegisterRequest(BaseRequestDTO):
    """The `POST /auth/register` body.

    `password` is a `SecretStr`. Beyond the `repr` protection that gives
    every layer below, it is also what keeps the value out of the
    generated OpenAPI *example* and out of FastAPI's own validation error
    payloads — which is why the platform's
    `_handle_request_validation_error` drops each error's `input` field
    (A64-010): between them, there is no path by which a submitted
    password reaches a response body or a log line.

    Inherits `extra="forbid"` from `BaseRequestDTO`, so a client sending
    `password_hash` — trying to choose their own, pre-computed — is
    rejected rather than silently ignored.
    """

    username: UsernameField
    email: EmailField
    password: PasswordField
    preferred_language: Locale = Locale.EN
    timezone: TimezoneField = "UTC"
    display_name: DisplayNameField | None = None
