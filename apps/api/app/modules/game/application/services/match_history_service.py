"""`GameMatchHistory` and `GameMatchReplay` — SPEC-REPLAY §1, §4.

The two published reads, implemented over the repositories `game` already
has. Neither writes, and neither is on a live path.

## The history read is stored facts only

Every field on `MatchHistoryEntry` comes off the match row. Nothing is
replayed, nothing is derived through the engine — which is exactly what
makes §4's split possible: a match played under an unsupported engine
version keeps its history and loses only its replay, and that is sayable
only because the history read does not touch the rules.

## The replay read refuses rather than approximates

`ReplayEngine` already raises for an unsupported version (A64-014.8), and
that refusal is translated here into the published `UnsupportedEngineVersion`
so a consumer branches on a `game.public` type rather than on a domain one.

**No attempt is made** — §4's wording, and it holds for the reconstruction:
`ReplayEngine` refuses before touching a move.

What this class does **not** avoid is loading the log: `replay_data` reads
every ply before the version is examined, so an unsupported match here costs
a full log read for an answer that was knowable from one row. A64-018.4
audited it and put the cheap refusal in `VisibleMatchReplay`, which already
holds the match entry — so the API path never pays it. This reader, used
directly, still does.

## Why the plies carry positions

A client stepping through a game renders a board per ply and has no engine
of its own. Replaying once here and publishing the boards means the
reconstruction happens where the rules live; publishing only the moves would
push it to every client, and the first one to disagree with the server would
be a disputed game nobody could settle.
"""

import logging
from collections.abc import Sequence
from uuid import UUID

from app.modules.engine import Position
from app.modules.game.application.ports import MatchHistoryStore
from app.modules.game.application.services.match_replay_service import PersistedMatchReplay
from app.modules.game.domain.exceptions import (
    UnsupportedEngineVersion as UnsupportedEngineVersionInDomain,
)
from app.modules.game.domain.replay import ReplayEngine
from app.modules.game.domain.variants import ProductVariant
from app.modules.game.public.history import (
    HistoryCursor,
    MatchHistoryEntry,
    MatchHistoryPage,
    MatchReplay,
    ReplayPly,
    UnsupportedEngineVersion,
)
from app.modules.game.public.snapshots import PlacedPiece

logger = logging.getLogger(__name__)


class GameMatchHistory:
    """`MatchHistoryReader` over the match relation."""

    def __init__(self, matches: MatchHistoryStore) -> None:
        self._matches = matches

    async def history_for(
        self, player_id: UUID, *, after: HistoryCursor | None = None, limit: int = 20
    ) -> MatchHistoryPage:
        page: MatchHistoryPage = await self._matches.finished_for(
            player_id, after=after, limit=limit
        )
        return page

    async def entry_for(self, match_id: UUID) -> MatchHistoryEntry | None:
        entry: MatchHistoryEntry | None = await self._matches.finished_entry(match_id)
        return entry


class GameMatchReplay:
    """`MatchReplayReader` — the durable log, played back through the rules."""

    def __init__(self, *, replays: PersistedMatchReplay, engine: ReplayEngine) -> None:
        self._replays = replays
        self._engine = engine

    async def replay_of(self, match_id: UUID) -> MatchReplay | None:
        """The whole game, or `None`. Raises for an unsupported version.

        One log read and one engine application per ply — linear in the
        game and bounded above by the draw rules, which terminate it.
        """
        data = await self._replays.replay_data(match_id)
        if data is None:
            return None

        try:
            match = self._engine.replay(data)
        except UnsupportedEngineVersionInDomain as refused:
            # Translated at the boundary so a consumer branches on a
            # `game.public` type — importing the domain error is what the
            # privacy contract forbids and what §6 is about.
            logger.info(
                "replay_refused_unsupported_engine",
                extra={"match_id": str(match_id), "engine_version": data.engine_version.number},
            )
            raise UnsupportedEngineVersion(str(refused)) from refused

        return MatchReplay(
            match_id=match_id,
            variant=ProductVariant(data.variant.value),
            engine_version=data.engine_version.number,
            opening=_placement(data.opening_position),
            plies=tuple(self._plies(data, match_id)),
            outcome=match.result.outcome if match.result else None,
            termination_reason=match.result.reason if match.result else None,
            winner=match.result.winner if match.result else None,
        )

    def _plies(self, data, match_id: UUID) -> Sequence[ReplayPly]:  # type: ignore[no-untyped-def]
        """Every ply with the board it produced.

        Replayed **again**, one ply at a time, rather than instrumenting
        `ReplayEngine.replay` to emit intermediate positions. That would
        widen the rules engine's contract for a presentation need, and it is
        the one class on this platform whose contract must not widen for a
        reader — the same argument `ReplayData` makes about storing derived
        state.

        The cost is one extra pass over a game that is tens of plies.
        """
        plies: list[ReplayPly] = []

        for index, record in enumerate(data.records, start=1):
            stepping = self._engine.replay(_prefix(data, index))
            plies.append(
                ReplayPly(
                    ply_number=record.ply_number,
                    side=record.seat,
                    path=tuple(str(square) for square in record.move.path),
                    captured=tuple(str(square) for square in record.move.captured),
                    promoted_to=(
                        record.move.promoted_to.value if record.move.promoted_to else None
                    ),
                    pieces=_placement(stepping.position),
                    fingerprint=stepping.position.fingerprint,
                    think_time_ms=record.think_time_ms,
                    remaining_clock_ms=record.remaining_clock_ms,
                )
            )

        return tuple(plies)


def _prefix(data, plies: int):  # type: ignore[no-untyped-def]
    """`data` truncated to its first `plies` moves.

    The expected result is dropped: a prefix of a finished game has not
    ended, so checking it against the final result would fail on every ply
    but the last.
    """
    from dataclasses import replace

    return replace(data, records=tuple(data.records[:plies]), expected_result=None)


def _placement(position: Position) -> Sequence[PlacedPiece]:
    """A board as the flat list `MatchSnapshot` already publishes.

    The same projection, deliberately: a client that renders a live game and
    one that renders a replay use one format, so there is one parser to get
    right rather than two that can disagree.
    """
    return tuple(
        PlacedPiece(square=str(square), side=piece.side.value, rank=piece.rank.value)
        for square, piece in sorted(
            position.board.occupied_squares.items(), key=lambda entry: entry[0]
        )
    )


__all__ = ["GameMatchHistory", "GameMatchReplay"]
