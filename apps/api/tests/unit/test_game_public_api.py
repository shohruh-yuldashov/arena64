"""`game.public` — the boundary `matchmaking` reaches `game` through.

A64-015.2 gives the package its first contents, and what is tested here is
mostly what it *refuses* to publish: the engine plays three variants and a
player may choose one.

The other half is the wiring rule. Every engine collaborator is stateless
(`specs/game-engine/audit.md` §14), so one instance serves the process —
and a test that the accessor returns the same object is what stops the
first pairing worker building its own.
"""

import inspect

import pytest

from app.modules.engine import CURRENT_ENGINE_VERSION, BoardVariant
from app.modules.game import public as game_public
from app.modules.game.public import (
    GameEngineServices,
    ProductVariant,
    VariantNotOffered,
    board_variant_of,
    engine_services,
    game_engine_version,
    is_offered,
    require_offered,
    variant_catalogue,
)


class TestTheCatalogue:
    def test_russian_8x8_is_selectable(self) -> None:
        assert ProductVariant.RUSSIAN_8X8 in variant_catalogue()

    def test_english_8x8_is_not_selectable(self) -> None:
        """It is a testing and configuration fixture, not a product —
        `specs/game-engine/audit.md` §9. The engine plays it, the perft
        oracle depends on it, and no player is offered it."""
        assert not is_offered(BoardVariant.ENGLISH_8X8.value)

    def test_international_10x10_is_not_selectable_either(self) -> None:
        """Its draw rules are a placeholder, not a claim (§7.7). Offering it
        would ship Russian's rules under another name."""
        assert not is_offered(BoardVariant.INTERNATIONAL_10X10.value)

    def test_an_unsupported_variant_is_refused_by_name(self) -> None:
        with pytest.raises(VariantNotOffered, match="english_8x8"):
            require_offered("english_8x8")

    def test_the_refusal_lists_what_is_offered(self) -> None:
        """A client that guessed wrong needs the list, and there is nothing
        sensitive in it."""
        with pytest.raises(VariantNotOffered, match="russian_8x8"):
            require_offered("draughts_64")

    def test_an_offered_variant_comes_back_as_a_product_variant(self) -> None:
        assert require_offered("russian_8x8") is ProductVariant.RUSSIAN_8X8

    def test_the_catalogue_order_is_stable(self) -> None:
        assert variant_catalogue() == variant_catalogue()


class TestTheTwoEnumsAreOneIdentifier:
    def test_every_product_variant_is_a_board_variant(self) -> None:
        """Not two identifiers for one rule set — a stored ticket, a wire
        payload and an engine call all spell it the same way."""
        for variant in ProductVariant:
            assert variant.value in {member.value for member in BoardVariant}

    def test_the_conversion_is_total(self) -> None:
        for variant in ProductVariant:
            assert board_variant_of(variant).value == variant.value

    def test_the_product_catalogue_is_a_strict_subset(self) -> None:
        """Strict, and that is the point: a `ProductVariant` that covered
        every `BoardVariant` would be a distinction with no content."""
        offered = {variant.value for variant in ProductVariant}
        playable = {variant.value for variant in BoardVariant}

        assert offered < playable


class TestEngineVersion:
    def test_it_is_the_engine_s_own_constant(self) -> None:
        """Read from one place, never derived from a date or a build
        (GE-55). A future match request stamps it (AD-15)."""
        assert game_engine_version() == CURRENT_ENGINE_VERSION

    def test_it_serialises_to_an_integer(self) -> None:
        assert isinstance(game_engine_version().as_primitive(), int)


class TestStatelessCollaboratorsAreSharedOnce:
    def test_the_accessor_returns_one_instance(self) -> None:
        """Wired once per process. A route handler or a queue service
        constructing its own is what §3 of A64-015.2 forbids and what a
        composition root exists to prevent."""
        assert engine_services() is engine_services()

    def test_every_collaborator_is_shared_too(self) -> None:
        one, other = engine_services(), engine_services()

        assert one.generator is other.generator
        assert one.validator is other.validator
        assert one.applier is other.applier
        assert one.terminal is other.terminal
        assert one.draw_rules is other.draw_rules
        assert one.replay is other.replay

    def test_the_bundle_is_immutable(self) -> None:
        """Shared state that could be reassigned is mutable global state
        however stateless its members are."""
        with pytest.raises(AttributeError):
            engine_services().generator = None  # type: ignore[misc, assignment]

    def test_a_fresh_bundle_wires_the_dependency_graph(self) -> None:
        """A validator needs a generator, an applier needs a validator, and
        the replay engine needs all three — built once, in that order."""
        services = GameEngineServices.create()

        assert isinstance(services, GameEngineServices)
        assert services.applier is not None
        assert services.replay is not None


