"""Finished games, published — SPEC-REPLAY §1, §4, §6.

Two reads and one refusal:

    MatchHistoryReader   a player's finished matches, newest first
    MatchReplayReader    one match, ply by ply
    UnsupportedEngineVersion  the replay this platform will not approximate

## Why `replay` gets a published read rather than `ReplayEngine`

SPEC-REPLAY §6, and the boundary the gateway already keeps. `ReplayEngine`
plays every ply through the same validator, applier, terminal evaluator and
draw rules a live game uses — it *is* the rules, and R-2 makes the rules
`game`'s. A consumer holding one would be a second module able to decide how
a game ended.

What crosses instead is the outcome: positions as the same flat placement
list `MatchSnapshot` already publishes, and the moves as primitives. A
consumer can render a game and cannot adjudicate one.

## Why history and replay are separate reads

They answer different questions at different costs. A history page is one
indexed scan of match rows; a replay is a full log read plus one engine
application per ply. Folding them together would make listing thirty
matches replay thirty games.

It is also what makes §4's split expressible: an unsupported engine version
keeps its **history** and loses its **replay**, which is only sayable if the
two are separate surfaces.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, Protocol
from uuid import UUID

from app.core.error_codes import ErrorCode
from app.core.exceptions import ConflictError
from app.modules.engine import PlayerSide
from app.modules.game.domain.match_record import MatchRecordStatus
from app.modules.game.domain.result import MatchOutcome, TerminationReason
from app.modules.game.domain.variants import ProductVariant
from app.modules.game.public.matches import MatchTimeControl
from app.modules.game.public.snapshots import PlacedPiece


class UnsupportedEngineVersion(ConflictError):
    """This match was played under rules this build cannot reproduce — §4.

    `SUPPORTED_ENGINE_VERSIONS` holds version 2 only, and A64-014.8's rule
    is that replay **refuses rather than approximates**: a game played under
    rules that have since been fixed must not be reconstructed under the new
    ones, because the reconstruction could end differently from the game
    that was actually rated and displayed.

    Raised by the *replay* read and never by the history read. Hiding the
    match would make a player's record incomplete over an engineering
    detail; replaying it anyway would make the archive disagree with
    history.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.UNSUPPORTED_ENGINE_VERSION


@dataclass(frozen=True, slots=True)
class MatchHistoryEntry:
    """One finished match, as a list renders it.

    Stored facts only — no position, no move count derived by replay.
    Everything here is read from the match row, which is why §4 can keep a
    match visible whose replay is refused: none of it depends on the engine.
    """

    match_id: UUID
    variant: ProductVariant
    rated: bool
    """Whether this match is publicly visible — SPEC-REPLAY §3. Carried
    rather than filtered away, so a participant reading their own history
    can see which of their games a stranger would find."""

    engine_version: int
    """The rules it was played under — AD-15. Published so a client can tell
    that a replay will be refused *before* asking for one."""

    light_player_id: UUID
    dark_player_id: UUID

    outcome: MatchOutcome | None
    termination_reason: TerminationReason | None
    winner: PlayerSide | None

    time_control: MatchTimeControl | None
    speed_class: str | None
    """How much time each side had, and which rating a result moved —
    A64-020.5F §14.

    Both were absent, and `speed_class` was worse than absent: the response
    schema declared it and the mapper hardcoded `None`, so every history row
    ever served claimed the match had no speed class. Read from the row now,
    where both have always been.

    `None` for an untimed match, which is a real state rather than a gap.
    """

    ply_number: int
    ended_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class HistoryCursor:
    """Where a history page ended.

    The two ordering values, not an offset: `OFFSET` re-scans and shifts
    when a match is inserted between reads, so a player paging back through
    their record could see a game twice or miss one. The same argument the
    leaderboard makes, over a relation that grows faster.
    """

    created_at: datetime
    match_id: UUID


@dataclass(frozen=True, slots=True)
class MatchHistoryPage:
    entries: Sequence[MatchHistoryEntry]
    next_cursor: HistoryCursor | None
    """`None` on the last page. Derived from whether a further row exists,
    so a page that is exactly `limit` long and also last does not send a
    reader back for an empty one."""


@dataclass(frozen=True, slots=True)
class ReplayPly:
    """One ply of a replay: what was played, and the board it produced.

    The position **after** the move, because that is what a viewer looks at
    — a client stepping forward renders `pieces` and needs no engine of its
    own. The starting position is `MatchReplay.opening`.
    """

    ply_number: int
    side: PlayerSide
    path: Sequence[str]
    captured: Sequence[str]
    promoted_to: str | None

    pieces: Sequence[PlacedPiece]
    """The board after this ply — the same flat placement list
    `MatchSnapshot` publishes, so a client has one board format rather than
    two."""

    fingerprint: str
    think_time_ms: int | None
    remaining_clock_ms: int | None


