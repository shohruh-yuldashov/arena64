"""Who may see which finished match — SPEC-REPLAY §3.

The visibility rule lives here, in `game`'s application layer, and not in a
route. §5 of A64-018.3 requires it and the reason is the one this platform
keeps everywhere: a rule enforced at a route is enforced at *that* route,
and the second reader of match history would have to remember it.

## The rule

    rated    everyone
    casual   the two people who played it

**A casual match a stranger asks for is indistinguishable from one that does
not exist.** Not a `403`, not a different message, not a slower response —
the same `None` an unknown id produces. A `403` would confirm the match is
real, which is enough to enumerate match ids and learn who is playing
casually with whom.

That is the same rule `MatchRosterReader`, the room join and the spectator
policy already keep, and it is why this returns `None` rather than raising.

## Why the filter is applied after the page is read, not inside the query

A history page is "this player's matches", and the viewer is a *different*
question. Pushing both into one predicate would make the query say
`(light = :player OR dark = :player) AND (rated OR light = :viewer OR dark =
:viewer)` — correct, and a shape where a future third condition silently
changes which index serves it.

Reading the page and filtering costs at most `limit` rows of waste, and the
common case wastes none: a player reading their own history is a
participant in every row, and a stranger reading a public history sees a
population that is almost all rated.

**The cursor is unaffected**, which is the part that matters: it comes from
the last row the *query* returned, so paging stays total and cannot skip a
match even when the filter removes one.
"""

import logging
from uuid import UUID

from app.modules.game.public.history import (
    HistoryCursor,
    MatchHistoryEntry,
    MatchHistoryPage,
    MatchHistoryReader,
    MatchReplay,
    MatchReplayReader,
)

logger = logging.getLogger(__name__)


def is_visible_to(entry: MatchHistoryEntry, viewer_id: UUID) -> bool:
    """Whether `viewer_id` may see this match — SPEC-REPLAY §3.

    A free function rather than a method, because it is a pure predicate
    over two values and both readers below need it. There is deliberately
    **no** configurable dimension: v0.6.0 has no per-match privacy setting,
    so a viewer's own preferences cannot enter here and a future one has a
    single place to enter.
    """
    if entry.rated:
        return True
    return viewer_id in (entry.light_player_id, entry.dark_player_id)


class VisibleMatchHistory:
    """`MatchHistoryReader` narrowed to what one viewer may see.

    Wraps the published reader rather than replacing it: `game` answers
    "this player's matches" and this answers "…that you may see". Keeping
    them apart is what lets a future admin surface read the unfiltered one
    deliberately rather than by forgetting a parameter.
    """

    def __init__(self, history: MatchHistoryReader) -> None:
        self._history = history

    async def history_for(
        self,
        player_id: UUID,
        *,
        viewer_id: UUID,
        after: HistoryCursor | None = None,
        limit: int = 20,
    ) -> MatchHistoryPage:
        """One page of `player_id`'s matches, as `viewer_id` may see them.

        The cursor is the page's, untouched — see this module's docstring on
        why filtering after the read keeps paging total.
        """
        page = await self._history.history_for(player_id, after=after, limit=limit)
        return MatchHistoryPage(
            entries=[entry for entry in page.entries if is_visible_to(entry, viewer_id)],
            next_cursor=page.next_cursor,
        )


class VisibleMatchReplay:
    """`MatchReplayReader` gated by the same rule.

    Checks the match's **stored facts** before replaying anything: a casual
    match a stranger asks for costs one indexed row read, not a
    reconstruction that is then thrown away.
    """

    def __init__(self, *, history: MatchHistoryReader, replays: MatchReplayReader) -> None:
        self._history = history
        self._replays = replays

    async def replay_of(self, match_id: UUID, *, viewer_id: UUID) -> MatchReplay | None:
        """The game, or `None` if it does not exist **or** may not be seen.

        One answer for both, deliberately — see this module's docstring.
        `UnsupportedEngineVersion` still propagates, because refusing to
        reconstruct a match a viewer is entitled to see discloses nothing:
        they could already see it in their history.
        """
        entry = await self._history.entry_for(match_id)
        if entry is None or not is_visible_to(entry, viewer_id):
            return None

        return await self._replays.replay_of(match_id)


__all__ = ["VisibleMatchHistory", "VisibleMatchReplay", "is_visible_to"]
