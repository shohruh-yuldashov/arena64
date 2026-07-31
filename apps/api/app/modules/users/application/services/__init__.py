"""Application services — one class per cohesive set of use cases
(services.md §3)."""

from app.modules.users.application.services.user_service import UserService

__all__ = ["UserService"]
