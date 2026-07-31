"""Application services — one class per cohesive set of use cases
(services.md §3)."""

from app.modules.auth.application.services.registration_service import RegistrationService

__all__ = ["RegistrationService"]
