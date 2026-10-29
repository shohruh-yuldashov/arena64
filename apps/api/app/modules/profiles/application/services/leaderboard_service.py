"""The leaderboard as players see it — A64-033.

`rating` owns the ladder: one ordered relation per `(variant, speed_class)`
key, read live, with no projection and no cache (SPEC-LEADERBOARD §6). It
deliberately carries ids and numbers only, and leaves "who are these
people" to whoever renders it (§7). This is that renderer, in the module
whose one job is what a stranger may see about a player.

## Who is on a ladder

A row of `rating`'s ladder is shown when, and only when:

    the account has a public face    `users.find_public_profiles` returns it;
                                     deactivated and erased accounts are
                                     absent from that read by construction
    it is not restricted now         `admin.RestrictedAccounts`
    its rating is not frozen         decided inside `rating` — the ladder
                                     never returns a frozen row
    (country ladders) its *visible*  the country on the public profile,
    country is that country          which `users` has already set to
                                     `None` when its owner hides it

The last rule is the privacy one. A player who hides their country is on
the global ladder and on no country's, because placing them on one would
publish the very fact they hid. A player with no country is the same.

Provisional players are ranked and marked, as SPEC-LEADERBOARD §5 already
decided. Inactivity does not remove anybody: Glicko-2 widens an absent
player's deviation instead (SPEC-RATING §7.4).

## Why filtering happens here, and what it costs

BR-4 forbids joining `rating`'s table to `users`' or `admin`'s, so the
ladder is read in its own index order and each chunk is checked with two
batched reads — one query each, never one per row. A global ladder almost
never skips a row, so a page is three queries. A country ladder skips
everybody from elsewhere, so a request examines at most `SCAN_BUDGET`
ladder rows and, if it has not filled its page by then, returns what it
found with a cursor to continue from. Every request is bounded; none scans
the whole ladder.

## Ranks without counting

Ranks are positions among the rows that are shown, so they cannot be
`rating`'s count. They are carried instead: the cursor records how many
players the walk has ranked so far, and the next page continues from
there. That is exact for any walk from the top, which is the only way a
keyset ladder is entered, and it costs nothing.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from uuid import UUID

from app.core.clock import Clock
from app.modules.admin.public import RestrictedAccounts
from app.modules.rating.public import (
    LeaderboardCursor,
    LeaderboardEntry,
    LeaderboardReader,
    RatingKey,
)
from app.modules.users.public import PublicProfileReader, PublicUserProfile

#: Ladder rows read per statement. `rating`'s own page ceiling.
SCAN_CHUNK: Final = 200

#: Ladder rows one request may examine before it returns what it has.
#: Five chunks: a global page is one, and a country page that has not found
#: its players among a thousand rated ones hands the walk back to the
#: client rather than holding the request open.
SCAN_BUDGET: Final = 1_000

DEFAULT_PAGE_SIZE: Final = 50
MAX_PAGE_SIZE: Final = 100


@dataclass(frozen=True, slots=True)
class LadderPosition:
    """Where a walk down the ladder stopped, and how many it had ranked."""

    after: LeaderboardCursor
    ranked: int


@dataclass(frozen=True, slots=True)
class RankedPlayer:
    rank: int
    entry: LeaderboardEntry
    profile: PublicUserProfile


@dataclass(frozen=True, slots=True)
class LeaderboardView:
    players: Sequence[RankedPlayer]
    next: LadderPosition | None
    """`None` once the ladder is exhausted. A short or empty page with a
    position means the scan budget ran out first, not that nobody is left."""


class StandingStatus(StrEnum):
    RANKED = "ranked"
    """On this ladder, at `rank`."""

    BEYOND = "beyond"
    """On this ladder, further down than an exact rank is counted."""

    NOT_LISTED = "not_listed"
    """Rated, but not shown here: another country, a hidden one, none, or a
    restriction."""

    UNRATED = "unrated"
    """No rating in this speed class, or a frozen one."""


@dataclass(frozen=True, slots=True)
class OwnStanding:
    status: StandingStatus
    rank: int | None
    entry: LeaderboardEntry | None


def _cursor_of(entry: LeaderboardEntry) -> LeaderboardCursor:
    return LeaderboardCursor(
        rating=entry.rating, deviation=entry.deviation, player_id=entry.player_id
    )


class LeaderboardService:
    def __init__(
        self,
        *,
        ladder: LeaderboardReader,
        profiles: PublicProfileReader,
        restrictions: RestrictedAccounts,
        clock: Clock,
    ) -> None:
        self._ladder = ladder
        self._profiles = profiles
        self._restrictions = restrictions
        self._clock = clock

    async def page(
        self,
        key: RatingKey,
        *,
        country: str | None,
        after: LadderPosition | None,
        limit: int,
    ) -> LeaderboardView:
        """The next `limit` eligible players, best first."""
        wanted = max(1, min(limit, MAX_PAGE_SIZE))
        cursor = after.after if after is not None else None
        ranked = after.ranked if after is not None else 0
        found: list[RankedPlayer] = []
        scanned = 0

        while True:
            chunk = await self._ladder.page(key, after=cursor, limit=SCAN_CHUNK)
            shown = await self._shown(chunk.entries, country=country)

            for index, entry in enumerate(chunk.entries):
                cursor = _cursor_of(entry)
                profile = shown.get(entry.player_id)
                if profile is None:
                    continue
                ranked += 1
                found.append(RankedPlayer(rank=ranked, entry=entry, profile=profile))
                if len(found) == wanted:
                    more = index < len(chunk.entries) - 1 or chunk.next_cursor is not None
                    return LeaderboardView(
                        players=found,
                        next=LadderPosition(after=cursor, ranked=ranked) if more else None,
                    )

            scanned += len(chunk.entries)
            if chunk.next_cursor is None or cursor is None:
                return LeaderboardView(players=found, next=None)
            if scanned >= SCAN_BUDGET:
                return LeaderboardView(
                    players=found, next=LadderPosition(after=cursor, ranked=ranked)
                )

    async def standing(
        self, player_id: UUID, key: RatingKey, *, country: str | None
    ) -> OwnStanding:
        """The caller's own place on this ladder — never anybody else's.

        Exact when the caller is within `SCAN_BUDGET` ladder rows of the
        top, which is then a walk of at most that many rows. Further down,
        an exact rank would mean counting every eligible row above them on
        every request; the answer is `BEYOND` instead, with their rating,
        rather than an unbounded scan or a rank that is close but wrong.
        """
        neighbourhood = await self._ladder.around(player_id, key=key, span=1)
        if neighbourhood is None:
            return OwnStanding(status=StandingStatus.UNRATED, rank=None, entry=None)
        own = neighbourhood.entry

        if not (await self._shown([own], country=country)):
            return OwnStanding(status=StandingStatus.NOT_LISTED, rank=None, entry=own)

        # `rating`'s rank counts every unfrozen row above. Ours can only be
        # equal or better, and if theirs is past the budget, ours is not
        # worth an unbounded walk to pin down.
        if neighbourhood.rank > SCAN_BUDGET:
            return OwnStanding(status=StandingStatus.BEYOND, rank=None, entry=own)

        cursor: LeaderboardCursor | None = None
        ranked = 0
        scanned = 0
        while True:
            chunk = await self._ladder.page(key, after=cursor, limit=SCAN_CHUNK)
            shown = await self._shown(chunk.entries, country=country)
            for entry in chunk.entries:
                if entry.player_id in shown:
                    ranked += 1
                if entry.player_id == player_id:
                    return OwnStanding(status=StandingStatus.RANKED, rank=ranked, entry=own)
            scanned += len(chunk.entries)
            if chunk.next_cursor is None or not chunk.entries:
                # Moved off the ladder between the two reads.
                return OwnStanding(status=StandingStatus.UNRATED, rank=None, entry=None)
            if scanned >= SCAN_BUDGET:
                # Their rating fell between the two reads; the budget still
                # holds rather than following them down.
                return OwnStanding(status=StandingStatus.BEYOND, rank=None, entry=own)
            cursor = _cursor_of(chunk.entries[-1])

    async def _shown(
        self, entries: Sequence[LeaderboardEntry], *, country: str | None
    ) -> Mapping[UUID, PublicUserProfile]:
        """The entries that may appear, with their public faces. Two queries."""
        if not entries:
            return {}
        ids = [entry.player_id for entry in entries]
        profiles = await self._profiles.find_public_profiles(ids)
        restricted = await self._restrictions.restricted_among(ids, at=self._clock.now())
        return {
            player_id: profile
            for player_id, profile in profiles.items()
            if player_id not in restricted and (country is None or profile.country == country)
        }


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "LadderPosition",
    "LeaderboardService",
    "LeaderboardView",
    "MAX_PAGE_SIZE",
    "OwnStanding",
    "RankedPlayer",
    "SCAN_BUDGET",
    "StandingStatus",
]
