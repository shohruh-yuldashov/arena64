"""Migration rules worth failing a build over — A64-028.3 §21, §22.

## What A64-028.1's P2-3 actually found

It reported three migrations creating an index without `CONCURRENTLY`. The
number is larger and the shape is different: **thirty-nine** migrations call
`op.create_index`, and every one of them builds a blocking index, because
Alembic runs a migration inside a transaction and `CREATE INDEX
CONCURRENTLY` cannot run in one. So "some migrations are unsafe" is the
wrong frame — *all* of them build indexes in a lock, and that is correct.

## Why it is correct

A plain `CREATE INDEX` takes a `SHARE` lock and blocks writes to the table
until the build finishes. On an empty table that is milliseconds. Arena64
has not launched: a production database is created by `alembic upgrade
head` against an empty one, so every historical migration meets empty
tables, always. Rewriting them would change nothing and would risk a schema
that no longer matches revisions already applied elsewhere.

## Where the exposure actually is

A migration that indexes a table it **also creates** is safe by
construction — the table has no rows yet, whenever it runs. A migration that
indexes a table created by some *earlier* migration is the one whose cost
depends on how much data is in that table on the day it runs, and after
launch that is not zero.

Eleven migrations do that. None of them is a problem today and all of them
will have run before the first player exists. The list is here so that the
*next* one is a decision somebody made rather than one nobody noticed.
"""

import re
from pathlib import Path

VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"

#: Migrations that build an index on a table an earlier migration created.
#: Safe because every one runs at `t=0`; declared because the next one might
#: not. See `docs/05-operations/data-reliability.md` for what to do when a
#: migration must index a populated table.
INDEXES_ON_PRE_EXISTING_TABLES: frozenset[str] = frozenset(
    {
        "a2d6f81b4c73_add_queue_ticket_variant.py",
        "b7d24e08f193_add_queue_ticket_time_control.py",
        "c3f8a51b7d24_add_tournament_no_show_adjudication.py",
        "c7a1f2e93b45_add_outbox_retention_indexes.py",
        "c8f1a2d6e930_add_match_origin.py",
        "c92f4b1e7a06_add_queue_cooldown_requeue_and_retention.py",
        "d4a91c7e3b62_add_tournament_pairing_attempts.py",
        "d4f2b83c05a1_index_notifications_for_operations.py",
        "e91b47c05fa3_add_tournament_standings.py",
        "f1a7c3e5b920_create_game_match_and_reservation_deadline.py",
        "f2c8b4e07a91_add_tournament_bracket.py",
    }
)

_INDEXED_BY_KEYWORD = re.compile(r"op\.create_index\((?:[^)]*?)table_name=[\"']([a-z_]+)[\"']")
_INDEXED_POSITIONAL = re.compile(
    r"op\.create_index\(\s*[\"'][a-z_]+[\"']\s*,\s*[\"']([a-z_]+)[\"']"
)
_INDEXED_IN_SQL = re.compile(
    r"CREATE\s+INDEX\s+(?!CONCURRENTLY)[a-z_]+\s+ON\s+(?:[a-z_]+\.)?([a-z_]+)", re.IGNORECASE
)
_CREATED = re.compile(r"op\.create_table\(\s*[\"']([a-z_]+)[\"']")


def _statements(source: str) -> str:
    """The migration's code, without its prose.

    Several of these files discuss `CREATE INDEX` in a docstring — including
    the one that explains why it cannot be concurrent — and a check that
    read the comments would flag every migration that thought about the
    problem hardest.
    """
    return re.sub(r"(?m)#.*$", "", re.sub(r'"""(?:.|\n)*?"""', "", source))


def _indexes_a_pre_existing_table(source: str) -> bool:
    code = _statements(source)
    indexed = (
        set(_INDEXED_BY_KEYWORD.findall(code))
        | set(_INDEXED_POSITIONAL.findall(code))
        | set(_INDEXED_IN_SQL.findall(code))
    )
    return bool(indexed - set(_CREATED.findall(code)))


def test_a_new_migration_cannot_quietly_index_a_populated_table() -> None:
    """The guard P2-3 is worth having.

    Failing here is not "you did something wrong" — it is "say whether that
    table will have rows in it". If it will, the index needs the concurrent
    form and a migration Alembic does not wrap in a transaction.
    """
    actual = {
        migration.name
        for migration in VERSIONS.glob("*.py")
        if _indexes_a_pre_existing_table(migration.read_text())
    }

    assert actual - INDEXES_ON_PRE_EXISTING_TABLES == set(), (
        "This migration indexes a table an earlier migration created, which "
        "takes a SHARE lock on whatever rows it holds. Declare it in "
        "INDEXES_ON_PRE_EXISTING_TABLES if the table is small or empty; see "
        "docs/05-operations/data-reliability.md if it is not."
    )
    assert INDEXES_ON_PRE_EXISTING_TABLES - actual == set(), (
        "A declared migration no longer does this; remove it so the list keeps meaning something."
    )


def test_every_migration_can_be_undone() -> None:
    """A `downgrade` that is `pass` is a deploy nobody can roll back.
    A64-028.1 verified it once by running the whole history down and back
    up; this keeps it true without paying for that every run."""
    stubbed = [
        migration.name
        for migration in VERSIONS.glob("*.py")
        if re.search(
            r"def downgrade\(\)[^:]*:\s*(?:\"\"\"(?:.|\n)*?\"\"\"\s*)?pass\s*$",
            migration.read_text(),
            re.MULTILINE,
        )
    ]

    assert stubbed == []
