"""Wire schemas for the `profiles` module."""

from app.modules.profiles.presentation.schemas.my_profile import (
    MyProfileResponse,
    ProfileUpdateRequest,
)
from app.modules.profiles.presentation.schemas.privacy import (
    PrivacySettingsResponse,
    PrivacySettingsUpdateRequest,
)
from app.modules.profiles.presentation.schemas.profile import (
    ProfileResponse,
    RatingResponse,
    RatingsResponse,
    StatisticsResponse,
)

__all__ = [
    "MyProfileResponse",
    "PrivacySettingsResponse",
    "PrivacySettingsUpdateRequest",
    "ProfileResponse",
    "ProfileUpdateRequest",
    "RatingResponse",
    "RatingsResponse",
    "StatisticsResponse",
]
