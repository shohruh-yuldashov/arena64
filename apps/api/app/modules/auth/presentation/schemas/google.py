"""Wire schemas for "Continue with Google" — A64-031.E1.

None of them carries a Google token, code or subject. What crosses to the
browser is whether Google is offered, and — to the browser that just
proved it at Google — which address a pending sign-in is for.
"""

from typing import Literal

from app.core.dto import BaseRequestDTO, BaseResponseDTO
from app.core.enums import Locale
from app.modules.auth.domain.google_sign_in import PendingSignIn
from app.modules.auth.presentation.schemas.login import LoginPasswordField
from app.modules.users.presentation.schemas.user import TimezoneField, UsernameField


class SignInProviders(BaseResponseDTO):
    """Which outside sign-in methods this deployment offers. The sign-in
    page shows a button for each `true`, and nothing for a `false`."""

    google: bool


class PendingGoogleSignInRead(BaseResponseDTO):
    """The step this browser's verified Google identity is waiting for."""

    purpose: Literal["create_account", "link_account"]
    email: str
    """The address Google vouched for — shown so the person knows which
    account they are creating or connecting."""
    next: str
    """The in-app path to go to afterwards, already checked."""

    @classmethod
    def of(cls, pending: PendingSignIn) -> "PendingGoogleSignInRead":
        return cls(purpose=pending.purpose.value, email=pending.email, next=pending.next_path)


class GoogleAccountRequest(BaseRequestDTO):
    """The `POST /auth/browser/google/account` body: what a Google identity
    still needs to become an account. The address comes from Google and
    there is no password, so neither is a field here."""

    username: UsernameField
    preferred_language: Locale = Locale.EN
    timezone: TimezoneField = "UTC"

    model_config = {
        "extra": "forbid",
        "json_schema_extra": {
            "examples": [
                {"username": "player_one", "preferred_language": "uz", "timezone": "Asia/Tashkent"}
            ]
        },
    }


class GoogleLinkRequest(BaseRequestDTO):
    """The `POST /auth/browser/google/link` body: the password of the
    account the address belongs to. No identifier — the account was fixed
    when Google returned, and is not the caller's to choose."""

    password: LoginPasswordField

    model_config = {
        "extra": "forbid",
        "json_schema_extra": {"examples": [{"password": "CorrectHorse1!"}]},
    }
