"""An in-memory `UserRepository` — repositories.md RP-05's fake.

Satisfies the same `Protocol` as `SqlAlchemyUserRepository` structurally,
and is held to the same contract suite (`tests/contract/test_user_repository.py`),
which is the only thing that keeps the two honest about each other.

The behaviours it goes out of its way to reproduce are the ones a naive
dict-backed fake gets wrong and a service would then be tested against
incorrectly:

  - **case-insensitive username lookup and uniqueness**, keyed on
    `Username.folded` exactly as the real adapter queries the generated
    `username_folded` column;
  - **the same typed exceptions** on a uniqueness violation, so a service
    test proves the same branch the production path takes;
  - **copies on the way in and out**, because the real repository maps
    through an ORM row and therefore hands back a *different object* than
    the caller passed. A fake that stored the caller's instance would let
    a test mutate a "persisted" user without saving and still see the
    change on the next read — passing for a reason production would not.
"""

import copy
from datetime import datetime
from uuid import UUID

from app.core.pagination import CursorPageInfo, CursorPageParams, decode_cursor, encode_cursor
from app.modules.users.domain.entities import User
from app.modules.users.domain.exceptions import (
    EmailAlreadyExists,
    UsernameAlreadyExists,
    UserNotFound,
)
from app.modules.users.domain.value_objects import Email, Username


class FakeUserRepository:
    def __init__(self, users: list[User] | None = None) -> None:
        self._users: dict[UUID, User] = {}
        for user in users or []:
            self._users[user.id] = copy.deepcopy(user)

    # --- reads --------------------------------------------------------------

    async def get_by_id(self, user_id: UUID) -> User | None:
        found = self._users.get(user_id)
        return copy.deepcopy(found) if found is not None else None

    async def get_by_username(self, username: Username) -> User | None:
        for user in self._users.values():
            if user.username.folded == username.folded:
                return copy.deepcopy(user)
        return None

    async def get_by_email(self, email: Email) -> User | None:
        for user in self._users.values():
            if user.email.value == email.value:
                return copy.deepcopy(user)
        return None

    async def exists_by_username(self, username: Username) -> bool:
        return any(user.username.folded == username.folded for user in self._users.values())

    async def exists_by_email(self, email: Email) -> bool:
        return any(user.email.value == email.value for user in self._users.values())

    async def list(
        self,
        params: CursorPageParams,
        *,
        is_active: bool | None = None,
    ) -> tuple[list[User], CursorPageInfo]:
        candidates = [
            user
            for user in self._users.values()
            if is_active is None or user.is_active == is_active
        ]
        # Same ordering key as the real adapter's index: `(created_at, id)`.
        # `id` is the tiebreak that stops rows sharing a timestamp from
        # being skipped or repeated across a page boundary.
        candidates.sort(key=lambda user: (user.created_at, user.id))

        if params.cursor is not None:
            last_created_at, last_id = decode_cursor(params.cursor)
            # Compare as real values, not as strings. `encode_cursor`
            # serialises via `str()`, so parsing back with the *same*
            # spelling is what keeps the fake's page boundaries identical
            # to the real adapter's — an earlier version compared
            # `datetime.isoformat()` against `str(datetime)`, which differ
            # by one character ('T' vs ' ') and silently dropped a row at
            # every boundary.
            boundary = (datetime.fromisoformat(str(last_created_at)), UUID(str(last_id)))
            candidates = [user for user in candidates if (user.created_at, user.id) > boundary]

        window = candidates[: params.limit + 1]
        has_more = len(window) > params.limit
        page_users = window[: params.limit]

        next_cursor: str | None = None
        if has_more and page_users:
            last = page_users[-1]
            next_cursor = encode_cursor(last.created_at, last.id)

        return (
            [copy.deepcopy(user) for user in page_users],
            CursorPageInfo(next_cursor=next_cursor, has_more=has_more),
        )

    # --- writes -------------------------------------------------------------

    async def create(self, user: User) -> User:
        if await self.exists_by_username(user.username):
            raise UsernameAlreadyExists("That username is already taken.")
        if await self.exists_by_email(user.email):
            raise EmailAlreadyExists("That email address is already registered.")

        self._users[user.id] = copy.deepcopy(user)
        return copy.deepcopy(user)

    async def update(self, user: User) -> User:
        if user.id not in self._users:
            raise UserNotFound(f"No user with id {user.id}.")

        for other_id, other in self._users.items():
            if other_id == user.id:
                continue
            if other.username.folded == user.username.folded:
                raise UsernameAlreadyExists("That username is already taken.")
            if other.email.value == user.email.value:
                raise EmailAlreadyExists("That email address is already registered.")

        self._users[user.id] = copy.deepcopy(user)
        return copy.deepcopy(user)

    async def delete(self, user_id: UUID) -> bool:
        return self._users.pop(user_id, None) is not None
