"""The ports `game`'s use cases declare — AD-06 puts them in the layer that
*needs* them, so a service depends on a contract and never on
`SqlAlchemyMatchRecordRepository`.

Two protocols, split by **capability** rather than by aggregate — the
argument every port pair on this platform makes:

    MatchRecordRepository  read and write one match
    MatchRetentionStore    delete the ones that never became games

The second exists because the first must not be able to reach a `DELETE`:
`game.match` is the permanent competitive record A-4 is about, and a bug in
the acceptance sweep that deleted rather than expired would be
unrecoverable. See `MatchRetentionStore`.

The line these two share, and that separates both from the three protocols
published in `game.public`: those state what *other modules* may ask, these
state what this module's own services need from a table. A consumer holding
`PairingReconciliationReader` can learn whether a ticket produced a match;
only something holding one of these can change anything.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.modules.engine import EngineVersion, PlayerSide, Position
from app.modules.game.domain.match_record import MatchRecord
from app.modules.game.domain.move_log import MoveRecord
from app.modules.game.domain.variants import MatchOrigin
from app.modules.game.public.history import (
    HistoryCursor,
    MatchHistoryEntry,
    MatchHistoryPage,
)


class MatchRecordRepository(Protocol):
    """Storage for `MatchRecord` — one repository per aggregate root.

    Every method is designed for **more than one worker**, because the
    deployment AD-02 describes is several processes against one table.
    `create` and `claim_overdue` are the two where that is not free; the
    rest are keyed on a primary key or a partial index and are safe by
    construction.
    """

    async def create(self, record: MatchRecord) -> tuple[MatchRecord, bool]:
        """Writes a new match, or returns the one that already exists for
        its `pairing_id`.

        Returns the stored record and whether **this call** created it —
        the observable half of the idempotency contract
        `game.public.MatchCreationUseCase` states.

        **Idempotency is the unique index, not a check.** A64-015.4 §3
        forbids check-then-insert: two workers retrying one pairing would
        both read no row and both insert. This inserts, lets
        `uq_match__pairing_id` refuse the loser, and re-reads by
        `pairing_id` — so the race resolves in the database, deterministically,
        and both callers come away with the same `match_id`.

        Flushes, never commits (repositories.md §5.1): the caller's unit of
        work spans the match and the `match.created` outbox row, because an
        event for a match that rolled back is a notification about a game
        nobody has.
        """
        ...

    async def by_pairing(self, pairing_id: UUID) -> MatchRecord | None:
        """The match created for a pairing, or `None`.

        Served by `uq_match__pairing_id`, which is why the constraint that
        enforces idempotency is also the index that answers the question
        idempotency is about.
        """
        ...

    async def by_id(self, match_id: UUID) -> MatchRecord | None:
        """The match, read without locking it — A64-016.2.

        Deliberately beside `lock` rather than instead of it, because the
        two callers want opposite things. `accept` must lock: two players
        answering at the same instant have nowhere else to go, so the
        second must wait and see what the first wrote. A room join is not
        a contest — nothing about admitting a socket depends on what
        another socket is doing — so locking there would serialise every
        join on one match to protect an invariant that does not exist.

        `None` for a match that does not exist.
        """
        ...

    async def lock(self, match_id: UUID) -> MatchRecord | None:
        """The match, with its row locked for the caller's transaction.

        `SELECT ... FOR UPDATE` — **not** `SKIP LOCKED**, and the
        difference from every other claim on this platform is the point. A
        sweeper skipping a locked row moves on to another one; two players
        accepting the same match at the same instant have nowhere else to
        go, so the second must *wait* and then see what the first wrote.
        Skipping would report "no such match" to a player who has one.

        `None` for a match that does not exist. The lock is released with
        the transaction, as every lock on this platform is.
        """
        ...

    async def pending_for(self, player_id: UUID) -> MatchRecord | None:
        """The match this player has been offered and not yet answered.

        At most one — see `MatchAcceptanceUseCase.pending_match` on why
        that needs no rule of its own. Served by the two partial indexes on
        `(player_id) WHERE status = 'pending_acceptance'`, so the read is
        bounded by concurrency rather than by history.
        """
        ...

    async def advance(self, record: MatchRecord, *, expected_ply: int) -> bool:
        """Writes a match one move further on, only if its ply is still
        `expected_ply` — A64-016.4 §3, §8.

        The compare-and-set that keeps "only one move may win for one match
        version" true even where the row lock is not held. `lock` is what
        actually serialises two players; this is the guarantee that
        survives a future path forgetting it.

        Writes the settlement columns in the same statement, because a move
        that ends a game advances the ply *and* completes the match — two
        writes would be a window in which a match is one move on and has no
        result.

        Returns `False` for a losing write, which the caller turns into
        `StaleMatchState`.
        """
        ...

    async def settle(self, record: MatchRecord) -> bool:
        """Writes an accepted, declined or expired match, **only if the row
        still says what it said when it was read**.

        Returns whether it applied. A compare-and-set on the fields that
        may have moved — `status` and the two `accepted_at` instants — for
        the reason every write on `queue_ticket` carries one: a blind
        `UPDATE` would let a decline overwrite an activation, or a
        reconciler's expiry overwrite an acceptance that arrived a
        millisecond earlier.

        Redundant while the caller holds `lock`'s row lock, and checked
        anyway: the day the two are separated, a silent last-write-wins is
        a match two people accepted being recorded as expired.
        """
        ...

    async def claim_overdue(self, *, now: datetime, limit: int) -> Sequence[MatchRecord]:
        """Takes up to `limit` pending matches whose window has closed,
        oldest deadline first, for this worker.

        **Safe under concurrency**, by the outbox's mechanism rather than a
        new one: `SELECT ... FOR UPDATE SKIP LOCKED`. Two reconcilers
        calling this simultaneously receive disjoint sets.

        The rows stay pending — claiming is not a transition. `settle`
        above is what resolves them, in the caller's transaction, so a
        worker that dies between the two leaves matches the next tick
        simply claims again.
        """
        ...

    async def settlements_for(self, ticket_ids: Sequence[UUID]) -> Sequence[MatchRecord]:
        """Every match created from one of these queue tickets.

        One statement for the batch. Serves
        `game.public.PairingReconciliationReader`, whose contract explains
        why the key is the ticket rather than the pairing.
        """
        ...

    async def by_origin_refs(
        self, origin_refs: Sequence[UUID], *, origin: MatchOrigin
    ) -> Sequence[MatchRecord]:
        """Every match one context created for these references — R-25.

        One statement for the batch, served by `ix_match__origin_ref`, which
        is partial on `origin_ref IS NOT NULL` and therefore covers exactly
        the matches somebody else asked for. Serves
        `game.public.OriginMatchReader`, whose contract explains why the key
        is the reference rather than the match id.
        """
        ...

    async def latest_opponent_among(self, player_ids: Sequence[UUID]) -> Mapping[UUID, UUID]:
        """For each of these players that has one, the opponent of their
        **most recent settled match**.

        A projection rather than a sequence of records, and the shape is
        load-bearing: "whose latest match is this" is a question a list of
        matches cannot answer without re-deriving it, and re-deriving it
        gets the tie wrong — a match that is player A's latest is not
        necessarily player B's.

        One statement, `DISTINCT ON` — see
        `SqlAlchemyMatchRecordRepository.latest_opponent_among`. Serves
        `game.public.RecentOpponentReader`, whose contract records what
        "recent" means today and what it will mean once a match can
        complete.
        """
        ...


__all__ = ["MatchRecordRepository", "MatchRetentionStore"]


class MatchRetentionStore(Protocol):
    """Deleting the matches that never became games — A64-015.5 §8.

    A **second port rather than two methods on `MatchRecordRepository`**,
    and the split is the one `OutboxRetentionStore` makes against
    `OutboxRepository`: the acceptance service can create, lock, settle and
    expire a match; it must not be able to *delete* one. A bug in the
    expiry sweep that reached a `DELETE` would destroy the permanent
    competitive record A-4 is about.

    Satisfied by an adapter constructed only by the retention job's own
    session — nothing on the HTTP path holds one.
    """

    async def prune_abandoned(self, *, before: datetime, batch_size: int) -> int:
        """Deletes up to `batch_size` cancelled or expired matches settled
        before `before`. Returns how many rows went.

        See `game.public.AbandonedMatchRetention` for the contract and for
        why an `active` match is unreachable from this statement.
        """
        ...

    async def unsettled_before(self, instant: datetime) -> int:
        """How many matches older than `instant` are still
        `pending_acceptance`.

        Not used to decide anything. It is the number that says *why* the
        floor did not move, and here it is a genuine alarm: a pending match
        older than the whole retention horizon means the acceptance-expiry
        sweep has stopped, and two players are holding an offer nothing will
        ever resolve. `PruneResult` carries `retained_unpublished` for
        exactly the same reason.
        """
        ...


@dataclass(frozen=True, slots=True)
class LoggedMove:
    """One move, as the durable log stores it — A64-016.4 §1.

    `MoveRecord` plus the two facts it deliberately does not carry: who
    moved, and which engine build applied it.

    `MoveRecord` is the *aggregate's* entry and lives inside a `Match` that
    already knows both — the side from parity and the version from the
    match. The **row** carries them explicitly for §8.4's own reason: a
    takeback would break the parity assumption, and an engine version bump
    mid-match is exactly when a replay needs to know which build produced a
    ply.
    """

    record: MoveRecord
    seat: PlayerSide
    engine_version: EngineVersion
    created_at: datetime

    received_at: datetime
    """When the **gateway** saw the frame — MT-9, A64-016.5 §2.

    The temporal authority for the flag race, and deliberately not
    `created_at`: that is when the row was appended, which is later by the
    width of every queue, lock and validation between the two. Collapsing
    them would make the platform's own processing delay part of the flag
    decision, which outcome tenet T-2 forbids.
    """


class MoveLogRepository(Protocol):
    """The append-only move log — MT-5, A64-016.4 §1.

    Two operations and no third: no `update`, no `delete`, no `replace`. A
    repository that could amend an entry would make "append-only" a
    convention rather than a property.
    """

    async def append(self, match_id: UUID, entry: LoggedMove) -> None:
        """Appends one move. Flushes, never commits.

        **Does not check for a duplicate first.** §2 forbids relying on
        in-memory deduplication, and a check-then-insert is exactly that:
        two concurrent submissions for the same ply both pass the check and
        the database refuses one of them anyway. `uq_move__ply` is the
        mechanism; the caller catches its refusal and answers
        `StaleMatchState`.
        """
        ...

    async def for_replay(self, match_id: UUID) -> Sequence[MoveRecord]:
        """Every move of one match, in order.

        **Unbounded**, uniquely among this platform's list reads. A replay
        that saw a page would reconstruct a different game, silently, and
        there is no correct page size for "all of it". The bound is the
        game.
        """
        ...


@dataclass(frozen=True, slots=True)
class LiveMatchState:
    """A match as it stands mid-game — AD-18's "live position". A64-016.3.

    Two fields, and the pairing is the whole design: a position and the ply
    it was reached at. The ply is the **version** the compare-and-set is
    made against, so it is not bookkeeping beside the position — it is what
    makes concurrent writes safe.
    """

    position: Position
    ply: int
    """How many moves have been played. `0` for a match nobody has moved
    in yet, which is also the value a lazily-seeded state carries."""


class LiveMatchStore(Protocol):
    """Where a match in progress lives — architecture.md AD-18.

    "Live position lives in Redis. Moves are appended durably to
    PostgreSQL." This is the first half. The second half — the durable move
    log — is **not built**, and that gap is stated here rather than left to
    be discovered: until it exists, a Redis primary failure loses an
    in-flight game with no replay path, which is the mitigation AD-19
    depends on.

    That is acceptable only because no rated game is played yet. See
    `docs/01-architecture/websocket.md` §16.

    ## Why compare-and-set rather than a lock

    §6 forbids a process-local lock and asks for "optimistic versioning,
    row locking, Redis atomic operations, or the existing authoritative-
    state mechanism". A lock would also serialise the two players of a
    match through one process, which is exactly the coupling a horizontally
    scaled gateway must not have.

    `advance` is conditional on the ply that was read. Two moves submitted
    against the same state produce one write and one refusal, decided
    inside Redis rather than by a check either caller could pass.
    """

    async def load(self, match_id: UUID) -> LiveMatchState | None:
        """The current live state, or `None` if there is none.

        `None` means "no move has been played and nothing has seeded it" —
        an ordinary answer for a freshly activated match, and the caller
        seeds from the variant's opening position rather than treating it
        as an error.
        """
        ...

    async def advance(
        self, match_id: UUID, *, state: LiveMatchState, expected_ply: int, ttl_seconds: int
    ) -> bool:
        """Writes `state` **only if** the stored ply is still `expected_ply`.

        Returns `False` when it is not — the optimistic-concurrency
        failure, which the caller turns into `StaleMatchState`.

        `expected_ply` of `0` also accepts an **absent** key, which is what
        makes lazy seeding safe: two nodes both finding no state and both
        applying the first move resolve to one winner, because the second's
        condition no longer holds once the first has written.

        The TTL is an argument rather than a policy this holds, so "how
        long may a game sit idle before its live state is dropped" is
        visible at the call site and configurable — see
        `GameSettings.live_state_ttl_seconds`.
        """
        ...


@dataclass(frozen=True, slots=True)
class ClaimedDeadline:
    """One expired clock deadline, claimed by exactly one worker.

    A **token**, not a fact: it names the position the deadline was written
    for, and the worker checks that against the authoritative match row
    before adjudicating. A deadline whose ply has moved on was superseded by
    a move, and adjudicating it would flag a player who had already moved.
    """

    match_id: UUID
    ply_number: int
    side: PlayerSide


class ClockDeadlineStore(Protocol):
    """Where AD-21's deadlines live — A64-016.5 §5.

    "The clock is adjudicated by a worker against Redis, not by in-process
    timers." An `asyncio` timer per match lives on one node; a deadline in a
    sorted set is owned by no node and adjudicated by whichever worker is
    healthy.

    **Never raises on a write.** A deadline that could not be written is a
    match that will not flag — bad, and strictly better than a move that
    fails after it was applied, which is what propagating would produce.
    """

    async def schedule(
        self, match_id: UUID, *, ply_number: int, side: PlayerSide, deadline: datetime
    ) -> None:
        """Writes this match's deadline, **replacing** whatever it had.

        Replace rather than add, so a match cannot hold two live deadlines —
        which would flag it twice, once for a position it had left.
        """
        ...

    async def cancel(self, match_id: UUID) -> None:
        """Removes this match's deadline. Idempotent — a finished or untimed
        match has none, and removing nothing is not a failure."""
        ...

    async def claim_expired(self, *, now: datetime, limit: int) -> Sequence[ClaimedDeadline]:
        """Up to `limit` passed deadlines, claimed **exclusively**.

        Two workers must receive disjoint sets, which is a property of the
        store rather than of the worker — see `RedisClockDeadlineStore`.
        Bounded, like every read on this platform.
        """
        ...


class MatchHistoryStore(Protocol):
    """Finished matches, read for a history page — SPEC-REPLAY §1.

    Its own port rather than a method on `MatchRecordRepository`, and the
    split is the *question* rather than the table: that repository loads and
    mutates an aggregate under a lock, and this one answers a paginated
    read over rows nobody will write again. A reader holding the write
    repository could `lock` a finished match, which nothing should.
    """

    async def finished_for(
        self, player_id: UUID, *, after: HistoryCursor | None, limit: int
    ) -> MatchHistoryPage:
        """One page of this player's finished matches, newest first.

        Keyset by `(created_at, match_id)`, so the order is total and a page
        cannot skip or repeat a match when a game finishes between reads.
        """
        ...

    async def finished_entry(self, match_id: UUID) -> MatchHistoryEntry | None:
        """One finished match's stored facts, or `None`.

        The read a visibility check makes before deciding (SPEC-REPLAY §3):
        it says who played and whether the match was rated, and touches no
        move log.
        """
        ...
