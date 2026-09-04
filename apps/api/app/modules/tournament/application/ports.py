"""What `tournament`'s use cases need from the world — AD-06.

Storage for the aggregate and everything beneath it, plus three narrow views
of other contexts: whether a player exists (`users`), what a field rates
(`rating`), and — since A64-019.5 — the ability to ask `game` for a match
and to learn what became of one. Every cross-context entry here is a
`Protocol` declared in *this* module and satisfied structurally by the other
one's published surface, which is what keeps `tournament` holding the
narrowest thing that answers its question.

## Why the capacity check is the repository's, not a service's

`register` takes a lock and counts inside one transaction, because
check-then-insert **outside** a lock is exactly what a concurrent field
overflows. A unique index cannot help here: it stops one player entering
twice, and says nothing about how many players there are.

So the port's contract is the transaction, not the query — and the service
above it cannot get it wrong by calling two methods in the wrong order.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import ClassVar, Protocol
from uuid import UUID

from app.core.error_codes import ErrorCode
from app.core.exceptions import ConflictError, DomainError, NotFoundError
from app.modules.game.public import ProductVariant
from app.modules.rating.public import RatingSnapshot, SpeedClass
from app.modules.tournament.application.read_models import (
    TournamentFilter,
    TournamentListCursor,
    TournamentPage,
)
from app.modules.tournament.domain.attempts import AdvancementReason, PairingAttempt
from app.modules.tournament.domain.bracket_plan import (
    BracketSlot,
    LocatedNode,
    PersistedSeed,
)
from app.modules.tournament.domain.registration import Registration
from app.modules.tournament.domain.rounds import TournamentRound
from app.modules.tournament.domain.seeding import PlannedPairing, Seed
from app.modules.tournament.domain.standings import Standing
from app.modules.tournament.domain.tournament import Tournament


class TournamentNotFound(NotFoundError):
    """No tournament with that id.

    A raise rather than a `None` return, because every caller's answer is
    the same and a use case that had to branch on absence would eventually
    forget.

    A `NotFoundError` since A64-019.8, so the HTTP boundary answers `404`
    without a route translating anything — §7's "use 404 for unknown
    resources", held by the type rather than by each handler.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.TOURNAMENT_NOT_FOUND


class AlreadyRegistered(ConflictError):
    """This player is already entered — the unique key refused the insert.

    Raised from the constraint rather than from a prior read, so two
    concurrent requests cannot both find nothing and both insert.

    A `409`: the request was well formed and the platform's *state*
    refused it, which is exactly what a client retrying a dropped response
    needs to be able to tell from a validation failure.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.ALREADY_REGISTERED


class TournamentIsFull(ConflictError):
    """Capacity is reached. Raised inside the lock — see this module's
    docstring.

    Its own code rather than a bare `conflict`, because a client's answer
    is specific and different from every other refusal here: offer another
    tournament.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.TOURNAMENT_FULL


class RegistrationNotOpen(ConflictError):
    """The tournament is not accepting entries.

    Covers "not yet open" and "already closed" with one type: a client's
    response to both is the same — hide the button — and distinguishing
    them would say more about a tournament's schedule than a refusal needs
    to.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.REGISTRATION_NOT_OPEN


class RegistrationDeadlinePassed(ConflictError):
    """The advertised deadline has passed — A64-019.8.

    Distinct from `RegistrationNotOpen`, and the distinction is the
    client's: a closed tournament is closed, and this one is still
    *marked* open only because the sweep has not run yet. Telling a player
    "registration closed at 14:00" is a different sentence from "this
    tournament is not accepting entries", and only this code can carry it.

    Checked on the **locked row** by the use case, so a player cannot beat
    the sweep by a few seconds — the deadline is the promise, not the
    worker's tick.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.REGISTRATION_DEADLINE_PASSED


class NotRegistered(NotFoundError):
    """This player has no live entry to withdraw.

    A `404` rather than a `409`: from the caller's side the resource
    `/registrations/me` does not exist, and that is the same answer whether
    they never entered or already withdrew. It is also what makes a
    repeated withdrawal safe to send — see the route.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.REGISTRATION_NOT_FOUND


class PlayerDirectory(Protocol):
    """Whether a player exists — §3.

    **One method**, and narrower than anything `users` publishes: a
    tournament asks whether an id names somebody and has no business
    reading their email, their profile or their credentials. Declaring the
    shape here rather than importing a wider port is what keeps that true —
    `users`' own reader satisfies this structurally, and the composition
    root is where they meet.
    """

    async def get_profile(self, user_id: UUID) -> object:
        """Raises `users.public.UserNotFound` when there is no such player.

        The return value is deliberately `object`: this module does not read
        it. What it needs is the *absence* — a raise — and a typed profile
        crossing here would be data `tournament` has no use for.
        """
        ...


class NotSeedable(ConflictError):
    """The tournament cannot be seeded yet — §2.

    Registration must be **closed** first: seeding an open tournament would
    build a bracket from a field that can still change, and the plan is
    immutable once written.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.INVALID_TOURNAMENT_STATE


