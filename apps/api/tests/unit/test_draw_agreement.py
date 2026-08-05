"""The draw-agreement transition rules — A64-020.5C-pre §2, §3, §16.

Pure domain: no database, no clock, no transport. What is asserted here is
the *rules* — who may answer an offer, and when a player who has had one
resolved may make another — because those are the two things a transport
must not be allowed to decide and the two a reader cannot verify by
inspection.

Two tests, not eight. §16 caps this phase at ten across every layer, and the
persistence, the concurrency and the wire mapping each need one more than
this file could give them. So each test here carries the assertions
belonging to one *rule* rather than one per line.
"""

from datetime import UTC, datetime

import pytest

from app.core.identifiers import generate_uuid7
from app.modules.engine import CURRENT_ENGINE_VERSION, PlayerSide
from app.modules.game.domain.exceptions import (
    DrawOfferAlreadyPending,
    DrawOfferNotAllowedYet,
    DrawOfferNotPending,
    DrawOfferNotRecipient,
)
from app.modules.game.domain.match_record import MatchRecord, MatchRecordStatus, MatchSeat
from app.modules.game.domain.variants import ProductVariant

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


def _active(*, ply: int = 0) -> MatchRecord:
    """A match being played, at a given ply. Untimed — the clock is
    `test_live_clock.py`'s and nothing here touches it."""
    return MatchRecord(
        pairing_id=generate_uuid7(),
        variant=ProductVariant.RUSSIAN_8X8,
        rated=True,
        engine_version=CURRENT_ENGINE_VERSION,
        light=MatchSeat(
            player_id=generate_uuid7(), queue_ticket_id=generate_uuid7(), accepted_at=NOW
        ),
        dark=MatchSeat(
            player_id=generate_uuid7(), queue_ticket_id=generate_uuid7(), accepted_at=NOW
        ),
        created_at=NOW,
        acceptance_deadline=NOW.replace(minute=1),
        status=MatchRecordStatus.ACTIVE,
        settled_at=NOW,
        ply_number=ply,
    )


class TestOnlyTheRecipientAnswers:
    def test_an_offer_is_answerable_by_the_opponent_and_by_nobody_else(self) -> None:
        """§1: "only the recipient may accept", "only the recipient may
        decline", and at most one offer stands.

        Four refusals and one success, because they are one rule seen from
        five sides and splitting them would be five tests asserting one
        `if`.

        The **offerer answering their own offer** is the case worth having:
        a client that showed an accept button to the player who just
        offered would produce exactly this call, and the domain refusing it
        is what makes that a bug in one place rather than a game ended by a
        misrendered button.
        """
        match = _active(ply=3).offered_draw(PlayerSide.LIGHT, at=NOW)

        # The offerer cannot answer themselves, either way.
        with pytest.raises(DrawOfferNotRecipient):
            match.accepted_draw(PlayerSide.LIGHT, at=NOW)
        with pytest.raises(DrawOfferNotRecipient):
            match.declined_draw(PlayerSide.LIGHT, at=NOW)

        # And cannot stack a second offer on their own.
        with pytest.raises(DrawOfferAlreadyPending):
            match.offered_draw(PlayerSide.LIGHT, at=NOW)
        # Nor can the opponent offer into a standing one — §1's "a
        # competing offer while one is pending must not create two offers".
        with pytest.raises(DrawOfferAlreadyPending):
            match.offered_draw(PlayerSide.DARK, at=NOW)

        # The recipient can, and the match is drawn by agreement.
        drawn = match.accepted_draw(PlayerSide.DARK, at=NOW)
        assert drawn.status is MatchRecordStatus.COMPLETED
        assert drawn.termination_reason is not None
        assert drawn.termination_reason.value == "agreed_draw"
        assert drawn.winner is None
        # The board did not move.
        assert drawn.ply_number == 3

    def test_answering_when_nothing_stands_is_refused(self) -> None:
        """§1: "acceptance after the offer has expired or disappeared is
        rejected safely".

        The race this models is real and common: the recipient's accept is
        in flight when their own move clears the offer. Refused rather than
        applied, because accepting an offer that no longer stands would end
        a game on state neither player could see.
        """
        match = _active(ply=2)
        with pytest.raises(DrawOfferNotPending):
            match.accepted_draw(PlayerSide.DARK, at=NOW)
        with pytest.raises(DrawOfferNotPending):
            match.declined_draw(PlayerSide.DARK, at=NOW)


