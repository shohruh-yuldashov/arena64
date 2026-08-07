"""add OTP email verification

Revision ID: d1f6a83c04e2
Revises: c9e4b21f0d73
Create Date: 2026-08-07 11:48:03.774219

A64-021.5H §4, §34. Two columns on `auth.email_verification_tokens`, so a
six-digit code and a verification link are one challenge table and one state
machine.

## Why not a second table

They answer the same question — *does this person control this address* —
and both end at the same `users.user.is_verified`. Two tables would be two
"is there a live challenge" queries, two invalidation rules, and a window in
which somebody holds one of each. The existing partial unique index
(`used_at IS NULL`) already says *one live challenge per account*, and that
is exactly the rule a code needs.

## `token_hash` did not have to change

A link stores `sha256(token)`; a code stores `HMAC(secret, challenge || user
|| code)`. Both are 32 bytes, which is what the column is and what its
`CHECK` already enforces. The distinction is what the value *means*, and
that is `kind`'s job — see `domain.otp.otp_verifier` on why a keyed verifier
rather than a digest.

## No backfill, and no guessed codes

Every existing row is a link, and `server_default 'link'` says so without a
second statement. §34 forbids migrating them into codes, and the reason is
that it is not possible: a code cannot be derived from a link's digest, and
inventing one would issue credentials nobody was sent.

Already-issued links keep working (§13). What changes is what a *new*
registration sends.

## Reversibility

Fully reversible: `downgrade` drops both columns. What is lost is the attempt
count on any live code challenge — so a challenge mid-way through its five
guesses returns to zero. That is a widening of an attacker's window rather
than a loss of data, it lasts only as long as the ten-minute TTL, and it is
stated here rather than mitigated: a downgrade of a credential table cannot
preserve a column the schema no longer has.

Live *link* challenges are unaffected in every direction.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d1f6a83c04e2"
down_revision: str | Sequence[str] | None = "c9e4b21f0d73"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "auth"
_TABLE = "email_verification_tokens"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            "kind",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'link'"),
        ),
        schema=_SCHEMA,
    )
    op.add_column(
        _TABLE,
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        schema=_SCHEMA,
    )


def downgrade() -> None:
    op.drop_column(_TABLE, "attempt_count", schema=_SCHEMA)
    op.drop_column(_TABLE, "kind", schema=_SCHEMA)