class TournamentNotStartable(ConflictError):
    """The tournament cannot be started — §5.

    Distinct from `NotSeedable`, which is about building a bracket from a
    field that can still change. This is about the lifecycle: a draft, an
    open registration, a completed tournament and a cancelled one are all
    refusals, and none of them is a seeding problem.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.INVALID_TOURNAMENT_STATE


class PairingRepository(Protocol):
    """A round's slots. Written once, never rewritten — §10."""

    async def plan_for(self, tournament_id: UUID, *, round_number: int) -> list[PlannedPairing]:
        """The persisted plan, or an empty list if there is none.

        The idempotency read: a second seeding attempt finds this and
        returns it rather than producing a second bracket.
        """
        ...

    async def save_plan(
        self, tournament_id: UUID, pairings: list[PlannedPairing]
    ) -> list[PlannedPairing]:
        """Writes a round's slots. Raises `PlanAlreadyExists` on a collision.

        The primary key `(tournament, round, slot)` is the guard: two
        workers seeding at once cannot both insert, so the loser reads the
        winner's plan instead of overwriting it.
        """
        ...


class PlanAlreadyExists(DomainError):
    """A plan for this round is already persisted.

    Raised from the primary key rather than a prior read, so concurrent
    seeding is decided by the database. The caller treats it as a signal to
    re-read, not as a failure — the work was done by whoever won.
    """


class SeedRepository(Protocol):
    """Active entrants, and their assigned seed numbers."""

    async def active_entrants(self, tournament_id: UUID) -> list[UUID]:
        """Every player with a live registration — §2.

        Withdrawn entries are excluded, and the primary key already makes
        duplicates impossible, so this is the eligible field exactly.
        """
        ...

    async def assign(self, tournament_id: UUID, seeds: list[Seed]) -> None:
        """Persists seed numbers onto the registrations — §4."""
        ...

    async def seeds_for(self, tournament_id: UUID) -> list[PersistedSeed]:
        """The persisted seeding, for a retry to return unchanged — §4.

        `PersistedSeed`, not `Seed`: storage holds a number, not the rating
        that produced it, and a type that carried both would have to invent
        the half it does not have.
        """
        ...


class RatingSnapshots(Protocol):
    """Seeding ratings, in one batch — §3.

    `tournament`'s own narrow view of `rating.public`. A method per player
    would be the N+1 §3 forbids on a field of up to 128, and declaring the
    shape here keeps this module from importing a wider surface than it
    reads.
    """

    async def ratings_for(
        self,
        player_ids: Sequence[UUID],
        *,
        variant: ProductVariant,
        speed_class: SpeedClass,
    ) -> Mapping[UUID, RatingSnapshot]:
        """Every named player's rating in this tournament's key.

        **Complete**: `rating.public` fills an unrated player with the
        starting triple rather than omitting them, so a caller cannot
        silently drop an entrant by reading a key that is absent.
        """
        ...


class BracketRepository(Protocol):
    """The materialised tree. Written whole, advanced by compare-and-set."""

    async def exists(self, tournament_id: UUID) -> bool: ...

    async def materialise(
        self,
        tournament_id: UUID,
        nodes: list[BracketSlot],
        rounds: list[TournamentRound],
    ) -> None:
        """Writes every round and every node in one flush — §10.

        Raises `PlanAlreadyExists` on a collision, which is how two workers
        materialising at once resolve: one writes, the other re-reads.
        """
        ...

    async def nodes_for(self, tournament_id: UUID) -> list[BracketSlot]: ...

    async def locate(self, pairing_id: UUID) -> LocatedNode | None:
        """One node by its surrogate id, with the tournament it belongs to.

        The lookup a completion needs: `match.completed` carries
        `origin_ref` and nothing that says which tournament it is — which is
        the point of an opaque reference (§6c), and the reason this cannot
        be answered by `nodes_for`.
        """
        ...

    async def claim_winner(
        self,
        tournament_id: UUID,
        *,
        round_number: int,
        slot: int,
        winner_id: UUID,
        reason: AdvancementReason,
    ) -> bool:
        """Sets the winner **if there is none**. Returns whether this call did.

        The compare-and-set §8 requires: the guard is in the `WHERE`, so two
        workers cannot both write and the loser learns it lost rather than
        overwriting.

        The reason is written in the same statement, because the table's
        `ck_pairing__reason_iff_winner` refuses one without the other — and
        because a second write to set it would be a window in which the
        bracket says somebody advanced and cannot say why.
        """
        ...

    async def fill_seat(
        self,
        tournament_id: UUID,
        *,
        round_number: int,
        slot: int,
        player_id: UUID,
        seed: int | None,
        light: bool,
    ) -> None:
        """Puts an advancing winner into a parent seat, if it is empty."""
        ...


