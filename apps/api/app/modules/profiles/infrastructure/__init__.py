"""Adapters satisfying the ports `profiles` declares in `application/`.

`unrated_providers.py` was renamed to `statistics_providers.py` by
A64-012.6, when one of the two placeholders it held stopped being one:
`DatabaseStatisticsProvider` reads a player's real record through
`statistics.public`, and `NoMatchesStatisticsProvider` became the fallback
beside it rather than the only implementation. `UnratedRatingProvider` is
still a placeholder and moved across unchanged — A64-012.6 excludes rating
calculation.
"""

from app.modules.profiles.infrastructure.statistics_providers import (
    DatabaseStatisticsProvider,
    NoMatchesStatisticsProvider,
    UnratedRatingProvider,
)

__all__ = [
    "DatabaseStatisticsProvider",
    "NoMatchesStatisticsProvider",
    "UnratedRatingProvider",
]
