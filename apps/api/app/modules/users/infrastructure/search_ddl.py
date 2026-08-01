"""The search normalisation function and its two trigram indexes, as DDL
attached to the metadata — A64-013.1.

## Why this exists at all

`users.search_normalise` and the two GIN indexes are created by migration
`a7c31f5d9e04`, which is how they reach a real database. But the contract
suite does not run migrations: `tests/contract/conftest.py` builds its
schema with `Base.metadata.create_all`, because a suite that replayed every
migration would take the whole history to start and would test the
migrations rather than the code.

Without this module, `create_all` would produce a `users.user` table with no
`search_normalise` function, and every search test would fail with
`function users.search_normalise(text) does not exist` — a failure about the
test harness rather than about the code under test.

So the DDL is registered as an `after_create` hook on the metadata. A
`create_all` now produces the same searchable schema a migrated database
has.

## The duplication, stated rather than hidden

These statements also appear, character for character, in migration
`a7c31f5d9e04`. That is duplication and it is the lesser of two evils.

A migration is a **historical record**: it describes what was done to
databases that already exist, and it must keep describing that even after
the application's idea of the schema moves on. Importing this module from
the migration would make a past migration change whenever this file
changes, which is how a replayed history stops reproducing the database it
originally produced. The codebase already accepts the same trade for the
`CHECK` bounds in `models.py`.

What keeps the two honest is not discipline but a test:
`tests/contract/test_user_search_repository.py` asserts that the query
actually uses an index, and it runs against the schema *this* file
produces. A drift between here and the migration surfaces as a plan
regression in CI rather than as a slow endpoint in production.
"""

from typing import Final

from sqlalchemy import DDL, event

from app.database.base import Base

#: `pg_trgm` supplies `gin_trgm_ops`, without which `LIKE '%x%'` cannot use
#: an index at all. `unaccent` supplies the accent folding.
#:
#: Both are *trusted* extensions in PostgreSQL 13+, so the database owner
#: can create them without superuser rights — which is what lets the same
#: role that runs the test suite create them.
_CREATE_EXTENSIONS: Final = (
    "CREATE EXTENSION IF NOT EXISTS pg_trgm",
    "CREATE EXTENSION IF NOT EXISTS unaccent",
)

#: The one normalisation both sides of every comparison go through.
#:
#: `unaccent(text)` is STABLE rather than IMMUTABLE — it reads a dictionary
#: that could be replaced — and PostgreSQL will not index a non-immutable
#: expression. Naming the dictionary explicitly in the two-argument form is
#: what makes the wrapper safe to declare IMMUTABLE.
#:
#: STRICT so a `NULL` display name normalises to `NULL`: `NULL LIKE x` is
#: `NULL`, so a player without one simply never matches on it, and the query
#: needs no branch for the case.
_CREATE_FUNCTION: Final = """
CREATE OR REPLACE FUNCTION users.search_normalise(value text)
RETURNS text
LANGUAGE sql
IMMUTABLE
STRICT
PARALLEL SAFE
AS $$
    SELECT public.unaccent('public.unaccent', lower(normalize(value, NFKC)))
$$
"""

#: One index per searchable column rather than one over a concatenation:
#: the ranking has to tell a username prefix from a display-name prefix, and
#: a concatenated index can answer neither separately. PostgreSQL
#: `BitmapOr`s the two for the match.
_CREATE_INDEXES: Final = (
    (
        "CREATE INDEX IF NOT EXISTS ix_user__username_search "
        "ON users.user USING gin (users.search_normalise(username) gin_trgm_ops)"
    ),
    (
        "CREATE INDEX IF NOT EXISTS ix_user__display_name_search "
        "ON users.user USING gin (users.search_normalise(display_name) gin_trgm_ops)"
    ),
)


def register_search_ddl() -> None:
    """Attaches the DDL to `Base.metadata`'s `after_create`.

    Listeners fire in registration order, which is the order they are
    written below and the only order that works: the function depends on
    both extensions, and the indexes depend on the function.

    On `metadata` rather than on `UserModel.__table__`, because
    `CREATE EXTENSION` is a database-level statement and firing it once per
    table creation would be noise. `after_create` on the metadata runs once,
    after every table exists — including `users.user`, which the indexes
    need.

    Idempotent throughout (`IF NOT EXISTS`, `OR REPLACE`), so a partially
    created schema, a re-run, or a shared instance that already has the
    extensions all converge rather than failing.

    Called from `app.database.models`, which is the module every entrypoint
    imports to register tables — so registration cannot be forgotten by an
    entrypoint that happens not to import `users`.
    """
    for statement in (*_CREATE_EXTENSIONS, _CREATE_FUNCTION, *_CREATE_INDEXES):
        # `DDL.__init__` carries no annotations in SQLAlchemy's stubs, which
        # mypy's strict mode reports as an untyped call. The argument is a
        # module-level `str` constant three lines up, so there is nothing
        # here for a type to protect.
        event.listen(Base.metadata, "after_create", DDL(statement))  # type: ignore[no-untyped-call]
