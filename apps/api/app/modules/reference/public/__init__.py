"""`reference`'s published surface — the only way into this module.

    TimeControlId           which controls exist, as a closed enum
    TimeControl             one catalogue entry, with its presentation
    TimeControlSnapshot     the durable subset a record copies
    TimeControlCatalogue    the read port
    UnsupportedTimeControl  the one refusal

Everything else is private and is held so by the `import-linter` contract
`reference-internals-are-private`: no module may reach `reference.domain` or
`reference.infrastructure`.

## The direction of the one dependency this module has

`reference -> rating.public`, for `SpeedClass`. That looks inverted — speed
class reads like reference data, and database.md §6.2 puts it on
`reference.time_control` — and it is deliberate.

`SpeedClass` is already a shipped, persisted native enum in three of
`rating`'s columns and in `game`'s seat snapshots, and it is *the rating
key's second component* (SPEC-RATING §7.1). Moving it here would be a
migration of the one dataset A-4 promises never to corrupt, bought to make
an import arrow point the way a document draws it. So this module adopts
`rating`'s vocabulary rather than minting a second one — which is the rule
CLAUDE.md §3.4 states, and the same call `QueuePool` made when it refused to
define "blitz" locally.
"""

from app.modules.reference.domain.exceptions import UnsupportedTimeControl
from app.modules.reference.domain.time_control import (
    TimeControl,
    TimeControlId,
    TimeControlSnapshot,
)
from app.modules.reference.public.time_controls import TimeControlCatalogue

__all__ = [
    "TimeControl",
    "TimeControlCatalogue",
    "TimeControlId",
    "TimeControlSnapshot",
    "UnsupportedTimeControl",
]