class AttemptAlreadyExists(DomainError):
    """This pairing already has an attempt with that number — §6c.

    Raised from `unique (pairing_id, attempt_number)` rather than from a
    prior read, so a redelivered `match.completed` cannot produce a second
    rematch. The caller treats it as a signal to re-read: the work was done
    by whoever won.
    """


class PairingAttemptRepository(Protocol):
    """The `game` matches played for a bracket node — §6c.

    A relation rather than a column, so the rules are the database's: one
    row per attempt, one match per attempt, and no third attempt.
    """

    async def record(self, attempt: PairingAttempt) -> PairingAttempt:
        """Writes a newly created attempt. Raises `AttemptAlreadyExists`.

        The insert **is** the idempotency guard, and it is why a caller
        creates the `game` match first: `game`'s own key returns the same
        match on a retry, so the loser here discards nothing.
        """
        ...

    async def complete(self, attempt: PairingAttempt) -> bool:
        """Records an attempt's result **if it has none**. Returns whether
        this call did it.

        The same compare-and-set shape as `BracketRepository.claim_winner`,
        and for the same reason: two deliveries of one completion must not
        both write, and the loser learns it lost rather than overwriting a
        result with an identical one it cannot distinguish from a different
        one.
        """
        ...

    async def by_match(self, match_id: UUID) -> PairingAttempt | None:
        """The attempt a `game` match was played for, or `None`.

        Keyed by match because that is what a completion carries. Served by
        `uq_pairing_attempt__match`.
        """
        ...

    async def for_pairings(self, pairing_ids: Sequence[UUID]) -> list[PairingAttempt]:
        """Every attempt of these nodes, in one statement.

        Batched for the reconciler's sake: it claims a bounded page of nodes
        per tick, and a query per node would make the recovery job the N+1
        it exists to avoid.
        """
        ...

    async def latest_for(self, pairing_id: UUID) -> PairingAttempt | None:
        """The highest-numbered attempt of one node, or `None`."""
        ...

    async def mark_present(self, match_id: UUID, player_id: UUID, *, at: datetime) -> bool:
        """Records that a player reached this match — §6e. Returns whether
        this call did it.

        One guarded statement and no read, because it runs on every gateway
        room join including for matches no tournament owns. Idempotent on a
        reconnect: the guard is `IS NULL`, so the instant recorded is the
        **first** arrival.
        """
        ...

    async def claim_no_show(self, *, now: datetime, limit: int) -> list[PairingAttempt]:
        """Up to `limit` unsettled attempts past their deadline — §6e.

        `FOR UPDATE SKIP LOCKED` and bounded, so two sweeps take disjoint
        sets and neither waits. Claiming is not deciding: the rows stay
        unsettled and the caller adjudicates in its own transaction.
        """
        ...


class StandingsAlreadyRecorded(DomainError):
    """This tournament's results are already materialised — §6f.

    Raised from the primary key rather than a prior read, so two workers
    completing one tournament cannot both write. The caller treats it as a
    signal to return the stored result: standings are immutable, so the
    winner's rows and the loser's would have been identical anyway.
    """


class StandingRepository(Protocol):
    """A completed tournament's final placement — §6f.

    Written **once**, read many times. There is no update method and there
    will not be one: a standing is a snapshot of a bracket that can no
    longer change, and a correction is the Administration epic's (OQ-1).
    """

    async def record(self, standings: Sequence[Standing]) -> None:
        """Materialises every standing in one flush.

        Raises `StandingsAlreadyRecorded` on a collision. All or nothing:
        a tournament with some of its results is one nothing can page over
        and nothing can repair, because the bracket it was derived from is
        already terminal.
        """
        ...

    async def standings_for(self, tournament_id: UUID) -> list[Standing]:
        """The published order — rank, then seed, then player id.

        Ordered by the index rather than by the caller, so the wire order
        and the stored order cannot drift.
        """
        ...

    async def exists(self, tournament_id: UUID) -> bool:
        """Whether this tournament's results have been materialised."""
        ...


class RoundRepository(Protocol):
    """A tournament's rounds and their lifecycle — §6b.

    Separate from `BracketRepository`, which writes them once at
    materialisation: this is what moves them afterwards, and a repository
    that could do both would let a publish be written as a materialise.
    """

    async def rounds_for(self, tournament_id: UUID) -> list[TournamentRound]: ...

    async def save(self, round_: TournamentRound) -> None:
        """Persists a round's transition. The state machine is the
        aggregate's — see `domain/rounds.py`."""
        ...


