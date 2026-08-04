"""The bounded rematch policy — SPEC-TOURNAMENT §6c.

Pure: the decision is a function of one attempt's outcome and the seeding
that predates it, so every branch is checkable without a match, a database
or a clock.

Four branches, and the two that matter most are the ones that must *not*
happen — a third attempt, and a winner nobody played for.
"""

from uuid import UUID, uuid4

import pytest

from app.modules.tournament.domain.attempts import (
    MAX_ATTEMPTS,
    AdvancementReason,
    AttemptOutcome,
    PairingAttempt,
    decide,
    rematch_seats,
)
from app.modules.tournament.domain.exceptions import InvalidBracketPosition

HIGHER_SEED = UUID("00000000-0000-0000-0000-000000000001")
LOWER_SEED = UUID("00000000-0000-0000-0000-000000000002")


def _attempt(number: int) -> PairingAttempt:
    return PairingAttempt(
        id=uuid4(),
        pairing_id=uuid4(),
        attempt_number=number,
        match_id=uuid4(),
        light_player_id=HIGHER_SEED,
        dark_player_id=LOWER_SEED,
    )


class TestTheBoundedRematch:
    def test_a_draw_on_the_first_attempt_orders_a_rematch_with_swapped_sides(
        self,
    ) -> None:
        """§1 — nobody advances, and the second game is not the first again.

        The sides swap because the first attempt's seats came from the
        bracket's alternating rule; repeating them would give one player the
        first move in both games of a tie.

        `decided` is `False`, which is the assertion that matters: a
        rematch-due decision must not also carry a winner, or a caller could
        advance somebody *and* create a second match.
        """
        first = _attempt(1)

        decision = decide(
            first,
            outcome=AttemptOutcome.DRAW,
            winner_id=None,
            higher_seed_player_id=HIGHER_SEED,
        )

        assert decision.rematch_due is True
        assert decision.decided is False
        assert decision.reason is None
        assert rematch_seats(first) == (LOWER_SEED, HIGHER_SEED)

    def test_a_second_draw_advances_the_higher_seed_and_orders_no_third_game(
        self,
    ) -> None:
        """§2 — the bound, and the reason it is the seed rather than a coin.

        A third game repeats the question that twice failed to answer it; a
        random winner is a permanent record decided by chance; manual
        adjudication needs an `admin` module that does not exist. The seed
        is the one answer already earned, recorded before anyone played.

        `rematch_due` is `False` — there is no third attempt, and the
        attempt model refuses one structurally as well.
        """
        decision = decide(
            _attempt(MAX_ATTEMPTS),
            outcome=AttemptOutcome.DRAW,
            winner_id=None,
            higher_seed_player_id=HIGHER_SEED,
        )

        assert decision.winner_id == HIGHER_SEED
        assert decision.reason is AdvancementReason.ADJUDICATION
        assert decision.rematch_due is False

    def test_a_decisive_result_advances_the_winner_on_either_attempt(self) -> None:
        """§7 — the ordinary case, checked on both attempts.

        The second attempt is included deliberately: a policy that only
        looked at `is_final_attempt` for draws could still mishandle a
        decisive rematch, and that is the common outcome of one.
        """
        for number in (1, MAX_ATTEMPTS):
            decision = decide(
                _attempt(number),
                outcome=AttemptOutcome.DECISIVE,
                winner_id=LOWER_SEED,
                higher_seed_player_id=HIGHER_SEED,
            )
            assert decision.winner_id == LOWER_SEED
            assert decision.reason is AdvancementReason.PLAYED
            assert decision.rematch_due is False

    def test_a_third_attempt_cannot_be_constructed(self) -> None:
        """§4's bound, enforced by the entity rather than by a caller.

        A service that forgot the limit would produce an attempt the policy
        has no branch for — so the type refuses it, and the database's
        `unique (pairing_id, attempt_number)` plus a range check will refuse
        the row.
        """
        with pytest.raises(InvalidBracketPosition):
            _attempt(MAX_ATTEMPTS + 1)
        with pytest.raises(InvalidBracketPosition):
            _attempt(0)

    def test_a_decisive_attempt_without_a_winner_is_refused(self) -> None:
        """The one malformed input this function can detect.

        A decisive result with no winner would otherwise advance `None` into
        a parent seat, and the bracket would look filled while holding
        nobody.
        """
        with pytest.raises(InvalidBracketPosition):
            decide(
                _attempt(1),
                outcome=AttemptOutcome.DECISIVE,
                winner_id=None,
                higher_seed_player_id=HIGHER_SEED,
            )
