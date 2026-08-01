"""add user search normalisation and trigram indexes

Revision ID: a7c31f5d9e04
Revises: b0f336b06542
Create Date: 2026-08-01 15:10:44.201883

A64-013.1 gives `GET /users/search` the two indexes that keep it off a
sequential scan, and the one function both the indexes and the query are
built on.

## Nothing here changes a table

No column is added, no constraint is altered, and `users.user` holds exactly
what it held before. That is deliberate: everything below is a *query
optimisation artifact*, so it can be dropped and recreated at any time
without touching data, and `infrastructure/models.py` needs no knowledge
that search exists.

The alternative — generated columns holding a normalised copy of the
username and display name — was rejected for that reason. It would have put
a search concern into the table every module reads, doubled the storage of
two of its columns, and required the ORM model to map columns nothing ever
selects.

## The two extensions

`pg_trgm` provides `gin_trgm_ops`, which is what makes `LIKE '%term%'`
index-accelerated rather than the sequential scan it is without one.
`unaccent` provides the accent folding A64-013.1 asks for.

Both are **trusted** extensions in PostgreSQL 13+, so a database owner can
create them without superuser rights — which is what makes this migration
runnable by the same role that runs every other one. `IF NOT EXISTS` because
a shared instance may already have them.

## `users.search_normalise`, and why the wrapper is necessary

    unaccent(lower(normalize(x, NFKC)))

`unaccent(text)` is **STABLE, not IMMUTABLE**, because it reads a
dictionary that could in principle be replaced — and PostgreSQL refuses to
build an index on a non-immutable expression. The two-argument form
`unaccent('unaccent', x)` names the dictionary explicitly, which is what
makes it safe to wrap as `IMMUTABLE`. This is the standard recipe and its
one real caveat is worth stating: if somebody replaces the `unaccent`
dictionary, these indexes must be reindexed, because PostgreSQL will
believe stored values that were computed under the old rules.

`STRICT` so a `NULL` display name normalises to `NULL` rather than an empty
string — `NULL LIKE anything` is `NULL`, so a player with no display name
simply never matches on it, which is the correct behaviour and one fewer
branch in the query.

`PARALLEL SAFE` so a scan that does fall back to sequential can still be
parallelised.

**The expression is the contract.** `SqlAlchemyUserRepository.search` calls
this function on the username, the display name and the term, and the
indexes below are built on the first two of those calls. If the two ever
render differently, PostgreSQL silently plans a sequential scan — no error,
no failing test, just a search that gets slower as the platform grows. That
is what `tests/contract/test_user_search_repository.py`'s plan assertion is
for.

## Why two indexes and not one over a concatenation

An index on `search_normalise(username || ' ' || display_name)` would serve
the `WHERE` in one scan and would make the *ranking* impossible: the query
has to distinguish "the username starts with this" from "the display name
starts with this", and a concatenated index cannot answer either separately.

Two indexes let PostgreSQL `BitmapOr` them for the match and evaluate the
rank on the rows that survive.

## Reversibility

Complete. `downgrade` drops the indexes and the function. The two
extensions are deliberately **not** dropped: they are database-wide
objects, another schema may have come to depend on them, and dropping an
extension out from under an unrelated index is a far worse outcome than
leaving two unused ones behind.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "a7c31f5d9e04"
down_revision: str | Sequence[str] | None = "b0f336b06542"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Kept as one constant so `upgrade` creates exactly what `downgrade` drops,
#: and so the expression the repository must match is written once here.
_NORMALISE_FUNCTION = """
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


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
    op.execute(_NORMALISE_FUNCTION)

    # `username` rather than `username_folded`, even though the folded
    # column already exists and is already `lower(normalize(...))`. The
    # function has to be applied to *something*, and applying it to the raw
    # column keeps one expression for both indexed columns — an index over
    # `search_normalise(username_folded)` would fold twice and would mean
    # the query had to remember which column each side used.
    op.execute(
        "CREATE INDEX ix_user__username_search "
        "ON users.user USING gin (users.search_normalise(username) gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX ix_user__display_name_search "
        "ON users.user USING gin (users.search_normalise(display_name) gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS users.ix_user__display_name_search")
    op.execute("DROP INDEX IF EXISTS users.ix_user__username_search")
    op.execute("DROP FUNCTION IF EXISTS users.search_normalise(text)")
