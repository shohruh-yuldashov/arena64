"""What `tournament`'s use cases need from the world — AD-06.

Two repositories and one published reader from `users`. Nothing else: this
phase creates no matches, reads no ratings and delivers no notifications.

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
from typing import Protocol
from uuid import UUID

from app.core.exceptions import DomainError
from app.modules.game.public import ProductVariant
from app.modules.rating.public import RatingSnapshot, SpeedClass
from app.modules.tournament.domain.registration import Registration
from app.modules.tournament.domain.seeding import PlannedPairing, Seed
from app.modules.tournament.domain.tournament import Tournament


class TournamentNotFound(DomainError):
    """No tournament with that id.

    A `DomainError` rather than a `None` return, because every caller's
    answer is the same and a use case that had to branch on absence would
    eventually forget."""


class AlreadyRegistered(DomainError):
    """This player is already entered — the unique key refused the insert.

    Raised from the constraint rather than from a prior read, so two
    concurrent requests cannot both find nothing and both insert.
    """


class TournamentIsFull(DomainError):
    """Capacity is reached. Raised inside the lock — see this module's
    docstring."""


class RegistrationNotOpen(DomainError):
    """The tournament is not accepting entries.

    Covers "not yet open" and "already closed" with one type: a client's
    response to both is the same, and distinguishing them would say more
    about a tournament's schedule than a refusal needs to.
    """


class NotRegistered(DomainError):
    """This player has no live entry to withdraw."""


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


class NotSeedable(DomainError):
    """The tournament cannot be seeded yet — §2.

    Registration must be **closed** first: seeding an open tournament would
    build a bracket from a field that can still change, and the plan is
    immutable once written.
    """


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

    async def seeds_for(self, tournament_id: UUID) -> list[Seed]:
        """The persisted seeding, for a retry to return unchanged."""
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


__all__ = [
    "AlreadyRegistered",
    "NotSeedable",
    "PairingRepository",
    "PlanAlreadyExists",
    "RatingSnapshots",
    "SeedRepository",
    "PlayerDirectory",
    "NotRegistered",
    "RegistrationNotOpen",
    "RegistrationRepository",
    "TournamentIsFull",
    "TournamentNotFound",
    "TournamentRepository",
]
