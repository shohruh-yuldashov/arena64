"""In-memory doubles for `rating`'s ports.

The store models the two properties the service depends on: a read that
answers with the unrated state when there is no row (SPEC-RATING §7.5), and
a write that refuses a second adjustment for one `(player, match)` (PR-1).

It does **not** model a transaction. Atomicity is asserted against real
PostgreSQL in `tests/contract/test_rating_persistence.py`; what the unit
tests check is that the service *asks* for one and does not commit when a
save fails.
"""

from uuid import UUID

from app.modules.rating.application.ports import AdjustmentAlreadyApplied
from app.modules.rating.domain.keys import RatingKey
from app.modules.rating.domain.player_rating import PlayerRating, RatingAdjustment


class InMemoryPlayerRatings:
    """`PlayerRatingRepository` as two dicts and a set.

    `fail_on_save` makes the *n*th save raise, which is the only way to
    reach the half-written state §4 forbids — a store that could not fail
    partway could not prove the service rolls back.
    """

    def __init__(self, *, fail_on_save: int | None = None) -> None:
        self.stored: dict[tuple[UUID, RatingKey], PlayerRating] = {}
        self.adjustments: list[RatingAdjustment] = []
        self._applied: set[tuple[UUID, UUID]] = set()
        self._fail_on_save = fail_on_save
        self._saves = 0

    async def load(self, player_id: UUID, *, key: RatingKey) -> PlayerRating:
        return self.stored.get((player_id, key)) or PlayerRating.unrated(player_id, key)

    async def save(self, rating: PlayerRating, adjustment: RatingAdjustment) -> None:
        self._saves += 1
        if self._fail_on_save is not None and self._saves == self._fail_on_save:
            raise RuntimeError("save failed")

        # The unique constraint, modelled: `(player, match)` once, ever.
        key = (adjustment.player_id, adjustment.match_id)
        if key in self._applied:
            raise AdjustmentAlreadyApplied(f"{adjustment.match_id} already applied")
        self._applied.add(key)

        self.stored[(rating.player_id, rating.key)] = rating
        self.adjustments.append(adjustment)
