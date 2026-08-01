"""`UserService` — the module's use cases.

A command service in the sense of services.md §3.1: one PostgreSQL
transaction per write use case, owned here and nowhere else. It
orchestrates; it does not compute (services.md §3.2) — every rule about
what a valid username *is* lives in `domain/validators.py`, and every rule
about what a `User` *is* lives in the entity. What lives here is the
sequencing: check, construct, persist, commit.

Three collaborators, all injected as ports, none constructed inside:

  `UserRepository`   storage, declared in `application/ports.py` (AD-06)
  `UnitOfWork`       the transaction boundary (services.md §9.1)
  `Clock`            "now", because AD-07 forbids reading it directly

That is what makes this class testable with an in-memory fake and a fixed
clock, with no database and no sleeping (repositories.md RP-05).

Explicitly **not** here, per the task's constraints and the module
boundary: password hashing or verification, token issuance, session
handling, "current user" resolution. `password_hash` passes through this
service as an opaque string it never inspects.
"""

import logging
from uuid import UUID

from app.core.clock import Clock
from app.core.pagination import CursorPageInfo, CursorPageParams
from app.core.sentinels import is_set
from app.core.unit_of_work import UnitOfWork
from app.modules.users.application.commands import CreateUser, UpdateUserProfile
from app.modules.users.application.ports import UserRepository
from app.modules.users.domain.entities import User
from app.modules.users.domain.exceptions import (
    EmailAlreadyExists,
    UsernameAlreadyExists,
    UserNotFound,
)
from app.modules.users.domain.validators import validate_language
from app.modules.users.domain.value_objects import (
    Bio,
    CountryCode,
    DisplayName,
    Email,
    Timezone,
    Username,
)

logger = logging.getLogger(__name__)


