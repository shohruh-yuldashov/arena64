"""Wire schemas for the `auth` module."""

from app.modules.auth.presentation.schemas.browser import BrowserSession
from app.modules.auth.presentation.schemas.login import LoginRequest
from app.modules.auth.presentation.schemas.password_reset import (
    ForgotPasswordRequest,
    ResetPasswordRequest,
)
from app.modules.auth.presentation.schemas.register import RegisterRequest
from app.modules.auth.presentation.schemas.tickets import WebSocketTicketRead
from app.modules.auth.presentation.schemas.tokens import RefreshRequest, TokenPair
from app.modules.auth.presentation.schemas.verification import (
    ResendVerificationRequest,
    VerificationAccepted,
    VerifyEmailRequest,
)

__all__ = [
    "BrowserSession",
    "ForgotPasswordRequest",
    "LoginRequest",
    "RefreshRequest",
    "RegisterRequest",
    "ResendVerificationRequest",
    "ResetPasswordRequest",
    "TokenPair",
    "VerificationAccepted",
    "VerifyEmailRequest",
    "WebSocketTicketRead",
]