@dataclass(frozen=True, slots=True)
class ReplaySeat:
    """One seat of a finished match, as an archive renders it —
    A64-020.5E §5, §13.

    **The identifier and the rating, never a name.** Who a player id
    belongs to is `users`' to answer and is gated by a privacy policy this
    module has no business reproducing; what `game` owns is which seat they
    held and what they rated when the game began. The presentation layer
    composes the two — see `matchmaking`'s pending-match router, which
    makes the identical join for the identical reason.
    """

    player_id: UUID
    rating_value: float | None
    rating_deviation: float | None
    is_provisional: bool | None
    """The snapshot taken when the match was created (PR-3), or `None` for
    a match created before ratings were captured. Not recomputed: a replay
    shows what the game was played at, not what the player rates now."""


@dataclass(frozen=True, slots=True)
class MatchReplay:
    """A whole finished game, reconstructed.

    Produced only by replaying the durable log through the real rules, so
    the result it reports is the result the rules produce — never a stored
    copy that a rules fix could leave stale.

    ## Why the metadata is here and not fetched separately

    A64-020.5E. Everything below the plies is on the **match row**, which
    `PersistedMatchReplay` already reads to find the variant and the engine
    version — so carrying it costs nothing and its absence cost a great
    deal: there is no `GET /matches/{id}`, and a client wanting to know
    whether a replayed game was rated would have had to page through a
    player's whole history to find one row.
    """

    match_id: UUID
    variant: ProductVariant
    engine_version: int

    opening: Sequence[PlacedPiece]
    plies: Sequence[ReplayPly]

    outcome: MatchOutcome | None
    termination_reason: TerminationReason | None
    winner: PlayerSide | None

    status: MatchRecordStatus
    rated: bool
    speed_class: str | None
    time_control: MatchTimeControl | None
    light: ReplaySeat
    dark: ReplaySeat
    created_at: datetime
    ended_at: datetime | None


@dataclass(frozen=True, slots=True)
class CompletedMatchRecord:
    """One finished match, for a projection to fold — A64-020.5F §11.

    Deliberately not `MatchHistoryEntry`: that is what a *list* renders and
    carries a variant, an engine version, a ply count and a speed class, all
    of which a counter ignores. A narrow shape is what keeps a rebuild from
    quietly depending on a field it should not.

    `completed_at` is `ended_at`, which is when the contest ended (§6) — the
    same instant the live consumer reads from the event envelope, so the two
    paths order matches identically.
    """

    match_id: UUID
    light_player_id: UUID
    dark_player_id: UUID
    outcome: MatchOutcome
    winner: PlayerSide | None
    rated: bool
    termination_reason: TerminationReason
    completed_at: datetime


class CompletedMatchScanner(Protocol):
    """Every finished match, in a stable order — §11.

    Its own port rather than a method on `MatchHistoryReader`, because that
    one is *a player's* history and this is *all* of it: a reader that could
    do both would let a caller page through every game on the platform by
    passing a nullable player id, which is not a read any product surface
    should be able to make.

    Exists for the statistics backfill and for nothing else. That is stated
    rather than implied, because an "all matches" scan is the kind of port
    that acquires callers.
    """

    async def scan_completed(
        self, *, after: tuple[datetime, UUID] | None, limit: int
    ) -> Sequence[CompletedMatchRecord]:
        """One bounded page, ordered by `(ended_at, match_id)` ascending.

        **Keyset, never `OFFSET`** — §10. An offset scan re-reads every row
        it skips, so a backfill's last page costs the whole table; and a row
        inserted mid-run shifts every subsequent offset, which is how a
        resumable job silently skips a match.

        The order is the same total order the projection compares
        watermarks with, so a backfill folds matches in the order they
        actually happened and the streaks it produces are the streaks the
        live consumer would have produced.
        """
        ...


class MatchHistoryReader(Protocol):
    """A player's finished matches — read-only."""

    async def history_for(
        self, player_id: UUID, *, after: HistoryCursor | None = None, limit: int = 20
    ) -> MatchHistoryPage:
        """One page of this player's finished matches, newest first.

        **Every** match they played, rated and casual: this is the read a
        participant makes about themselves. The visibility rule in
        SPEC-REPLAY §3 is applied by the caller, which knows who is asking —
        a port that took a viewer would put a privacy decision inside
        `game`, which owns games rather than who may see them.
        """
        ...

    async def entry_for(self, match_id: UUID) -> MatchHistoryEntry | None:
        """One match's stored facts, or `None`.

        The read a visibility check needs before deciding: it says who
        played and whether the match was rated, without replaying anything.
        """
        ...


class MatchReplayReader(Protocol):
    """One finished match, played back — read-only."""

    async def replay_of(self, match_id: UUID) -> MatchReplay | None:
        """The whole game, or `None` if there is no such match.

        Raises `UnsupportedEngineVersion` when the match was played under
        rules this build cannot reproduce (§4). `None` and the exception are
        different answers deliberately: one means "no such game", the other
        means "this game exists and you may not see it reconstructed".
        """
        ...


__all__ = [
    "HistoryCursor",
    "MatchHistoryEntry",
    "MatchHistoryPage",
    "MatchHistoryReader",
    "MatchReplay",
    "CompletedMatchRecord",
    "CompletedMatchScanner",
    "MatchReplayReader",
    "ReplayPly",
    "ReplaySeat",
    "UnsupportedEngineVersion",
]
