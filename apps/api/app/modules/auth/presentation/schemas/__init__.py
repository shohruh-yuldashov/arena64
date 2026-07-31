"""Wire schemas for the `auth` module."""

from app.modules.auth.presentation.schemas.login import LoginRequest
from app.modules.auth.presentation.schemas.register import RegisterRequest
from app.modules.auth.presentation.schemas.tokens import RefreshRequest, TokenPair

__all__ = ["LoginRequest", "RefreshRequest", "RegisterRequest", "TokenPair"]