class TestTheReofferRule:
    def test_a_declined_offerer_waits_for_exactly_one_opponent_move(self) -> None:
        """§3, and the off-by-one it asks to be tested.

        LIGHT offers at ply 3; DARK declines. DARK's next move is ply 4, so
        LIGHT becomes eligible at ply 4 and **not** at ply 3 — one move,
        not zero and not two.

        Asserted at each ply rather than only at the boundary, because an
        off-by-one that let LIGHT re-offer immediately and an off-by-one
        that made them wait two moves are both plausible and only one of
        them is visible from the boundary alone.
        """
        match = _active(ply=3).offered_draw(PlayerSide.LIGHT, at=NOW)
        declined = match.declined_draw(PlayerSide.DARK, at=NOW)

        assert declined.draw_agreement.offer is None
        # Still at ply 3: a decline is not a move.
        assert declined.ply_number == 3
        with pytest.raises(DrawOfferNotAllowedYet):
            declined.offered_draw(PlayerSide.LIGHT, at=NOW)

        # DARK is not restricted — declining costs nothing.
        assert declined.offered_draw(PlayerSide.DARK, at=NOW).draw_agreement.is_pending

        # DARK moves. Ply 4 is DARK's, and LIGHT may offer again.
        after_move = declined.advanced(ply_number=4)
        assert after_move.offered_draw(PlayerSide.LIGHT, at=NOW).draw_agreement.is_pending

    def test_an_offer_the_recipient_moves_past_costs_the_offerer_one_more_move(self) -> None:
        """§3's second case, which is the one with the harder arithmetic.

        LIGHT offers at ply 3. DARK's move at ply 4 clears the offer — and
        because that move is what *resolved* it, LIGHT must wait for one
        move **beyond** it. DARK's next move is ply 6, so LIGHT is refused
        at 4 and at 5 and allowed at 6.

        Ply 5 is the assertion that matters: it is LIGHT's own move, and a
        rule that counted plies rather than the opponent's moves would let
        LIGHT re-offer by moving themselves — which is the spam this exists
        to prevent.
        """
        match = _active(ply=3).offered_draw(PlayerSide.LIGHT, at=NOW)

        # DARK's move at ply 4 clears it — the move path calls this.
        cleared = match.after_move_by(PlayerSide.DARK, at_ply=4).advanced(ply_number=4)
        assert cleared.draw_agreement.offer is None

        with pytest.raises(DrawOfferNotAllowedYet):
            cleared.offered_draw(PlayerSide.LIGHT, at=NOW)

        # LIGHT moves. Their own move does not buy them an offer.
        with pytest.raises(DrawOfferNotAllowedYet):
            cleared.advanced(ply_number=5).offered_draw(PlayerSide.LIGHT, at=NOW)

        # DARK moves again. Now LIGHT may ask.
        allowed = cleared.advanced(ply_number=6)
        assert allowed.offered_draw(PlayerSide.LIGHT, at=NOW).draw_agreement.is_pending

    def test_the_offerers_own_move_leaves_their_offer_standing(self) -> None:
        """§10: only the **recipient's** move clears an offer.

        Playing on while the opponent thinks is the ordinary way a draw is
        offered, so a rule that cleared the offer on any move would make
        the feature unusable in a timed game — the offerer would have to
        choose between asking and staying on the clock.
        """
        match = _active(ply=3).offered_draw(PlayerSide.LIGHT, at=NOW)
        assert match.after_move_by(PlayerSide.LIGHT, at_ply=5).draw_agreement.is_pending
