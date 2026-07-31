"""The ports `users`' use cases program against.

Declared in `application/`, not `infrastructure/` — architecture.md AD-06.
The use case states the persistence it needs; infrastructure satisfies it.
That direction is what lets `UserService` be tested against an in-memory
fake with no database (repositories.md RP-05) and what would let the
storage decision change without a use case noticing.

Every method takes and returns **domain types** — `User`, `Username`,
`Email` — never ORM rows and never Pydantic models (repositories.md §4).
"""

from typing import Protocol
from uuid import UUID

from app.core.pagination import CursorPageInfo, CursorPageParams
from app.modules.users.domain.entities import User
from app.modules.users.domain.value_objects import Email, Username


class UserRepository(Protocol):
    """Collection-like access to the `User` aggregate.

    A `Protocol`, not an ABC: the SQLAlchemy adapter and the test fake
    satisfy it structurally without either importing the other or a shared
    base. Method names are stated by the task; each is a single storage
    operation with no business rule in it — "does this username collide"
    is `exists_by_username`, while "*therefore* reject the update" is the
    service's decision, not this port's (repositories.md §2 consequence 3:
    a repository has no opinions).
    """

    async def create(self, user: User) -> User:
        """Persists a new user and returns it. Flushes so a caller sees any
        database-assigned value immediately; never commits — the unit of
        work owns that (repositories.md §5.1)."""
        ...

    async def get_by_id(self, user_id: UUID) -> User | None:
        """`None` rather than raising: a user that legitimately may not
        exist is a normal outcome to model in the return type, not an
        exception (CLAUDE.md §9 rule 8). Turning `None` into `UserNotFound`
        is the service's job, because only the service knows whether
        absence is an error *for this use case*."""
        ...

    async def get_by_email(self, email: Email) -> User | None: ...

    async def get_by_username(self, username: Username) -> User | None: ...

    async def exists_by_email(self, email: Email) -> bool:
        """Separate from `get_by_email` because a uniqueness pre-check does
        not need to build an entity, and on a sign-up path that difference
        is a `SELECT 1` against a fetch of every column."""
        ...

    async def exists_by_username(self, username: Username) -> bool: ...

    async def update(self, user: User) -> User:
        """Persists changes to an already-loaded user."""
        ...

    async def replace_password_hash(
        self,
        user_id: UUID,
        *,
        expected_hash: str,
        new_hash: str,
    ) -> bool:
        """Conditionally swaps one stored hash for another; returns whether
        a row matched.

        A single conditional statement rather than `get_by_id` → mutate →
        `update`, because the condition *is* the point: read-then-write
        across two round trips has a window in which the credential
        changes, and losing that race means overwriting a new password
        with an old one. Expressing it as one `UPDATE ... WHERE
        password_hash = :expected` makes the check and the write atomic in
        the database, which is the only place they can be.

        Still a single storage operation with no opinion in it
        (repositories.md §2): *whether* to rehash is the caller's
        decision, and this port neither computes hashes nor knows what
        makes one stale.
        """
        ...

    async def delete(self, user_id: UUID) -> bool:
        """Removes the row. Returns whether anything was deleted, so the
        service can distinguish "gone now" from "was never there" without
        a second round trip.

        A *hard* delete, deliberately: database.md DB-20 forbids a generic
        soft-delete column, and `is_active` already covers the reversible
        "not usable right now" case. The irreversible case is erasure
        (DM-13), which anonymises rather than deletes and is a platform
        obligation with its own request record — not this method."""
        ...

    async def list(
        self,
        params: CursorPageParams,
        *,
        is_active: bool | None = None,
    ) -> tuple[list[User], CursorPageInfo]:
        """Keyset-paginated listing — repositories.md RP-03's default,
        because the user table only grows and `OFFSET` on it gets slower
        with every page.

        `is_active` is the one filter this port exposes, and it is an
        explicit named parameter rather than a generic predicate bag:
        repositories.md §4 forbids a generic filter API precisely so that
        every query has an owner and an index can be designed for it."""
        ...
