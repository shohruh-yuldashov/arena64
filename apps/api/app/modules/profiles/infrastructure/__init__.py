"""Adapters satisfying the ports `profiles` declares in `application/`.

One file per port, since A64-012.8:

    rating_providers.py       `RatingProvider` — one placeholder
    statistics_providers.py   `StatisticsProvider` — the real adapter and
                              its kill-switch fallback

They were one file between A64-012.6 and A64-012.8. That file had been
`unrated_providers.py` and was renamed when the statistics placeholder
stopped being one, which left a rating adapter inside a module named for
statistics — a name that no longer described its contents. Splitting is a
move, not a change: both names are still exported from here, so nothing
that imports them moved.

**No presence adapter here.** `RedisPresenceProvider` and
`NoPresenceProvider` live in `users`, which owns presence
(domain-model.md §299); `profiles` consumes `users.public.PresenceProvider`
directly, exactly as it consumes `PublicProfileReader`. See
`application/ports.py` on why two of this module's four sources are another
module's published ports rather than locally declared ones.
"""

from app.modules.profiles.infrastructure.rating_providers import UnratedRatingProvider
from app.modules.profiles.infrastructure.statistics_providers import (
    DatabaseStatisticsProvider,
    NoMatchesStatisticsProvider,
)

__all__ = [
    "DatabaseStatisticsProvider",
    "NoMatchesStatisticsProvider",
    "UnratedRatingProvider",
]
