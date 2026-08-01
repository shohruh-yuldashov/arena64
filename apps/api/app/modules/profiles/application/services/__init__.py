"""Application services — one class per cohesive set of use cases
(services.md §3)."""

from app.modules.profiles.application.services.profile_service import ProfileService

__all__ = ["ProfileService"]