class UserService:
    def __init__(
        self,
        *,
        users: UserRepository,
        unit_of_work: UnitOfWork,
        clock: Clock,
    ) -> None:
        self._users = users
        self._uow = unit_of_work
        self._clock = clock

    # --- retrieval ----------------------------------------------------------

    async def get_user(self, user_id: UUID) -> User:
        """Raises `UserNotFound` rather than returning `None`.

        The port returns `None` because absence is a normal outcome *for
        the port*; this use case is "fetch the user the caller named", and
        for it, absence is the failure. Converting once here means no route
        and no future caller repeats the `if user is None: raise` line.
        """
        user = await self._users.get_by_id(user_id)
        if user is None:
            raise UserNotFound(f"No user with id {user_id}.")
        return user

    async def find_by_username(self, username: str) -> User:
        """Lookup by handle, case-insensitively (UP-1) — the folding
        happens in the `Username` value object and the repository's query,
        so a caller passing 'ALICE' finds 'alice'."""
        user = await self._users.get_by_username(Username(username))
        if user is None:
            raise UserNotFound(f"No user with username {username!r}.")
        return user

    async def find_by_email(self, email: str) -> User:
        user = await self._users.get_by_email(Email(email))
        if user is None:
            # Deliberately does not echo the address back in the message —
            # an error string is a place personal data leaks into logs and
            # screenshots (services.md §8.5).
            raise UserNotFound("No user with that email address.")
        return user

    async def lookup_by_email(self, email: str) -> User | None:
        """`find_by_email` without the exception.

        Not redundant with it, and not a candidate for merging: the two
        differ in what absence *means* to the caller. For `find_by_email`
        the user was named and must exist, so absence is a failure. For a
        sign-in attempt, absence is an ordinary outcome that must be
        indistinguishable — in code path, in error, and in elapsed time —
        from a wrong password. A caller that had to wrap this in
        `try/except UserNotFound` would be building an exception on the
        hot path of the most-attacked endpoint on the platform, and
        raising is not free.
        """
        return await self._users.get_by_email(Email(email))

    async def list_users(
        self,
        params: CursorPageParams,
        *,
        is_active: bool | None = None,
    ) -> tuple[list[User], CursorPageInfo]:
        """Read-only: opens no transaction. Keyset-paginated (RP-03)."""
        return await self._users.list(params, is_active=is_active)

    # --- creation -----------------------------------------------------------

    async def create_user(self, command: CreateUser) -> User:
        """Creates a user from an already-hashed credential.

        Reachable from no HTTP route in A64-010 — registration is A64-011's
        endpoint to add, on top of this. It exists now rather than later
        because the uniqueness rules below are *this* module's to enforce,
        and `auth` calling a service that already owns them is a much
        better seam than `auth` reaching for this module's repository.

        The two `exists_by_*` pre-checks are not the real guard, and
        deliberately so — BE-06: two concurrent sign-ups both pass a
        check-then-act, and only the database's unique constraint is
        correct under concurrency. The repository translates that
        constraint violation into the *same* two exceptions raised here, so
        no caller can tell which layer rejected it and none depends on the
        race.
        """
        username = Username(command.username)
        email = Email(command.email)
        timezone = Timezone(command.timezone)
        language = validate_language(command.preferred_language)

        if await self._users.exists_by_username(username):
            raise UsernameAlreadyExists(f"Username {command.username!r} is already taken.")
        if await self._users.exists_by_email(email):
            raise EmailAlreadyExists("That email address is already registered.")

        user = User.create(
            username=username,
            email=email,
            password_hash=command.password_hash,
            preferred_language=language,
            timezone=timezone,
            created_at=self._clock.now(),
            display_name=DisplayName(command.display_name) if command.display_name else None,
        )

        async with self._uow:
            created = await self._users.create(user)
            await self._uow.commit()

        logger.info("user_created", extra={"user_id": str(created.id)})
        return created

    # --- profile ------------------------------------------------------------

    async def update_profile(self, user_id: UUID, command: UpdateUserProfile) -> User:
        """Applies a partial update.

        Each field is applied only if the caller actually sent it —
        `is_set` distinguishes "absent" from "explicitly null", so clearing
        a display name and leaving it alone are different requests rather
        than the same one (see `app.core.sentinels`).

        Values arriving as raw strings are re-validated through the same
        value objects a create goes through, because this service is also
        callable from places that never passed through a Pydantic schema.
        """
        user = await self.get_user(user_id)

        # Each value object validates on construction, so an invalid
        # field raises before anything is assigned and the entity is never
        # left half-updated. `None` clears the field rather than
        # constructing an empty value — absence is one state.
        if is_set(command.display_name):
            user.display_name = DisplayName(command.display_name) if command.display_name else None
        if is_set(command.bio):
            user.bio = Bio(command.bio) if command.bio else None
        if is_set(command.country):
            user.country = CountryCode(command.country) if command.country else None
        if is_set(command.preferred_language):
            user.preferred_language = command.preferred_language
        if is_set(command.timezone):
            user.timezone = Timezone(command.timezone)

        user.updated_at = self._clock.now()

        async with self._uow:
            updated = await self._users.update(user)
            await self._uow.commit()

        return updated

    # --- credentials --------------------------------------------------------

    async def replace_password_hash(
        self,
        user_id: UUID,
        *,
        expected_hash: str,
        new_hash: str,
    ) -> bool:
        """Stores a re-derived hash for the *same* password.

        This service still never hashes, verifies or inspects a credential
        — the string arrives already computed by `auth`, exactly as it does
        on `create_user`. What this owns is only the transaction around the
        write, which is what `UserService` owns for every other write in
        the module.

        The return value is honest about the compare-and-swap declining:
        `False` means the stored hash was not what the caller last read.
        The caller's correct response is to do nothing (see
        `AuthenticationService` on why a rehash never fails a sign-in).
        """
        async with self._uow:
            applied = await self._users.replace_password_hash(
                user_id, expected_hash=expected_hash, new_hash=new_hash
            )
            await self._uow.commit()

        if applied:
            # No hash, no parameters, no email — only that it happened, to
            # whom, and therefore how far a parameter rollout has reached.
            logger.info("password_hash_rehashed", extra={"user_id": str(user_id)})
        return applied

    async def set_password_hash(self, user_id: UUID, *, new_hash: str) -> None:
        """Replaces the stored credential outright — A64-011.7's reset.

        As with every other credential method here, this service does not
        hash, verify or inspect anything: the string arrives already
        computed by `auth`, which owns the algorithm and its parameters.
        What this owns is the transaction and the `UserNotFound`.

        Unconditional, unlike `replace_password_hash` — see the port on
        why a recovery flow must win the race rather than lose it.

        Raises `UserNotFound` when no row matched, rather than returning
        `False`. The caller reached here holding a valid, unexpired,
        unconsumed reset token for this `user_id`, so a missing row means
        the account was deleted between the link being issued and being
        clicked. That is a genuine failure and not an outcome the caller
        can act on, which is exactly the distinction `get_user` draws.

        Returns `None` for the same reason: with absence raised, `True` is
        the only value this could ever return, and a `bool` nothing can
        falsify is a branch waiting to be written on a security path.
        """
        async with self._uow:
            applied = await self._users.set_password_hash(user_id, new_hash=new_hash)
            await self._uow.commit()

        if not applied:
            raise UserNotFound(f"No user with id {user_id}.")

        # No hash, no password, no email — only that it happened and to
        # whom. `users` records the *write*; `auth` records the reset that
        # caused it, because only `auth` knows a token was involved.
        logger.info("password_hash_replaced", extra={"user_id": str(user_id)})

    # --- avatar (A64-012.2) --------------------------------------------------

    async def set_avatar(self, user_id: UUID, *, object_key: str) -> User:
        """Points the account at a newly stored avatar object.

        This service still never inspects an image: `object_key` arrives as
        an opaque string from `avatars`, which has already validated,
        processed and stored the bytes. What this owns is the transaction
        and the invariant that the three avatar columns move together —
        enforced by `User.set_avatar`, which is why this method does not
        assign them itself.

        Raises `UserNotFound` if the account is gone.
        """
        user = await self.get_user(user_id)
        user.set_avatar(object_key, at=self._clock.now())
        user.updated_at = self._clock.now()

        async with self._uow:
            updated = await self._users.update(user)
            await self._uow.commit()

        # The version, never the key. An object key is not secret, but it is
        # an address — logging it on every upload turns the log into an
        # index of every stored object, which is the first thing worth
        # having if the store is ever misconfigured to allow listing.
        logger.info(
            "avatar_reference_set",
            extra={"user_id": str(user_id), "avatar_version": updated.avatar_version},
        )
        return updated

    async def clear_avatar(self, user_id: UUID) -> User:
        """Removes the reference and bumps the version.

        Idempotent for a player who has none — it succeeds and bumps
        anyway, because the version is a cache key rather than a count and
        a caller retrying after a dropped response must not get an error
        (CLAUDE.md §3 rule 8).

        Deletes nothing from storage: this module has no storage. Removing
        the objects is `AvatarService.delete`'s next step.
        """
        user = await self.get_user(user_id)
        user.clear_avatar()
        user.updated_at = self._clock.now()

        async with self._uow:
            updated = await self._users.update(user)
            await self._uow.commit()

        logger.info(
            "avatar_reference_cleared",
            extra={"user_id": str(user_id), "avatar_version": updated.avatar_version},
        )
        return updated

    async def mark_email_verified(self, user_id: UUID) -> User:
        """Records that ownership of the address has been proven.

        The transition domain-model.md §6.1 draws as `PendingVerification
        -> Active`. The *decision* that verification happened belongs to
        `auth`, which holds the token; the invariant that nothing else
        flips the flag stays here, which is why this method exists rather
        than `auth` writing the column.

        Idempotent: verifying an already-verified account is a no-op that
        still succeeds and skips the write. A caller retrying after a
        dropped response must not get an error for the retry (CLAUDE.md §3
        rule 8) — and on this flow the retry is a person double-clicking a
        link in an email, which is the common case rather than the edge.
        """
        user = await self.get_user(user_id)
        if user.is_verified:
            return user

        user.mark_verified()
        user.updated_at = self._clock.now()

        async with self._uow:
            updated = await self._users.update(user)
            await self._uow.commit()

        logger.info("email_verified", extra={"user_id": str(user_id)})
        return updated

    # --- lifecycle ----------------------------------------------------------

    async def activate(self, user_id: UUID) -> User:
        """Idempotent: activating an already-active user is a no-op that
        still succeeds. A caller retrying after a dropped response must not
        get an error for the retry (CLAUDE.md §3 rule 8)."""
        return await self._set_active(user_id, is_active=True)

    async def deactivate(self, user_id: UUID) -> User:
        """Idempotent, as `activate`. Not a delete — see
        `User.deactivate`'s docstring on why this is a reversible state and
        not a soft-delete flag."""
        return await self._set_active(user_id, is_active=False)

    async def _set_active(self, user_id: UUID, *, is_active: bool) -> User:
        user = await self.get_user(user_id)

        if user.is_active == is_active:
            return user

        if is_active:
            user.activate()
        else:
            user.deactivate()
        user.updated_at = self._clock.now()

        async with self._uow:
            updated = await self._users.update(user)
            await self._uow.commit()

        logger.info(
            "user_activation_changed",
            extra={"user_id": str(user_id), "is_active": is_active},
        )
        return updated
