"""The audit enums, as the **migrations** define them — A64-024 hardening.

This suite exists because three phases shipped an enum member without a
migration and every test passed.

## What went wrong, and why nothing saw it

`admin.audit_action` was created by `b2d5f8a41c70` with two labels.
A64-024.6, A64-024.7 and A64-024.5H each added members to the Python
`AuditAction` and none added `ALTER TYPE … ADD VALUE`. Against a migrated
database every one of those mutations died at `flush()`:

    invalid input value for enum admin.audit_action: "tournament.create"

Two things hid it, and both are worth naming because they will hide the
next one too:

1. **Alembic autogenerate does not detect enum value additions.** It
   compares tables, indexes and constraints; a new label on an existing
   type is none of those. `7989d2c17008` recorded this in 2026 and it was
   true again.
2. **The contract fixture builds its schema with `Base.metadata.create_all`.**
   That creates the type from the *current* Python enum, so the suite
   always ran against a database that had every value. A test that asked
   the database would have agreed with the code and learned nothing.

## Why this reads migrations instead of a schema

The deployed schema is whatever the migration chain produces, so that chain
is the thing the Python enum must agree with. Reading `pg_enum` from a
`create_all` database compares the code to itself.

So these tests parse `alembic/versions/` for the labels each enum is ever
given — the initial `postgresql.ENUM(...)` and every subsequent
`ADD VALUE` — and compare that set to the Python enum. A member added
without a migration fails here, immediately, with the name of the value.

The scan is deliberately literal-minded. A migration that added a label in
some way it does not recognise would make this fail rather than pass, which
is the safe direction: the fix is to teach the scanner, and a false alarm
costs a minute where a false pass cost three features.
"""

import re
from enum import Enum
from pathlib import Path

import pytest

from app.modules.admin.domain.audit import (
    AuditAction,
    AuditActorType,
    AuditOutcome,
    AuditSubjectType,
)

#: Where the chain lives. Resolved from this file so the suite does not
#: depend on the working directory a runner happens to use.
VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"


def _labels_from_migrations(qualified: str) -> set[str]:
    """Every label the migration chain ever gives `schema.type`.

    Two shapes, because the chain uses two:

        postgresql.ENUM("a", "b", name="audit_action", schema="admin")
        ALTER TYPE admin.audit_action ADD VALUE IF NOT EXISTS 'x'

    The first is read by locating the `name=` and `schema=` pair and taking
    the double-quoted strings that precede it; the second by matching the
    statement directly.
    """
    schema, name = qualified.split(".", 1)
    found: set[str] = set()

    for path in VERSIONS.glob("*.py"):
        source = path.read_text()
        if qualified not in source and f'name="{name}"' not in source:
            continue

        # `ALTER TYPE … ADD VALUE` — the additive shape.
        for statement in re.finditer(
            rf"ALTER TYPE\s+{re.escape(qualified)}\s+ADD VALUE(?:\s+IF NOT EXISTS)?\s+'([^']+)'",
            source,
        ):
            found.add(statement.group(1))

        # `postgresql.ENUM(...)` — the creating shape. The labels are the
        # double-quoted strings between the call and its `name=`.
        #
        # `[^)]*?` rather than `.*?`: a file declares several enums, and a
        # dot-matches-all span would run from the *first* call to this
        # type's `name=` and collect every other type's labels on the way.
        for creation in re.finditer(
            rf"postgresql\.ENUM\(([^)]*?)name=\"{re.escape(name)}\"", source, re.DOTALL
        ):
            found.update(re.findall(r'"([^"]+)"', creation.group(1)))

    return found


@pytest.mark.parametrize(
    ("enum", "qualified"),
    [
        (AuditAction, "admin.audit_action"),
        (AuditSubjectType, "admin.audit_subject_type"),
        (AuditOutcome, "admin.audit_outcome"),
        (AuditActorType, "admin.audit_actor_type"),
    ],
    ids=["action", "subject_type", "outcome", "actor_type"],
)
def test_every_member_reaches_the_database_through_a_migration(
    enum: type[Enum], qualified: str
) -> None:
    """The check that was missing.

    A member added to the Python enum without a migration is a mutation
    that raises the first time somebody performs it in a real deployment —
    and passes every test, because the suite's schema is built from the
    same Python enum.
    """
    declared = {member.value for member in enum}
    migrated = _labels_from_migrations(qualified)

    missing = declared - migrated
    assert not missing, (
        f"{qualified}: {sorted(missing)} exist in Python and in no migration. "
        f"Add `ALTER TYPE {qualified} ADD VALUE IF NOT EXISTS '…'` — autogenerate "
        f"will not do it for you."
    )


def test_the_scanner_finds_the_original_and_the_added_labels() -> None:
    """A guard on the guard.

    If the scan silently matched nothing, every assertion above would pass
    vacuously — `set() - set()` is empty and so is `declared - {}` only when
    `declared` is empty, but a regex change could easily make it return
    everything or a stale subset. Asserting one label from each shape keeps
    the parser honest.
    """
    actions = _labels_from_migrations("admin.audit_action")

    # From `b2d5f8a41c70`'s `postgresql.ENUM(...)`.
    assert "admin.role.grant" in actions
    # From `e5a1c94f27d8`'s `ADD VALUE`.
    assert "tournament.create" in actions


def test_no_migration_declares_a_label_the_code_cannot_decode() -> None:
    """The drift in the other direction, and it is not symmetric.

    A label in the database that Python does not know is not a write
    failure — it is a **read** failure: `AuditAction(row.action)` raises
    when the console lists the trail. On an append-only table that row can
    never be corrected, so the value has to be removed from the migration
    before it is deployed rather than after.
    """
    declared = {member.value for member in AuditAction}
    unknown = _labels_from_migrations("admin.audit_action") - declared

    assert not unknown, (
        f"admin.audit_action: {sorted(unknown)} are migrated and unknown to "
        f"`AuditAction`. A row carrying one cannot be decoded, and "
        f"`admin.audit_entry` is append-only — it cannot be corrected later."
    )


#: The console's action-label map, which is the only other place every
#: `AuditAction` value has to appear.
#: `parents[3]` is `apps/` — this file is `apps/api/tests/contract/…`.
CONSOLE_VOCABULARY = (
    Path(__file__).resolve().parents[3] / "admin" / "src" / "features" / "audit" / "vocabulary.ts"
)


def test_every_action_has_a_label_in_the_admin_console() -> None:
    """The third place an action must reach, and the third to drift.

    An action with no entry in `AUDIT_ACTION_LABELS` renders as its raw
    identifier — `tournament.create` rather than a sentence. That is the
    fallback working as designed and is not a failure, which is exactly why
    it went unnoticed for a whole phase: five tournament actions shipped
    with no label and the console showed identifiers to operators.

    It is also not only cosmetic. The audit console builds its action filter
    from this map, so an action with no label is one nobody can filter by.

    This test reaches across the workspace, which is unusual and deliberate:
    the two facts live in two languages and there is no third place that
    holds both. A generated contract would be a fourth thing to keep in
    step.
    """
    if not CONSOLE_VOCABULARY.exists():  # pragma: no cover — backend-only checkout
        pytest.skip("apps/admin is not present in this checkout")

    labelled = set(re.findall(r'"([a-z][a-z0-9_.]*)":\s*"audit\.', CONSOLE_VOCABULARY.read_text()))
    missing = {member.value for member in AuditAction} - labelled

    assert not missing, (
        f"{sorted(missing)} have no entry in AUDIT_ACTION_LABELS. They render as raw "
        f"identifiers and cannot be filtered by in the audit console."
    )
