"""The SQLAlchemy adapter for `application.ports.UserRepository`.

Database-only, per the task's constraint and repositories.md §2's third
consequence: this class decides *how* to store and fetch, never *whether*
something may be stored. Every "is this allowed" question — is the
username taken, may this user be updated — is answered by `UserService`.

Two responsibilities beyond running SQL, both of which repositories.md
assigns here explicitly:

  **mapping** (§3) — between `UserModel` rows and `User` domain entities,
  in both directions, so nothing above this layer ever holds an ORM object
  and inherits its lazy-loading and session-lifetime behaviour.

  **error translation** (§3) — a driver `IntegrityError` becomes the same
  typed domain exception the service's pre-check would have raised. BE-06
  requires exactly this: the constraint is the authoritative check, and a
  caller must not be able to tell which layer rejected it.
"""

import logging
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, Select, exists, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import CursorPageInfo, CursorPageParams
from app.modules.users.domain.entities import User
from app.modules.users.domain.exceptions import (
    EmailAlreadyExists,
    UsernameAlreadyExists,
    UserNotFound,
)
from app.modules.users.domain.preferences import (
    GameplayPreferences,
    LocalePreferences,
    Preferences,
)
from app.modules.users.domain.privacy import PrivacySettings
from app.modules.users.domain.value_objects import (
    Bio,
    CountryCode,
    DisplayName,
    Email,
    Timezone,
    Username,
)
from app.modules.users.infrastructure.models import UserModel
from app.repositories.pagination import paginate_cursor

logger = logging.getLogger(__name__)

# Maps a violated constraint to the exception the service would have
# raised itself. Keyed by the names declared in `models.py.__table_args__`
# — if one is renamed there without being renamed here, the translation
# silently stops working, which is why a contract test drives a real
# violation through this path rather than trusting the mapping by eye.
_CONSTRAINT_ERRORS: dict[str, type[UsernameAlreadyExists | EmailAlreadyExists]] = {
    "uq_user__username_folded": UsernameAlreadyExists,
    "uq_user__email": EmailAlreadyExists,
}