class TestTheSurfaceIsDeliberate:
    PUBLISHED = {
        # A64-015.2 — which rule sets a player may choose, and the shared
        # engine collaborators.
        "GameEngineServices",
        "ProductVariant",
        "VariantNotOffered",
        "board_variant_of",
        "engine_services",
        "game_engine_version",
        "is_offered",
        "require_offered",
        "variant_catalogue",
        # A64-015.3 — the "creates match" command architecture.md §7 draws
        # from `matchmaking`. A command `game` accepts, never a type it
        # hands out, which is how the edge coexists with R-3.
        "CreateMatchRequest",
        "CreateMatchResult",
        "MatchCreationRefused",
        "MatchCreationUseCase",
        "MatchParticipant",
        "PlayerSide",
        "SeatRating",
        "TerminationReason",
        # A64-015.4 — the second half of that same edge. Two commands
        # `game` accepts (accept, decline), one view it hands out, the
        # sweep the reconciler drives, and the two reads the pairing scan
        # and the recovery job need.
        #
        # `MatchCreationUnavailable` and `UnavailableMatchCreation` are
        # **gone**, and their absence is the acceptance criterion rather
        # than a tidy-up: A64-015.3 shipped an adapter that refused every
        # request because `game` could not store a match, and a build that
        # still published it would be one where the refusal is still
        # reachable.
        "AcceptanceWindowClosed",
        "MatchAcceptanceExpiryUseCase",
        "MatchAcceptanceUseCase",
        "MatchNotFound",
        "MatchNotPending",
        "MatchRecordStatus",
        "NotAMatchParticipant",
        "PairingReconciliationReader",
        "PairingSettlement",
        "PendingMatchView",
        "RecentOpponentReader",
        # A64-015.5 — the events downstream modules subscribe to (R-3 says
        # they must, and a subscriber that cannot name one has to match on a
        # string literal), the two measurements that inform a `matchmaking`
        # setting, and the sweep that lets go of pairings which never became
        # games.
        "MATCH_AGGREGATE",
        "MATCH_ANSWER_LATENCY",
        "MATCH_OUTCOMES",
        "AbandonedMatchRetention",
        "AnswerLatency",
        "MatchAcceptanceExpired",
        "MatchAcceptedByPlayer",
        "MatchActivated",
        "MatchCreated",
        "MatchDeclined",
        "MatchOutcome",
        # A64-016.2 — the gateway's one question. A room admits a socket
        # only if `game` says the player is in the match, and the reader
        # published for it has exactly one method: a transport tier that was
        # compromised could enumerate nothing and change nothing.
        "MatchRoster",
        "MatchRosterReader",
        # A64-016.3 — the live-play boundary. The gateway holds
        # `SubmitMoveUseCase` and can do nothing else: it cannot read a
        # position, enumerate matches or resign one. The four failures are
        # published because a consumer must catch *the* error the service
        # raises, not a lookalike.
        "AppliedMove",
        "SubmitMoveRequest",
        "SubmitMoveResult",
        "SubmitMoveUseCase",
        "IllegalMoveSubmitted",
        "MatchNotActive",
        "NotYourTurn",
        "StaleMatchState",
        # A64-016.4 — the two events a played game produces. `MoveApplied`
        # is the platform's highest-volume event by a wide margin (one per
        # ply); `MatchCompleted` is the first that says a *game* happened
        # rather than a pairing, and is what `rating` and `statistics` will
        # key on.
        "MatchCompleted",
        "MoveApplied",
        # A64-016.5 — the clock. `ClockView` is what a client renders; a
        # `ClockState` is a domain value and stays withheld, so the gateway
        # can show a countdown and cannot construct one.
        "ClockExpired",
        "ClockView",
        # A64-016.6 — the authoritative snapshot a reconnect resumes from.
        # The gateway must not assemble one from `game` internals, so the
        # projection crosses as primitives and `Position` stays withheld.
        "MatchSnapshot",
        "MatchSnapshotReader",
        "PlacedPiece",
    }

    def test_nothing_is_published_by_accident(self) -> None:
        """R-1 makes this the only door into `game`. A name that arrives
        here without a decision is a dependency a consumer can take that
        nobody intended."""
        assert set(game_public.__all__) == self.PUBLISHED

    def test_the_match_aggregate_is_not_published(self) -> None:
        """R-3: the modules that care about matches subscribe to events
        rather than calling in. `matchmaking`'s edge points the other way —
        it *asks* `game` to create a match, and asks again to accept one.

        `MatchRecord` joins the withheld list in A64-015.4. Its *status* is
        published, because a client must be able to tell "still waiting"
        from "your opponent declined"; the record itself is not, because a
        consumer holding it could activate a match nobody accepted.
        """
        for withheld in (
            "Match",
            "MatchStatus",
            "MatchResult",
            "MatchRecord",
            "MatchSeat",
            "MoveRecord",
            "ReplayData",
        ):
            assert withheld not in game_public.__all__

    def test_the_adapter_that_refused_every_match_is_gone(self) -> None:
        """A64-015.4's acceptance criterion, as a test rather than a note.

        `UnavailableMatchCreation` existed because `game` had no table, and
        it refused every pairing on purpose. Removing it from the *wiring*
        is not enough — a class that still exists is one somebody can wire
        back — so the type and its refusal are deleted outright.
        """
        assert not hasattr(game_public, "UnavailableMatchCreation")
        assert not hasattr(game_public, "MatchCreationUnavailable")

    def test_every_published_name_resolves(self) -> None:
        for name in game_public.__all__:
            assert hasattr(game_public, name), name

    def test_the_module_imports_nothing_from_matchmaking(self) -> None:
        """The edge is one-directional. `game` importing a queue type would
        make the two modules mutually dependent, and no contract in
        `.importlinter` could then express either direction.

        Checked against imports rather than against the prose, which
        mentions `matchmaking` by name to explain why the package exists.
        """
        source = inspect.getsource(game_public)

        assert "app.modules.matchmaking" not in source