class TournamentRepository(Protocol):
    """The aggregate's storage."""

    async def create(self, tournament: Tournament) -> Tournament: ...

    async def by_id(self, tournament_id: UUID) -> Tournament | None: ...

    async def lock(self, tournament_id: UUID) -> Tournament | None:
        """`SELECT ... FOR UPDATE`. The capacity mechanism — §6.

        **No `SKIP LOCKED`**: two players registering at once are competing
        for the same slot, and skipping one would silently drop a
        registration rather than serialising it. The same argument
        `game`'s match lock makes.
        """
        ...

    async def save(self, tournament: Tournament) -> None:
        """Persists a lifecycle transition."""
        ...

    async def in_progress(self, *, limit: int) -> list[UUID]:
        """Up to `limit` tournaments currently being played — the
        reconciler's claim.

        `FOR UPDATE SKIP LOCKED`, like `close_overdue` and for the same
        reason: a tournament another worker is already reconciling is one
        this worker should leave alone rather than wait for. Bounded,
        because a sweep with no ceiling is an outage waiting for enough
        tournaments.
        """
        ...

    async def close_overdue(self, *, now: datetime) -> list[UUID]:
        """Closes every open tournament whose deadline has passed.

        Bounded, idempotent and safe under concurrent workers: it claims
        with `FOR UPDATE SKIP LOCKED` — the opposite choice from `lock`, and
        correct for the opposite reason. Here a row another worker is
        already closing is one this worker should leave alone, not wait for.
        """
        ...


class RegistrationRepository(Protocol):
    """Entries, and the count capacity is measured against."""

    async def add(self, registration: Registration, *, capacity: int) -> Registration:
        """Enters a player. Raises `AlreadyRegistered` or `TournamentIsFull`.

        Counts and inserts in **one** transaction, under the caller's lock
        on the tournament row. The capacity is passed rather than re-read,
        so the number enforced is the one the locked row carried.
        """
        ...

    async def withdraw(self, registration: Registration) -> None: ...

    async def find(self, tournament_id: UUID, player_id: UUID) -> Registration | None: ...

    async def count_active(self, tournament_id: UUID) -> int:
        """How many entries occupy a slot. For a read, not for the guard —
        the guard counts inside `add`'s transaction."""
        ...


class TournamentDirectory(Protocol):
    """The lobby — every public tournament, newest first. A64-020.0B.

    **Read-only, and narrower than the results adapter that satisfies it.**
    A route holding this can page the lobby and can do nothing else: it
    cannot read a bracket, cannot reach a registration, and has no method
    that writes. Declared here so `presentation` names an application port
    rather than a SQLAlchemy class — the layer rule (§3.1) applied to the
    one component this phase adds.

    Structural satisfaction, like every other port in this module: nothing
    inherits from it, so the adapter stays free to serve several ports from
    one session without any of them widening the others.
    """

    async def listing(
        self,
        *,
        filters: TournamentFilter,
        after: TournamentListCursor | None,
        limit: int,
        published_only: bool = False,
    ) -> TournamentPage:
        """One page of the lobby, ordered `created_at DESC, id DESC`.

        Total, so the keyset is stable. `after` continues a previous page;
        `None` starts at the newest. The limit is bounded by the caller —
        §10.5 makes every list endpoint paginate.

        Every status is included unless `filters` narrows it, completed and
        cancelled ones among them: a lobby that hid its finished
        tournaments would answer "what happened here?" with silence.

        `published_only` is **not** a sixth filter and is deliberately not on
        `TournamentFilter` — A64-026.4 §43.2. The five there come from a
        query string and a caller may choose any of them; this one comes
        from *who is asking*, and a caller who could set it could also unset
        it. It excludes `DRAFT`, which is the one state the enum itself
        describes as "not yet advertised", and it defaults to `False` so
        every existing authenticated caller is unchanged.
        """
        ...


__all__ = [
    "AlreadyRegistered",
    "AttemptAlreadyExists",
    "BracketRepository",
    "NotSeedable",
    "PairingAttemptRepository",
    "PairingRepository",
    "PlanAlreadyExists",
    "RatingSnapshots",
    "RoundRepository",
    "SeedRepository",
    "StandingRepository",
    "StandingsAlreadyRecorded",
    "PlayerDirectory",
    "NotRegistered",
    "RegistrationDeadlinePassed",
    "RegistrationNotOpen",
    "RegistrationRepository",
    "TournamentIsFull",
    "TournamentNotFound",
    "TournamentNotStartable",
    "TournamentDirectory",
    "TournamentRepository",
]