class SqlAlchemyUserRepository:
    """Constructed per use case with the active unit of work's session
    (repositories.md §5.1) — never holds a session longer than that."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- mapping ------------------------------------------------------------

    @staticmethod
    def _to_domain(row: UserModel) -> User:
        return User(
            id=row.id,
            username=Username(row.username),
            email=Email(row.email),
            password_hash=row.password_hash,
            is_active=row.is_active,
            is_verified=row.is_verified,
            created_at=row.created_at,
            updated_at=row.updated_at,
            display_name=DisplayName(row.display_name) if row.display_name else None,
            avatar_object_key=row.avatar_object_key,
            avatar_uploaded_at=row.avatar_uploaded_at,
            avatar_version=row.avatar_version,
            # Reconstructed through the value objects rather than assigned
            # raw, so a row written before a rule tightened fails loudly
            # here instead of flowing into a response. `None` stays `None`:
            # absence is one state, not an empty `Bio`.
            bio=Bio(row.bio) if row.bio else None,
            country=CountryCode(row.country_code) if row.country_code else None,
            # Locale lives inside `preferences` since A64-012.5, so the two
            # columns are reassembled here rather than assigned to the
            # entity's top level. `from_document` fills every key the
            # `jsonb` document does not carry, which is what makes `{}` —
            # what a fresh account holds — read as the platform defaults.
            preferences=Preferences(
                gameplay=GameplayPreferences.from_document(row.gameplay_preferences),
                locale=LocalePreferences(
                    preferred_language=row.preferred_language,
                    timezone=Timezone(row.timezone),
                ),
            ),
            # The five columns become one value object here and split back
            # into five in `_to_model` — which is the whole reason the
            # entity holds a `PrivacySettings` rather than five booleans.
            # Every consumer above this line takes the group or none of it.
            privacy=PrivacySettings(
                show_country=row.show_country,
                show_last_seen=row.show_last_seen,
                show_statistics=row.show_statistics,
                show_online_status=row.show_online_status,
                show_activity=row.show_activity,
            ),
            locked_until=row.locked_until,
        )

    @staticmethod
    def _to_model(user: User) -> UserModel:
        # `username_folded` is absent on purpose: it is a PostgreSQL
        # generated column (models.py), so assigning it here would be
        # rejected by the database.
        return UserModel(
            id=user.id,
            username=user.username.value,
            email=user.email.value,
            password_hash=user.password_hash,
            preferred_language=user.preferred_language,
            timezone=user.timezone.value,
            is_active=user.is_active,
            is_verified=user.is_verified,
            created_at=user.created_at,
            updated_at=user.updated_at,
            display_name=user.display_name.value if user.display_name else None,
            avatar_object_key=user.avatar_object_key,
            avatar_uploaded_at=user.avatar_uploaded_at,
            avatar_version=user.avatar_version,
            bio=user.bio.value if user.bio else None,
            country_code=user.country.value if user.country else None,
            gameplay_preferences=user.preferences.gameplay.as_document(),
            show_country=user.privacy.show_country,
            show_last_seen=user.privacy.show_last_seen,
            show_statistics=user.privacy.show_statistics,
            show_online_status=user.privacy.show_online_status,
            show_activity=user.privacy.show_activity,
            locked_until=user.locked_until,
        )

    @staticmethod
    def _apply_to_model(row: UserModel, user: User) -> None:
        """Copies the mutable fields of a domain entity onto its row.

        `id` and `created_at` are excluded deliberately — an entity's
        identity and its creation instant are not things an update may
        change, and silently allowing either would let a caller rewrite
        history by mutating a field on a detached object.
        """
        row.username = user.username.value
        row.email = user.email.value
        row.password_hash = user.password_hash
        row.preferred_language = user.preferred_language
        row.timezone = user.timezone.value
        row.is_active = user.is_active
        row.is_verified = user.is_verified
        row.updated_at = user.updated_at
        row.display_name = user.display_name.value if user.display_name else None
        row.avatar_object_key = user.avatar_object_key
        row.avatar_uploaded_at = user.avatar_uploaded_at
        row.avatar_version = user.avatar_version
        row.bio = user.bio.value if user.bio else None
        row.country_code = user.country.value if user.country else None
        row.gameplay_preferences = user.preferences.gameplay.as_document()
        row.show_country = user.privacy.show_country
        row.show_last_seen = user.privacy.show_last_seen
        row.show_statistics = user.privacy.show_statistics
        row.show_online_status = user.privacy.show_online_status
        row.show_activity = user.privacy.show_activity
        row.locked_until = user.locked_until

    # --- error translation --------------------------------------------------

    @staticmethod
    def _constraint_name(error: IntegrityError) -> str | None:
        """Digs the violated constraint's name out of the exception chain.

        Not simply `error.orig.constraint_name`: SQLAlchemy's asyncpg
        dialect wraps the driver's exception in its *own* `IntegrityError`
        before setting `.orig`, so the attribute lives one level further
        down on `error.orig.__cause__` — verified against asyncpg rather
        than assumed, because the obvious spelling silently returns `None`
        and every violation would then fall through as an unmapped defect.

        Walks the chain rather than hardcoding two levels, so a driver or
        SQLAlchemy version that nests differently keeps working.
        """
        seen: set[int] = set()
        current: BaseException | None = error.orig
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            name = getattr(current, "constraint_name", None)
            if isinstance(name, str) and name:
                return name
            current = current.__cause__
        return None

    @classmethod
    def _translate_integrity_error(cls, error: IntegrityError) -> Exception:
        """Turns a unique-constraint violation into the matching domain
        exception, leaving anything else untouched.

        The constraint name is read from asyncpg's own diagnostics rather
        than by matching on the exception's message text, which is
        localised and version-dependent.
        """
        constraint = cls._constraint_name(error)
        exception_type = _CONSTRAINT_ERRORS.get(constraint or "")

        if exception_type is UsernameAlreadyExists:
            return UsernameAlreadyExists("That username is already taken.")
        if exception_type is EmailAlreadyExists:
            return EmailAlreadyExists("That email address is already registered.")

        # An unrecognised constraint is a defect, not a domain outcome —
        # propagate it unchanged so it surfaces as a 500 with a stack
        # rather than being mislabelled as a user error (BE-07).
        logger.error("unmapped_integrity_error", extra={"constraint": constraint}, exc_info=error)
        return error

    # --- reads --------------------------------------------------------------

    async def get_by_id(self, user_id: UUID) -> User | None:
        row = await self._session.get(UserModel, user_id)
        return None if row is None else self._to_domain(row)

    async def get_by_username(self, username: Username) -> User | None:
        # Matches on the folded column, so lookup is case-insensitive in
        # exactly the same way uniqueness is (UP-1).
        statement = select(UserModel).where(UserModel.username_folded == username.folded)
        row = (await self._session.scalars(statement)).one_or_none()
        return None if row is None else self._to_domain(row)

    async def get_by_email(self, email: Email) -> User | None:
        statement = select(UserModel).where(UserModel.email == email.value)
        row = (await self._session.scalars(statement)).one_or_none()
        return None if row is None else self._to_domain(row)

    async def exists_by_username(self, username: Username) -> bool:
        statement = select(exists().where(UserModel.username_folded == username.folded))
        return bool(await self._session.scalar(statement))

    async def exists_by_email(self, email: Email) -> bool:
        statement = select(exists().where(UserModel.email == email.value))
        return bool(await self._session.scalar(statement))

    async def list(
        self,
        params: CursorPageParams,
        *,
        is_active: bool | None = None,
    ) -> tuple[list[User], CursorPageInfo]:
        statement: Select[tuple[UserModel]] = select(UserModel)
        if is_active is not None:
            statement = statement.where(UserModel.is_active == is_active)

        rows, page = await paginate_cursor(
            self._session,
            statement,
            params,
            order_column=UserModel.created_at,
            id_column=UserModel.id,
        )
        return [self._to_domain(row) for row in rows], page

    # --- writes -------------------------------------------------------------

    async def create(self, user: User) -> User:
        row = self._to_model(user)
        self._session.add(row)
        try:
            # Flush, never commit — the unit of work owns the transaction
            # (repositories.md §5.1). Flushing here is what makes the
            # constraint fire now, so the violation can be translated into
            # a domain error at the point that has the context to do it.
            await self._session.flush()
        except IntegrityError as error:
            raise self._translate_integrity_error(error) from error

        return self._to_domain(row)

    async def update(self, user: User) -> User:
        row = await self._session.get(UserModel, user.id)
        if row is None:
            # The service loaded this user moments ago, so reaching here
            # means it was deleted in between. Raising the same exception
            # the service raises for a missing user keeps that race
            # indistinguishable from an ordinary "not found" to a caller.
            raise UserNotFound(f"No user with id {user.id}.")

        self._apply_to_model(row, user)
        try:
            await self._session.flush()
        except IntegrityError as error:
            raise self._translate_integrity_error(error) from error

        return self._to_domain(row)

    async def replace_password_hash(
        self,
        user_id: UUID,
        *,
        expected_hash: str,
        new_hash: str,
    ) -> bool:
        # A Core `update()` rather than loading the row and assigning:
        # `WHERE password_hash = :expected` has to be evaluated by
        # PostgreSQL, in the same statement as the write, or it is not a
        # compare-and-swap at all (see the port's docstring).
        #
        # `updated_at` moves too — `TimestampMixin` declares
        # `onupdate=func.now()`, which fires for Core statements as well as
        # ORM flushes. That is correct rather than incidental: the row did
        # change, and anything syncing on `updated_at` should see it.
        #
        # `synchronize_session=False` because nothing in this session holds
        # the row; there is no identity-map copy to keep consistent.
        #
        # The `cast` is a typing accommodation, not a claim: `execute` is
        # declared to return `Result[Any]`, which has no `rowcount` because
        # a SELECT has no such notion — a DML statement always returns the
        # `CursorResult` subtype that does.
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                update(UserModel)
                .where(UserModel.id == user_id, UserModel.password_hash == expected_hash)
                .values(password_hash=new_hash)
                .execution_options(synchronize_session=False)
            ),
        )
        return result.rowcount == 1

    async def set_password_hash(self, user_id: UUID, *, new_hash: str) -> bool:
        # A Core `update()` for the same reasons as the method above minus
        # the condition: one statement, no read-modify-write, and
        # `updated_at` moved by `TimestampMixin`'s `onupdate`.
        #
        # No `WHERE password_hash = ...`, deliberately — see the port. A
        # reset must overwrite whatever is stored, including a hash written
        # a millisecond ago, because the person holding the token has
        # proven control of the address and the alternative is an account
        # that cannot be recovered while somebody else is writing to it.
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                update(UserModel)
                .where(UserModel.id == user_id)
                .values(password_hash=new_hash)
                .execution_options(synchronize_session=False)
            ),
        )
        return result.rowcount == 1

    async def delete(self, user_id: UUID) -> bool:
        row = await self._session.get(UserModel, user_id)
        if row is None:
            return False

        await self._session.delete(row)
        await self._session.flush()
        return True
