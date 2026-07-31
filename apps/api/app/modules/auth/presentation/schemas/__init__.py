"""Wire schemas for the `auth` module."""

from app.modules.auth.presentation.schemas.login import LoginRequest
from app.modules.auth.presentation.schemas.register import RegisterRequest
from app.modules.auth.presentation.schemas.tokens import RefreshRequest, TokenPair
from app.modules.auth.presentation.schemas.verification import (
    ResendVerificationRequest,
    VerificationAccepted,
    VerifyEmailRequest,
)

__all__ = [
    "LoginRequest",
    "RefreshRequest",
    "RegisterRequest",
    "ResendVerificationRequest",
    "TokenPair",
    "VerificationAccepted",
    "VerifyEmailRequest",
]
