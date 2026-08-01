"""The **only** package other modules may import from `statistics` — BE-03.

Everything else under `app.modules.statistics` is private. The rule exists
because Python's import system will happily let `profiles` reach into
`statistics.infrastructure.models` and query the table directly, and R-1
(architecture.md §7) forbids exactly that — but forbidding it in prose does
not stop it at the hundredth pull request. One named surface makes the rule
a single import-linter contract.

What is published, and why only this much:

  `PlayerStatistics`   the record itself — a frozen dataclass rather than a
                       Pydantic DTO, because `profiles.domain` holds it and
                       a domain layer must not import a framework
                       (architecture.md §8)
  `StatisticsReader`   read one player's record. One method
  `NO_MATCHES_PLAYED`  the empty record, so a consumer's fallback is
                       visibly the same value this module returns for a
                       player with no history rather than a second
                       definition of "no games"
  `DEFAULT_RATING`     what an unrated player's rating reads as

Deliberately **not** published: `StatisticsService` itself (a consumer gets
the one method it needs through the port, not the whole class), the
repository port (R-1: reach a module through its services, never its
storage), and the ORM model. There is also no writer of any kind — see
`application/ports.py` on why a projection's writer arrives with the thing
that produces the events, not before it.

## Why `profiles` still declares its own `StatisticsProvider`

`StatisticsReader` below and `profiles.application.ports.StatisticsProvider`
are structurally identical today: one method, same argument, same return.
That looks like duplication and is not, because they answer to different
owners.

`StatisticsReader` is *this* module's promise about what it will serve.
`StatisticsProvider` is `profiles`' statement of what it needs, and it is
what `NoMatchesStatisticsProvider` satisfies — a fallback that must keep
working when this module is switched off entirely, and therefore cannot be
defined in terms of this module's port. Collapsing them would make the
fallback depend on the thing it exists to replace.
"""

from app.modules.statistics.domain.statistics import (
    DEFAULT_RATING,
    NO_MATCHES_PLAYED,
    PlayerStatistics,
)
from app.modules.statistics.public.ports import StatisticsReader

__all__ = [
    "DEFAULT_RATING",
    "NO_MATCHES_PLAYED",
    "PlayerStatistics",
    "StatisticsReader",
]
