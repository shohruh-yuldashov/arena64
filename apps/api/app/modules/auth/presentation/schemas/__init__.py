"""Wire schemas for the `auth` module."""

from app.modules.auth.presentation.schemas.login import LoginRequest
from app.modules.auth.presentation.schemas.register import RegisterRequest

__all__ = ["LoginRequest", "RegisterRequest"]
