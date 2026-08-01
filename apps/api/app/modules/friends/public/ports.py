"""The port other modules may depend on — BE-03's published surface.

One port, one method. `profiles` (A64-013.3) is the first consumer and needs
exactly this: which of a page of players the viewer is friends with.
"""

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID


class FriendshipReader(Protocol):
    """Answers "is this viewer a friend of these players".

    **Read-only by construction**, and batch-only by construction. There is
    no way here to create or end a friendship, which is what makes it safe
    to hand to the module that composes every public profile on the
    platform; and there is no single-player form, which is what stops that
    module from calling it in a loop.

    Takes `UUID`s — DM-06's `player_id`, the only reference that crosses a
    context boundary. Deliberately not profiles or usernames: a social graph
    has no business receiving a display name.

    **Applies no privacy.** Whether two players are friends is a fact about
    the graph; whether a *field* may be shown to a friend is
    `users.domain.privacy`'s, applied by `PublicProfileComposer`. A port
    that answered both would be a second place a visibility rule lives.
    """

    async def friend_ids_among(self, player_id: UUID, others: Sequence[UUID]) -> set[UUID]:
        """The subset of `others` currently friends with `player_id`.

        **One query for the whole page.** This runs on the composition path,
        so a per-player form would multiply every profile render on the
        platform — the N+1 pattern CLAUDE.md §10.4 names as the single most
        common cause of slow endpoints.

        Returns the *other* players' ids rather than friendship rows,
        because that is what a caller does with the answer: index it.

        Never raises, and an empty `others` returns an empty set without
        touching the database. A friendship that ended is not a friendship:
        only live rows count.
        """
        ...
