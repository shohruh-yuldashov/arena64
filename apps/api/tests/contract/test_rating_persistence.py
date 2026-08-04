"""PR-1's exactly-once, against the constraint that provides it.

`tests/unit/` covers what the aggregate decides and what crosses a module
boundary. This covers the one property no in-memory double can model: a
**database** refusing the second insert.

That matters more here than anywhere else on the platform. PR-1 — "a match
affects a rating exactly once, enforced at the database, not in code" — is
the single most important invariant Arena64 has, and the traffic that finds
its window is a relay redelivering `game.match_completed`, which is the
normal case rather than an unlucky one.

Skipped, not failed, when PostgreSQL is unreachable.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.game.public import ProductVariant
from app.modules.rating.application.ports import AdjustmentAlreadyApplied
from app.modules.rating.domain.glicko2 import Glicko2Rating, MatchOutcomeScore
from app.modules.rating.domain.keys import RatingKey, SpeedClass
from app.modules.rating.domain.player_rating import PlayerRating
from app.modules.rating.infrastructure.repositories.player_rating_repository import (
    SqlAlchemyPlayerRatingRepository,
    SqlAlchemyRatingReader,
)

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
KEY = RatingKey(variant=ProductVariant.RUSSIAN_8X8, speed_class=SpeedClass.CLASSICAL)


class TestExactlyOnce:
    async def test_the_same_match_cannot_move_one_rating_twice(
        self, contract_session: AsyncSession
    ) -> None:
        """PR-1, and the reason check-then-insert is not an alternative.

        The first delivery creates the rating row and its adjustment. The
        second — a relay redelivering the same completion — is refused by
        `uq_rating_adjustment__player_match`, translated to
        `AdjustmentAlreadyApplied`, and the handler treats that as success.

        Both deliveries compute the **same** result, because the input is
        the seat snapshot rather than the current rating (PR-3). That is
        what makes the refusal safe rather than a lost update: nothing was
        lost, because the second computation had nothing new to add.

        The row is asserted afterwards, because the failure this guards
        against is not "an error was raised" but "the rating moved twice".
        """
        repository = SqlAlchemyPlayerRatingRepository(contract_session)
        player_id, match_id = uuid4(), uuid4()
        opponent = Glicko2Rating(1500, 200, 0.06)

        first = await repository.load(player_id, key=KEY)
        assert first.games_played == 0

        updated, adjustment = first.applied(
            opponent=opponent, score=MatchOutcomeScore.win(), match_id=match_id, at=NOW
        )
        await repository.save(updated, adjustment)

        stored = await repository.load(player_id, key=KEY)
        assert stored.games_played == 1
        assert stored.rating.value == pytest.approx(updated.rating.value)

        # The redelivery. Recomputed from the same seat snapshot, so the
        # numbers are identical — and refused all the same.
        replayed, replayed_adjustment = first.applied(
            opponent=opponent, score=MatchOutcomeScore.win(), match_id=match_id, at=NOW
        )
        # A `SAVEPOINT`, because PostgreSQL aborts the whole transaction
        # on a constraint violation — without one the read below would fail
        # with `InFailedSQLTransaction` and this test could not check the
        # thing it is about. It is also what the real handler does.
        savepoint = await contract_session.begin_nested()
        with pytest.raises(AdjustmentAlreadyApplied):
            await repository.save(replayed, replayed_adjustment)
        await savepoint.rollback()

        after = await repository.load(player_id, key=KEY)
        assert after.games_played == 1

    async def test_a_player_with_no_row_reads_as_unrated_and_upserts_on_first_write(
        self, contract_session: AsyncSession
    ) -> None:
        """§7.5 — no row until the first rated match.

        The read answers with the starting triple, so "absent" and
        "1500/350/0.06, zero games" are one state seen from two sides. The
        write is an upsert rather than a branch on that absence, because a
        caller that had to know which case it was is one that can get it
        wrong under two matches completing at once.

        The published reader is asserted alongside, because that equivalence
        is what `matchmaking` and `profiles` depend on — and it is served by
        a different class from the repository, so it could disagree.
        """
        player_id = uuid4()
        repository = SqlAlchemyPlayerRatingRepository(contract_session)
        reader = SqlAlchemyRatingReader(contract_session)

        assert await repository.load(player_id, key=KEY) == PlayerRating.unrated(player_id, KEY)

        snapshot = await reader.rating_for(player_id, key=KEY)
        assert (snapshot.value, snapshot.deviation, snapshot.games_played) == (1500.0, 350.0, 0)
        assert snapshot.is_provisional is True

        rating, adjustment = PlayerRating.unrated(player_id, KEY).applied(
            opponent=Glicko2Rating(1400, 100, 0.06),
            score=MatchOutcomeScore.loss(),
            match_id=uuid4(),
            at=NOW,
        )
        await repository.save(rating, adjustment)

        written = await reader.rating_for(player_id, key=KEY)
        assert written.games_played == 1
        assert written.value < 1500.0
        assert written.is_provisional is True
