"""allow non-queue match participants

A64-019.5H. `game.match`'s two ticket columns become nullable, because a
match need not have come from a queue.

    light_ticket_id  NOT NULL -> NULL
    dark_ticket_id   NOT NULL -> NULL

R-25 already made `origin` and `origin_ref` the authoritative statement of
where a match came from. The ticket columns are the **queue's** provenance
and nothing more, and requiring them made every other origin invent one:
A64-019.5 wrote a derived uuid5 for a tournament seat, which recorded that a
queue ticket existed when none ever did — a fabricated fact in a permanent
record (A-4), and one `settlements_for` would have answered questions about.

**The unique indexes are unaffected and are deliberately not touched.**
PostgreSQL treats each `NULL` as distinct, so `uq_match__light_ticket` and
`uq_match__dark_ticket` still refuse two matches claiming one ticket while
letting any number of ticketless matches coexist. Rewriting them as partial
indexes would be the same semantics with a rebuild.

Requiring a ticket for a *queue* match is not dropped, it moves: the pairing
is now checked against `origin` by `CreateMatchRequest.__post_init__` and
`MatchRecord.__post_init__`. It is deliberately **not** a `CHECK` here,
because `origin` carries a `server_default` of `'queue'` and every row
written before A64-019.0 has it — a constraint would be asserting something
about history this migration cannot verify.

Reversible. The downgrade restores `NOT NULL` and therefore **fails while
any ticketless match exists**, which is correct: there is no id to invent
for those rows, and inventing one is the defect this revision removes. An
operator downgrading past it must first decide what happens to non-queue
matches.

Revision ID: b6e2f04d19a7
Revises: d4a91c7e3b62
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b6e2f04d19a7"
down_revision: str | None = "d4a91c7e3b62"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "game"


def upgrade() -> None:
    op.alter_column(
        "match", "light_ticket_id", existing_type=sa.Uuid(), nullable=True, schema=_SCHEMA
    )
    op.alter_column(
        "match", "dark_ticket_id", existing_type=sa.Uuid(), nullable=True, schema=_SCHEMA
    )


def downgrade() -> None:
    # Fails if any match has no ticket — see this file's docstring on why
    # that is the correct behaviour rather than a rough edge.
    op.alter_column(
        "match", "dark_ticket_id", existing_type=sa.Uuid(), nullable=False, schema=_SCHEMA
    )
    op.alter_column(
        "match", "light_ticket_id", existing_type=sa.Uuid(), nullable=False, schema=_SCHEMA
    )
