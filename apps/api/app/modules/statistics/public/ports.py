"""The port other modules may depend on — BE-03's published surface.

One port, one method. `profiles` (A64-012.6) is the first consumer and
needs exactly this: a player's record by id.
"""

from typing import Protocol
from uuid import UUID

from app.modules.statistics.domain.statistics import PlayerStatistics


class StatisticsReader(Protocol):
    """Reads one player's aggregate competitive record.

    **Read-only by construction.** There is no way here to change a count,
    which is what makes it safe to hand to a module serving anonymous
    traffic. A projection is written by folding `match.completed` events in
    (domain-model.md §227) and never by an API caller, so a writer on this
    surface would not merely be unused — it would be wrong.

    Takes a `UUID` — DM-06's `player_id`, the only reference that crosses a
    context boundary. Deliberately not a profile or a username: a
    statistics context has no business receiving a display name, and a port
    that accepted one would make the consumer the reason it could read it.

    **Applies no privacy.** `show_statistics` is a `users` flag, applied by
    the consumer that composes a public profile — see
    `profiles.application.services.ProfileService`, which declines to call
    this at all for a player who has opted out. A privacy check here would
    be a second copy of a rule that already has an owner, and the two would
    eventually disagree.
    """

    async def for_player(self, player_id: UUID) -> PlayerStatistics:
        """This player's record. Never `None`, never raising.

        Returns `NO_MATCHES_PLAYED` — every count zero, both ratings at the
        starting value — for a player who has finished no matches, which is
        every account on the day it registers. Absence is the ordinary
        outcome for a projection, not a failure, and making it exceptional
        would put a `try` around the most common case.

        The same answer is returned for a `player_id` that belongs to no
        account at all. That is correct for this context, which does not
        own the player directory and has no way to tell the two apart — and
        it usefully denies an existence oracle to anyone probing ids.
        """
        ...
