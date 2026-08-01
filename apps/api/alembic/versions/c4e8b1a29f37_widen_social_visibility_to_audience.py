"""widen social visibility flags to the audience enum

Revision ID: c4e8b1a29f37
Revises: a7c31f5d9e04
Create Date: 2026-08-01 16:22:07.884512

A64-013.2's precondition: three boolean privacy columns become
`users.audience`, the enum database.md §491 has specified since before any
of them existed.

## Why now, and why this conversion is lossless

A boolean cannot express "friends only", and friend requests arrive in this
same task. Doing the widening *before* the friend graph exists is what keeps
it mechanical: with nobody to be a friend of, `true` meant exactly
`everyone` and `false` meant exactly `nobody`, so every existing row
converts to a value that means what it already meant.

After friendships exist, somebody would have to decide what a stored `true`
*should* have been — and there is no correct answer to that question, only
a guess applied to every account on the platform.

## Which three, and why not all five

`show_last_seen`, `show_online_status` and `show_activity`. Not
`show_country` (a self-declared profile field, and not listed in
database.md's audience columns) and not `show_statistics` (UP-5 keeps rated
results visible to the opponents who produced them, so a friends-only match
record is a control the platform could not honour). See
`users/domain/visibility.py`.

## The columns are renamed as well as retyped

`show_last_seen` -> `last_seen_visibility`, and so on. A `show_*` column
holding `'friends'` reads as a boolean at every call site and would be
compared to one eventually; renaming makes the type change impossible to
miss. The rename is free here because the same statement rewrites the
column anyway.

## Reversibility

Complete, and lossy in the direction that matters — which is stated rather
than hidden. `downgrade` maps `everyone -> true` and **both** `friends` and
`nobody` -> `false`, because a boolean has nowhere to put the third value.

That is the safe direction: a player who chose `friends` and is downgraded
becomes hidden rather than published. The alternative — `friends -> true` —
would publish, on a rollback, a field somebody had deliberately narrowed.
A rollback that silently widens a privacy setting is worse than one that
silently narrows it.

## Why `USING` rather than add-backfill-drop

The three-step dance exists to avoid a long `ACCESS EXCLUSIVE` lock on a hot
table. It is not needed here: `ALTER COLUMN ... TYPE ... USING` rewrites the
table once, and `users.user` is small enough at this stage that one rewrite
is measured in milliseconds. When it is not — the threshold is roughly a
million rows — the add-backfill-swap form is the one to reach for, and it is
recorded here so the next person does not have to rediscover why this one
was simple.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c4e8b1a29f37"
down_revision: str | Sequence[str] | None = "a7c31f5d9e04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: `(old boolean column, new audience column)`. Written once so `upgrade`
#: and `downgrade` cannot disagree about which three moved — the failure
#: that leaves a database half-converted and every read path guessing.
_COLUMNS = (
    ("show_last_seen", "last_seen_visibility"),
    ("show_online_status", "online_status_visibility"),
    ("show_activity", "activity_visibility"),
)

#: The platform defaults, as SQL literals. They match
#: `users.domain.privacy`'s constants, and the widening is the same one
#: `VisibilityLevel.of` applies: `last_seen` was `false` and is `nobody`,
#: the other two were `true` and are `everyone`.
_DEFAULTS = {
    "last_seen_visibility": "nobody",
    "online_status_visibility": "everyone",
    "activity_visibility": "everyone",
}

_AUDIENCE = sa.Enum(
    "everyone",
    "friends",
    "nobody",
    name="audience",
    schema="users",
)


def upgrade() -> None:
    # Created explicitly rather than left to the first `ALTER`: a type used
    # by three columns must exist before any of them references it, and
    # `checkfirst=False` makes a second run fail loudly instead of leaving
    # a half-migrated schema.
    _AUDIENCE.create(op.get_bind(), checkfirst=False)

    for old_name, new_name in _COLUMNS:
        # One statement per column: rename, retype and re-default together.
        # `USING` is what makes the conversion explicit rather than relying
        # on a cast PostgreSQL does not have between `boolean` and an enum.
        op.execute(
            f"""
            ALTER TABLE users."user"
                ALTER COLUMN {old_name} DROP DEFAULT,
                ALTER COLUMN {old_name} TYPE users.audience
                    USING (CASE WHEN {old_name} THEN 'everyone' ELSE 'nobody' END)::users.audience,
                ALTER COLUMN {old_name} SET DEFAULT '{_DEFAULTS[new_name]}'::users.audience
            """
        )
        op.alter_column("user", old_name, new_column_name=new_name, schema="users")


def downgrade() -> None:
    for old_name, new_name in _COLUMNS:
        op.alter_column("user", new_name, new_column_name=old_name, schema="users")
        # `everyone -> true`, and **both** other values -> `false`. Lossy on
        # purpose and in the safe direction — see this module's docstring on
        # why a rollback must never widen a privacy setting.
        op.execute(
            f"""
            ALTER TABLE users."user"
                ALTER COLUMN {old_name} DROP DEFAULT,
                ALTER COLUMN {old_name} TYPE boolean
                    USING ({old_name} = 'everyone'),
                ALTER COLUMN {old_name} SET DEFAULT
                    {'true' if _DEFAULTS[new_name] == 'everyone' else 'false'}
            """
        )

    _AUDIENCE.drop(op.get_bind(), checkfirst=False)
