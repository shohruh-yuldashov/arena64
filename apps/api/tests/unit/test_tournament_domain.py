"""The tournament domain — SPEC-TOURNAMENT §3, §5, §6.

Pure: no database, no clock beyond a fixed instant, no framework. What is
asserted is the four rules that make a bracket trustworthy — the format
this release actually runs, the field size it commits to, the lifecycle a
tournament may take, and the fact that a published pairing does not move.

Trivial getters and enum members are not tested. What is tested is what
would be wrong in a way a player could see.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.modules.game.public import ProductVariant
from app.modules.rating.public import SpeedClass
from app.modules.tournament.domain.brackets import BracketNode
from app.modules.tournament.domain.exceptions import (
    InvalidBracketPosition,
    InvalidCapacity,
    InvalidRoundNumber,
    InvalidTournamentTransition,
    PublishedRoundIsImmutable,
    UnsupportedTournamentFormat,
)
from app.modules.tournament.domain.rounds import RoundStatus, TournamentRound
from app.modules.tournament.domain.tournament import (
    MAX_CAPACITY,
    MIN_CAPACITY,
    Tournament,
    TournamentFormat,
    TournamentStatus,
)

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


def _tournament(**overrides: object) -> Tournament:
    base: dict[str, object] = {
        "id": uuid4(),
        "name": "Sunday Open",
        "format": TournamentFormat.SINGLE_ELIMINATION,
        "variant": ProductVariant.RUSSIAN_8X8,
        "speed_class": SpeedClass.CLASSICAL,
        "rated": True,
        "capacity": 16,
        "created_by": uuid4(),
        "created_at": NOW,
    }
    return Tournament(**{**base, **overrides})  # type: ignore[arg-type]


class TestCreatingATournament:
    def test_it_starts_as_a_draft_that_nobody_can_register_for(self) -> None:
        """§3 — a created tournament is not yet an advertised one.

        `DRAFT` exists so an administrator can assemble a tournament before
        anyone sees it. If creation opened registration, there would be no
        moment in which a mistake is correctable without a player having
        already entered.
        """
        tournament = _tournament()

        assert tournament.status is TournamentStatus.DRAFT
        assert tournament.is_open_for_registration is False

    def test_a_deferred_format_is_refused_at_construction(self) -> None:
        """T-1, and why the enum still carries the others.

        Every format is a member from day one (R-19's argument: adding one
        after tournaments have been recorded makes historical queries about
        format wrong), but only `SINGLE_ELIMINATION` is runnable. The
        refusal is at construction rather than at pairing, so a tournament
        nothing can pair cannot exist.
        """
        for deferred in (
            TournamentFormat.SWISS,
            TournamentFormat.ROUND_ROBIN,
            TournamentFormat.DOUBLE_ELIMINATION,
            TournamentFormat.ARENA,
        ):
            with pytest.raises(UnsupportedTournamentFormat):
                _tournament(format=deferred)

    def test_capacity_is_bounded_at_both_ends(self) -> None:
        """T-2, asserted **on** the boundaries rather than near them.

        Two is the smallest thing that is a tournament rather than a match;
        128 is where this release stops promising the behaviour holds. Both
        edges are legal and both neighbours are not — which is where an
        off-by-one would live.
        """
        assert _tournament(capacity=MIN_CAPACITY).capacity == MIN_CAPACITY
        assert _tournament(capacity=MAX_CAPACITY).capacity == MAX_CAPACITY

        with pytest.raises(InvalidCapacity):
            _tournament(capacity=MIN_CAPACITY - 1)
        with pytest.raises(InvalidCapacity):
            _tournament(capacity=MAX_CAPACITY + 1)


class TestTheLifecycle:
    def test_the_happy_path_runs_draft_to_completed(self) -> None:
        """§3's state machine, walked end to end.

        Each step returns a new tournament, so a caller holding the previous
        one still sees what it held — which is what makes "what was it
        before" a value rather than something to remember to copy.
        """
        tournament = _tournament()

        for status in (
            TournamentStatus.REGISTRATION_OPEN,
            TournamentStatus.REGISTRATION_CLOSED,
            TournamentStatus.IN_PROGRESS,
            TournamentStatus.COMPLETED,
        ):
            tournament = tournament.transitioned_to(status)

        assert tournament.status is TournamentStatus.COMPLETED
        assert tournament.status.is_terminal

    def test_skipping_a_state_and_reopening_a_finished_one_are_both_refused(
        self,
    ) -> None:
        """The two failures the table exists to prevent.

        **Skipping** would start a tournament whose field was never fixed,
        so the bracket would be built from a set that can still change.

        **Reopening** a completed tournament would make the standings
        somebody read stop being the standings — and a finished tournament
        is a permanent competitive record, A-4's class.

        Cancellation is reachable from every live state and from neither
        terminal one, which is the same rule seen from the other side.
        """
        draft = _tournament()

        with pytest.raises(InvalidTournamentTransition):
            draft.transitioned_to(TournamentStatus.IN_PROGRESS)

        completed = (
            draft.transitioned_to(TournamentStatus.REGISTRATION_OPEN)
            .transitioned_to(TournamentStatus.REGISTRATION_CLOSED)
            .transitioned_to(TournamentStatus.IN_PROGRESS)
            .transitioned_to(TournamentStatus.COMPLETED)
        )
        with pytest.raises(InvalidTournamentTransition):
            completed.transitioned_to(TournamentStatus.IN_PROGRESS)
        with pytest.raises(InvalidTournamentTransition):
            completed.transitioned_to(TournamentStatus.CANCELLED)


class TestRounds:
    def test_rounds_are_numbered_from_one(self) -> None:
        """Zero would make "the first round" ambiguous between the value and
        the ordinal — the same reason ply numbers start at one (MT-5)."""
        assert TournamentRound(tournament_id=uuid4(), round_number=1).round_number == 1

        for invalid in (0, -1):
            with pytest.raises(InvalidRoundNumber):
                TournamentRound(tournament_id=uuid4(), round_number=invalid)

    def test_a_published_round_refuses_further_pairing_changes(self) -> None:
        """§6 — the rule that makes a bracket trustworthy.

        Publication is when players can *read* their pairing. Changing it
        afterwards would mean the bracket somebody planned against is not
        the bracket their result is recorded in.

        `require_mutable` is on the round rather than at the call site, so
        the writer A64-019.3 adds inherits the rule instead of having to
        remember it. Asserted through that method, because it is the one a
        future caller will use.
        """
        pending = TournamentRound(tournament_id=uuid4(), round_number=1)
        pending.require_mutable()  # does not raise

        published = pending.published(NOW)

        assert published.status is RoundStatus.PUBLISHED
        assert published.published_at == NOW
        assert published.is_mutable is False
        with pytest.raises(PublishedRoundIsImmutable):
            published.require_mutable()

        # …and a completed round does not reopen — v0.x has no correction.
        completed = published.started(NOW).completed(NOW)
        with pytest.raises(InvalidTournamentTransition):
            completed.started(NOW)


class TestBracketNodes:
    def test_a_node_knows_its_neighbours_by_arithmetic_and_refuses_bad_coordinates(
        self,
    ) -> None:
        """The tree's shape *is* its coordinates — see the module docstring.

        Storing parent and child ids would be three columns that can
        disagree with each other and with the depth; arithmetic cannot. The
        final is `(0, 0)` in every tournament regardless of field size, so
        "who won" is one lookup.

        A position outside `2**depth` is a bracket that cannot be walked,
        and it is refused at construction rather than when advancement runs
        into it.
        """
        tournament_id = uuid4()
        semi = BracketNode(tournament_id=tournament_id, depth=1, position=1)

        assert semi.is_final is False
        assert semi.parent() == (0, 0)
        assert semi.children() == ((2, 2), (2, 3))
        assert BracketNode(tournament_id=tournament_id, depth=0, position=0).parent() is None

        # Depth 1 holds two nodes, so position 2 is outside it.
        with pytest.raises(InvalidBracketPosition):
            BracketNode(tournament_id=tournament_id, depth=1, position=2)
        with pytest.raises(InvalidBracketPosition):
            BracketNode(tournament_id=tournament_id, depth=-1, position=0)

    def test_a_winner_must_have_played_in_the_node(self) -> None:
        """The one bracket error nothing downstream can detect.

        An advancement by somebody who was never in the node produces a
        final between players who never met — and by the time that is
        visible, the rounds beneath it have already been recorded.

        A bye is distinguished from an unfilled node by exactly one seat
        being present, which is what decides who advances without playing.
        """
        light, dark, stranger = uuid4(), uuid4(), uuid4()
        node = BracketNode(
            tournament_id=uuid4(),
            depth=1,
            position=0,
            light_player_id=light,
            dark_player_id=dark,
        )

        assert node.is_playable is True
        assert node.is_bye is False
        assert node.with_winner(light).winner_id == light

        with pytest.raises(InvalidBracketPosition):
            node.with_winner(stranger)

        bye = BracketNode(tournament_id=uuid4(), depth=1, position=0, light_player_id=light)
        assert bye.is_bye is True
        assert bye.is_playable is False
