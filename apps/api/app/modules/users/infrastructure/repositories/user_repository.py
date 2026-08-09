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
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import (
    CursorResult,
    Select,
    Text,
    case,
    exists,
    func,
    literal,
    or_,
    select,
    tuple_,
    update,
)
from sqlalchemy import (
    cast as sql_cast,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationError
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
from app.modules.users.domain.search import SearchTerm
from app.modules.users.domain.value_objects import (
    Bio,
    CountryCode,
    DisplayName,
    Email,
    Timezone,
    Username,
)
from app.modules.users.infrastructure.models import UserModel
from app.modules.users.infrastructure.search_cursor import SearchCursor
from app.modules.users.public.administration import (
    AdminUserFilters,
    AdminUserPage,
    AdminUserRecord,
)
from app.modules.users.public.search import UserSearchQuery
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


#: The four ranking buckets, smallest first — `ORDER BY` is ascending, so a
#: lower number sorts earlier. Named constants rather than bare integers in
#: the `CASE` because the numbers also travel in the pagination cursor, and
#: a magic `2` in two files is a magic `2` somebody renumbers in one of them.
_RANK_EXACT_USERNAME = 0
_RANK_USERNAME_PREFIX = 1
_RANK_DISPLAY_NAME_PREFIX = 2
_RANK_PARTIAL = 3


def _search_normalise(expression: Any) -> Any:
    """`users.search_normalise(x)` — the one normalisation both the indexes
    and the query are built on.

    A helper rather than four inline `func` calls, so that the expression
    the GIN indexes were created on is written **once** on this side too.
    The indexes and these calls must render identically or PostgreSQL plans
    a sequential scan, which fails no test that is not looking for it —
    hence `test_user_search_repository.py`'s plan assertion.

    Typed as `Text` explicitly. Without it SQLAlchemy treats the result as
    untyped and `func.concat` composes it into something PostgreSQL rejects
    as `unknown`; with it, the `LIKE` and `||` below are unambiguously
    string operations.
    """
    return func.users.search_normalise(expression, type_=Text)


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
                show_statistics=row.show_statistics,
                last_seen=row.last_seen_visibility,
                online_status=row.online_status_visibility,
                activity=row.activity_visibility,
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
            last_seen_visibility=user.privacy.last_seen,
            show_statistics=user.privacy.show_statistics,
            online_status_visibility=user.privacy.online_status,
            activity_visibility=user.privacy.activity,
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
        row.last_seen_visibility = user.privacy.last_seen
        row.show_statistics = user.privacy.show_statistics
        row.online_status_visibility = user.privacy.online_status
        row.activity_visibility = user.privacy.activity
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

    async def get_active_by_ids(self, user_ids: Sequence[UUID]) -> Sequence[User]:
        """The **active** accounts among `user_ids`, in one query.

        Primary-key lookups batched with `IN`, so PostgreSQL probes the
        index once per id rather than scanning — the same access pattern as
        `get_by_id`, without the round trips.

        Filters `is_active` here rather than leaving it to the caller,
        because every consumer of this method serves a public view and
        `users` owns the rule. A withdrawn account is simply absent, which
        is the same answer `get_by_username` gives through
        `PublicProfileService`.

        Order is not specified and callers must not rely on it: they hold
        their own ordering — a friend-request list is ordered by when the
        request arrived, not by anything about the player.
        """
        if not user_ids:
            return []

        rows = await self._session.scalars(
            select(UserModel).where(
                UserModel.id.in_(user_ids),
                UserModel.is_active.is_(True),
            )
        )
        return [self._to_domain(row) for row in rows]

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

    async def search(self, query: UserSearchQuery) -> tuple[Sequence[User], str | None]:
        """Ranked, keyset-paginated search over username and display name —
        A64-013.1.

        ## One normalisation, applied three times

        `users.search_normalise(text)` — created by migration `a7c31f5d9e04`
        — is `unaccent(lower(normalize(x, NFKC)))` behind an `IMMUTABLE`
        wrapper. It is applied to the username, to the display name, and to
        the term, and **the two GIN indexes are built on exactly these
        expressions**.

        That is the whole reason the term is not normalised in Python.
        Matching requires both sides to agree character-for-character, and
        two implementations in two languages drift — `fold_username`'s
        docstring records that PostgreSQL's `lower()` and Python's
        `casefold()` already disagree about `ß`. A drift here would not
        raise; it would silently return nothing for the affected characters,
        and nobody reports a search that found no one. One function, called
        from one place, cannot drift.

        The index expressions must match these calls exactly or PostgreSQL
        will plan a sequential scan — a silent performance regression rather
        than an error. `tests/contract/test_user_search_repository.py`
        asserts the plan, which is the only way to catch it.

        ## The ranking

            0  exact username
            1  username prefix
            2  display-name prefix
            3  partial match anywhere

        `ORDER BY rank, username_folded, id`. Deterministic for a given
        term and dataset, which is what "stable ordering" requires and what
        the cursor depends on: `username_folded` is unique platform-wide, so
        the ordering is total before `id` is even considered.

        The `CASE` is evaluated twice — once to order, once inside the
        keyset predicate — because PostgreSQL cannot reference a `SELECT`
        alias in `WHERE`. The alternative is a subquery or CTE wrapping the
        ORM entity, which costs an `aliased()` indirection through the whole
        mapping for an expression the planner computes on rows it has
        already fetched.

        ## Why `LIKE` and not full-text search

        `to_tsvector` matches *words*, and a handle is not a word: nobody
        searching for `alic` expects to miss `alice`, and a tsquery prefix
        match cannot find `ice` inside it at all. Trigram similarity over
        `LIKE` is the shape that answers what a person typing into a search
        box means, and pg_trgm's GIN operator class makes `LIKE '%x%'`
        index-accelerated rather than the scan it is without one.

        The honest cost: at the two-character minimum a trigram index has
        one trigram to work with and its selectivity is poor, so short terms
        do more work than long ones. That is bounded by the rate limit and
        by the page size rather than by the query, and it is why the minimum
        is two rather than one.

        Returns the page's rows and the cursor for the next page, or `None`
        when this is the last one. A `Sequence` rather than a `list`, and
        not only because `list` is shadowed by the method above: callers
        iterate the page in rank order and must not reorder it in place.

        Raises `InvalidSearchCursor` for a cursor that is malformed or was
        issued for a different term.
        """
        statement = self.build_search_statement(query)

        rows = (await self._session.execute(statement)).all()

        has_more = len(rows) > query.limit
        page = rows[: query.limit]

        next_cursor: str | None = None
        if has_more and page:
            last_row, last_rank = page[-1]
            next_cursor = SearchCursor(
                rank=last_rank,
                username_folded=last_row.username_folded,
                player_id=last_row.id,
            ).encode(term=SearchTerm.parse(query.term).value)

        return [self._to_domain(row) for row, _ in page], next_cursor

    def build_search_statement(self, query: UserSearchQuery) -> Select[tuple[UserModel, int]]:
        """The search statement, built but not executed.

        Separated from `search` above so that
        `tests/contract/test_user_search_repository.py` can `EXPLAIN` the
        **real** query rather than a hand-written copy of it. That matters
        more than it sounds: the failure that test exists to catch is the
        repository's expression drifting away from the index's, and a test
        holding its own copy of the SQL could not catch it — the copy would
        drift with the index and agree with nothing.

        Public rather than underscore-prefixed for the same reason. It is
        part of what this adapter offers its own contract suite, and a
        leading underscore would be a claim that reaching for it is a
        violation when it is the point.
        """
        term = SearchTerm.parse(query.term)

        username_key = _search_normalise(UserModel.username)
        display_key = _search_normalise(UserModel.display_name)
        # The exact-match form and the `LIKE` form are separate binds and
        # must be: `pattern` carries backslashes in front of every `LIKE`
        # metacharacter, so comparing it with `=` would never match a
        # username containing an underscore — which is most of them.
        term_value = _search_normalise(literal(term.value))
        term_pattern = _search_normalise(literal(term.pattern))

        contains = func.concat("%", term_pattern, "%")
        starts_with = func.concat(term_pattern, "%")

        rank = case(
            (username_key == term_value, _RANK_EXACT_USERNAME),
            (username_key.like(starts_with, escape="\\"), _RANK_USERNAME_PREFIX),
            (display_key.like(starts_with, escape="\\"), _RANK_DISPLAY_NAME_PREFIX),
            else_=_RANK_PARTIAL,
        )

        statement = (
            select(UserModel, rank.label("search_rank"))
            .where(
                # Deactivated accounts are invisible here exactly as they
                # are on `GET /profiles/{username}` — `users` owns
                # `is_active`, and which handles belong to withdrawn
                # accounts is itself a disclosure.
                UserModel.is_active.is_(True),
                or_(
                    username_key.like(contains, escape="\\"),
                    display_key.like(contains, escape="\\"),
                ),
            )
            .order_by(rank, UserModel.username_folded, UserModel.id)
            # Over-fetch by one to learn whether a further page exists
            # without a second count — RP-03's reason, and here also
            # because counting a `LIKE` match gets slower the more it
            # matches.
            .limit(query.limit + 1)
        )

        if query.exclude_player_ids:
            # The blocking seam. Non-empty on every request today because
            # the searcher never appears in their own results, so this
            # branch is exercised rather than reserved — see
            # `UserSearchQuery.exclude_player_ids`.
            statement = statement.where(UserModel.id.notin_(query.exclude_player_ids))

        if query.cursor is not None:
            cursor = SearchCursor.decode(query.cursor, term=term.value)
            statement = statement.where(
                tuple_(rank, UserModel.username_folded, UserModel.id)
                > tuple_(
                    literal(cursor.rank),
                    literal(cursor.username_folded),
                    sql_cast(literal(cursor.player_id), UserModel.id.type),
                )
            )

        return statement

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


class SqlAlchemyAdministrativeUserDirectory:
    """`users.public.AdministrativeUserDirectory` over PostgreSQL — A64-024.3.

    A **separate adapter** from `SqlAlchemyUserRepository.search`, which
    answers a player's question with privacy applied and blocked players
    removed. This answers an operator's: every account, found by the two
    identifiers an operator actually has.

    ## Every query is index-backed and bounded

        no term    ORDER BY (created_at, id) DESC   ix_user__created_at_id
        term       username_folded LIKE 'x%'        uq_user__username_folded
                OR email            LIKE 'x%'       uq_user__email

    Prefix, not substring. `%term%` on either column cannot use a btree and
    would be a sequential scan on every keystroke — §3 rules that out, and
    an operator searching for an account has a prefix rather than a
    fragment. Username *substring* search already exists for players
    through the trigram indexes; it is not reused here because it does not
    cover email and this port's whole reason to exist is that it does.

    ## The keyset, and why it is `(created_at, id)`

    `created_at` alone is not unique, so a cursor on it silently skips or
    repeats rows under concurrent registration. The composite index exists
    for exactly this and the `id` tiebreak is what makes the ordering total.

    One page is **one query**. There is no count — see `AdminUserPage`.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_accounts(
        self,
        *,
        term: str | None,
        filters: "AdminUserFilters",
        limit: int,
        cursor: str | None,
    ) -> "AdminUserPage":
        statement = select(UserModel)

        if term is not None:
            # Normalised by the same SQL function the username index is
            # built on, so the two sides cannot drift — see `_search_normalise`
            # on why this is not done in Python. The email half is compared
            # against the stored, already-normalised address.
            pattern = f"{_escape_like(term.strip().lower())}%"
            statement = statement.where(
                or_(
                    UserModel.username_folded.like(pattern, escape="\\"),
                    UserModel.email.like(pattern, escape="\\"),
                )
            )

        if filters.is_active is not None:
            statement = statement.where(UserModel.is_active.is_(filters.is_active))
        if filters.is_verified is not None:
            statement = statement.where(UserModel.is_verified.is_(filters.is_verified))

        if cursor is not None:
            after = _AdminCursor.decode(cursor)
            statement = statement.where(
                # A row-value comparison, so the keyset is one index seek
                # rather than the `(a < x) OR (a = x AND b < y)` expansion a
                # planner cannot always fold back into one.
                tuple_(UserModel.created_at, UserModel.id)
                < tuple_(literal(after.created_at), literal(after.user_id))
            )

        # Over-fetch by one to learn whether a further page exists, rather
        # than issuing a `COUNT(*)` that would scan the table per page.
        rows = (
            (
                await self._session.execute(
                    statement.order_by(UserModel.created_at.desc(), UserModel.id.desc()).limit(
                        limit + 1
                    )
                )
            )
            .scalars()
            .all()
        )

        has_more = len(rows) > limit
        page = list(rows[:limit])
        next_cursor = (
            _AdminCursor(created_at=page[-1].created_at, user_id=page[-1].id).encode()
            if has_more and page
            else None
        )
        return AdminUserPage(
            records=[_to_admin_record(row) for row in page], next_cursor=next_cursor
        )

    async def find_account(self, user_id: UUID) -> "AdminUserRecord | None":
        row = await self._session.get(UserModel, user_id)
        return None if row is None else _to_admin_record(row)


def _to_admin_record(row: UserModel) -> "AdminUserRecord":
    """One row as the published record.

    Field by field rather than by reflection, so adding a column to
    `UserModel` — a credential, a token, anything — does **not** silently
    widen what the admin console can read.
    """
    return AdminUserRecord(
        id=row.id,
        username=row.username,
        email=row.email,
        display_name=row.display_name,
        is_active=row.is_active,
        is_verified=row.is_verified,
        created_at=row.created_at,
    )


def _escape_like(value: str) -> str:
    """Neutralises `LIKE` metacharacters in operator input.

    Without it an operator typing `%` matches every account and one typing
    `_` matches every account of that length — not an injection, but a
    search that silently answers a different question than the one asked.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@dataclass(frozen=True, slots=True)
class _AdminCursor:
    """The keyset position, as an opaque string.

    Base64 of `created_at|id`, which is not security — a caller may decode
    it — but is what stops a client treating it as an offset it may
    arithmetic on. An unparseable cursor raises rather than silently
    starting from the beginning, because "page 4 quietly became page 1" is
    the bug nobody reports.
    """

    created_at: datetime
    user_id: UUID

    def encode(self) -> str:
        raw = f"{self.created_at.isoformat()}|{self.user_id}"
        return urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, cursor: str) -> "_AdminCursor":
        padding = "=" * (-len(cursor) % 4)
        try:
            raw = urlsafe_b64decode(cursor + padding).decode()
            moment, identifier = raw.split("|", 1)
            return cls(created_at=datetime.fromisoformat(moment), user_id=UUID(identifier))
        except (ValueError, TypeError) as exc:
            raise ValidationError("That page cursor could not be read.") from exc
