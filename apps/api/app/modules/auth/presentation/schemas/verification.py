"""Wire schemas for the email-verification endpoints — A64-011.6."""

from pydantic import Field

from app.core.dto import BaseRequestDTO, BaseResponseDTO
from app.modules.auth.domain.otp import OTP_LENGTH
from app.modules.users.presentation.schemas.user import EmailField


class VerifyEmailRequest(BaseRequestDTO):
    """The `POST /auth/email/verify` body.

    The token travels in the body rather than a query string, even though
    the *link* the person clicks carries it as a query parameter. The link
    points at a frontend route; that page reads the parameter and posts it
    here. Query strings land in access logs, proxy logs and browser
    history, which is survivable for a 24-hour single-use token in the
    user's own browser and is not survivable in the API tier's logs.

    Inherits `extra="forbid"`, so a client sending `user_id` alongside the
    token is rejected rather than having it ignored.
    """

    token: str = Field(
        min_length=1,
        max_length=512,
        description="The `token` query parameter from the verification link.",
    )

    model_config = {
        "extra": "forbid",
        "json_schema_extra": {
            "examples": [{"token": "IqL9nQ8vXo2Zc7Rm4tYp1WkE6dGh0BsN3aFj5UvTxCe"}]
        },
    }


class VerifyCodeRequest(BaseRequestDTO):
    """The `POST /auth/email/verify-code` body — A64-021.5H §9.

    **No email address.** The session says who is verifying, so asking for
    the address again would be asking a caller to assert an identity they
    have already proved — and would make this endpoint accept an address
    somebody else owns.

    A `str`, not an `int`. `000042` is a code this platform issues
    (`domain.otp.generate_otp`), and an integer field would parse it as
    `42`, compare six characters against two, and reject a code that was
    correct. The pattern is the validation.
    """

    code: str = Field(
        min_length=OTP_LENGTH,
        max_length=OTP_LENGTH,
        pattern=rf"^\d{{{OTP_LENGTH}}}$",
        description="The six digits from the verification email.",
        examples=["482193"],
    )


class ResendVerificationRequest(BaseRequestDTO):
    """The `POST /auth/email/resend` body.

    Takes an address rather than requiring a bearer token, because the
    person who needs this is by definition the one who never received the
    first link — and may well never have signed in. Requiring
    authentication would make the endpoint useless to exactly its intended
    caller.

    `EmailField` reuses `users`' validator, so the address is normalised
    identically to registration and login. A malformed address is a
    field-level 422 and reveals nothing: an address that cannot be valid
    cannot belong to anyone.
    """

    email: EmailField = Field(description="The address the account was registered with.")

    model_config = {
        "extra": "forbid",
        "json_schema_extra": {"examples": [{"email": "player@example.com"}]},
    }


class VerificationAccepted(BaseResponseDTO):
    """The `POST /auth/email/resend` reply.

    **Identical whatever happened.** Unknown address, already-verified
    account, or a link genuinely sent — the body does not distinguish
    them, because distinguishing them would turn this endpoint into a
    membership oracle for any address an attacker cares to submit. See
    `EmailVerificationService`.

    A body rather than a bare `204`, because a client wants something to
    render, and the deliberately vague sentence *is* the product
    behaviour: "if an account exists for that address, we have sent a
    link."
    """

    detail: str = Field(
        description="A deliberately non-committal confirmation — see the endpoint docs."
    )
