"""The `statistics` ORM model — SQLAlchemy 2 typed mappings.

Owned exclusively by this module (database.md DB-03/DB-04). Lives in its own
`statistics` schema, which is what makes architecture.md §16's extraction
seam real, and carries **no foreign key to `users`** — cross-context
references are opaque `player_id` values (DM-06), and a foreign key here
would make the two schemas undeployable apart.

## What this table is, and what follows from that

A **projection** (domain-model.md DM-03, §11.5; database.md C5). Every
column is a count or a peak derived from match history, nothing here is the
system of record for anything, and the whole relation is truncatable and
rebuildable. Three consequences show up directly in the DDL:

  - **No row means "no matches played"**, not "missing data". A player's
    row is created the first time a result is folded in, so an account that
    has never played simply has none. The provider returns
    `NO_MATCHES_PLAYED` for that case rather than treating it as an error.
  - **`player_id` is the whole primary key**, with no surrogate. There is
    exactly one record per player and it has no identity apart from whose
    it is — a UUID of its own would be a second key nothing joins on.
  - **CHECK constraints are arithmetic, not policy.** They restate the
    invariants `PlayerStatistics.__post_init__` enforces, so a rebuild that
    writes an inconsistent row fails at the database rather than surfacing
    as a win rate above 100% (BE-06: the database is the authoritative
    check).

## Two documented deviations from database.md §9.5

**1. The primary key is `player_id`, not `(player_id, rating_category_id)`.**
§9.5 keys this table per rating category, and the reasoning is sound —
"wins by resignation versus wins on time" and per-speed records are what
players actually argue about. A64-012.6 specifies a single flat record and
excludes rating calculation, and there is no `rating_category` reference
table to key against yet (§202 puts it in `reference`, which does not
exist). Per CLAUDE.md's precedence rule the task wins.

The migration path is additive rather than a rewrite: a category column
with a default, the primary key widened, and existing rows become the
"overall" bucket. Worth knowing before the first `match.completed` arrives,
because folding results into a flat record and then splitting them is a
rebuild rather than a migration — and a rebuild is exactly what a
projection is for.

**2. No `player_statistics_termination`, no `head_to_head`, no
`source_watermark`, no `rebuilt_at`.** All three are specified by §9.5 and
all three exist to serve a producer this task explicitly excludes ("do NOT
implement game result processing"). `source_watermark` in particular is the
column that makes a fold idempotent, and it should arrive in the same
change as the thing doing the folding — added now it would be a column
nothing reads and nothing maintains, which is worse than absent because it
looks maintained.

## Why counters rather than a `jsonb` document

The opposite call from `users.user.gameplay_preferences`, deliberately.
These are aggregated across players (a leaderboard is an ordering over
`current_rating`), constrained arithmetically, and updated by a `MERGE`
that increments individual counters — database.md §82 names exactly that
pattern for this table. A preferences blob is read whole by one owner and
queried by nobody; this is the reverse.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Index, Integer, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.mixins import TimestampMixin
from app.modules.statistics.domain.statistics import DEFAULT_RATING

STATISTICS_SCHEMA = "statistics"


class PlayerStatisticsModel(Base, TimestampMixin):
    """The `statistics.player_statistics` row — one per player who has
    played.

    Composes `TimestampMixin` only. Deliberately **not**
    `UUIDPrimaryKeyMixin`: the key is the player, and a surrogate id would
    be an identity this projection does not have.
    """

    __tablename__ = "player_statistics"

    __table_args__ = (
        # Serves the "who is at the top" ordering a future leaderboard
        # projection will build over this column, and costs one index on a
        # table with one row per player. Descending because every reader of
        # a rating column reads it from the top.
        #
        # Not a unique index and not a constraint: two players may hold the
        # same rating, and that is the ordinary case.
        Index(
            "ix_player_statistics__current_rating",
            text("current_rating DESC"),
            postgresql_using="btree",
        ),
        # --- arithmetic invariants (BE-06) ---------------------------------
        #
        # Each restates a check `PlayerStatistics.__post_init__` performs,
        # so a row written by a rebuild script — which does not go through
        # the entity — cannot be inconsistent either. The application check
        # gives a good error at the boundary; this one is the guarantee.
        CheckConstraint(
            "games_played = wins + losses + draws",
            name="counts_sum_to_games_played",
        ),
        CheckConstraint(
            "wins >= 0 AND losses >= 0 AND draws >= 0 AND games_played >= 0",
            name="counts_are_not_negative",
        ),
        # A peak below the present value is a projection that missed an
        # update, not a rounding difference.
        CheckConstraint(
            "highest_rating >= current_rating",
            name="highest_rating_is_a_peak",
        ),
        # `GREATEST(current_streak, 0)` because the streak is signed: a
        # losing run says nothing about the best winning one.
        CheckConstraint(
            "best_win_streak >= GREATEST(current_streak, 0)",
            name="best_win_streak_covers_current",
        ),
        {"schema": STATISTICS_SCHEMA},
    )

    player_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    """DM-06's opaque cross-context reference. **No foreign key** to
    `users.user`: a `statistics` schema that could not be deployed without
    a `users` schema would make architecture.md §16's extraction seam
    decorative. Referential integrity across contexts is an eventual
    concern handled by the erasure flow (database.md §1611 keeps
    `player_id` as a tombstone here on purpose), not by a constraint."""

    games_played: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    wins: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    losses: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    draws: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    current_rating: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text(str(DEFAULT_RATING))
    )
    highest_rating: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text(str(DEFAULT_RATING))
    )

    current_streak: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    """Signed: positive is a winning run, negative a losing one, zero
    neither. One column rather than a length plus a kind, because the two
    cannot disagree if there is only one of them."""

    best_win_streak: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    # `win_rate` is deliberately not a column. It is `wins / games_played`,
    # and a stored copy is a number that can disagree with the four counts
    # beside it the first time a result is corrected or a rebuild runs.
    # Computed on read by `PlayerStatistics.win_rate`.

    created_at: Mapped[datetime]
    updated_at: Mapped[datetime | None]

    def __repr__(self) -> str:
        return (
            f"<PlayerStatisticsModel player_id={self.player_id!r} "
            f"games_played={self.games_played!r}>"
        )
