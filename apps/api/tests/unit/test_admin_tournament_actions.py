"""Audited tournament administration — A64-024.5H.

Tests over the **real** `TournamentAdministrationService` and the **real**
route handlers, with a counting lifecycle in place of `tournament`'s
services. What is asserted is the boundary: that admin drives commands and
decides nothing, that a client cannot name a state or an actor, that every
success is audited in one transaction, and that a refusal is recorded and
commits no mutation.

The transitions themselves belong to `tournament` and are tested there; a
copy of its rules here would be a second transition table.
"""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.core.identifiers import generate_uuid7
from app.modules.admin.application.ports import TournamentLifecycleResult
from app.modules.admin.application.services import AuditRecorder
from app.modules.admin.application.services.tournament_administration_service import (
    TournamentAdministrationService,
)
from app.modules.admin.domain.audit import AuditAction, AuditOutcome, AuditSubjectType
from app.modules.admin.presentation.routers.tournaments import (
    admin_tournaments_router,
    close_registration,
    create_tournament,
    open_registration,
    start_tournament,
)
from app.modules.admin.presentation.schemas.tournament_actions import (
    CreateTournamentRequest,
    TournamentActionResponse,
)
from app.modules.game.public import ProductVariant
from app.modules.rating.public import SpeedClass
from app.modules.tournament.application.ports import TournamentNotStartable
from app.modules.tournament.domain.tournament import TournamentStatus
from tests.fakes.admin_audit import InMemoryAuditEntries
from tests.fakes.presence_redis import MovableClock
from tests.unit.test_admin_authorization import NullUnitOfWork

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


class _Lifecycle:
    """`TournamentLifecycle`, recording what it was asked to do.

    Models the one behaviour admin depends on: a command either returns the
    state it reached or raises what the aggregate raised. It models **no
    transition rule** — those are `tournament`'s, and a fake that enforced
    them here would let this suite pass while the real table disagreed.
    """

    def __init__(self, *, refuse: bool = False, launched: int = 4) -> None:
        self.refuse = refuse
        self.launched = launched
        self.calls: list[str] = []
        self.created_by: UUID | None = None
        self.created_name: str | None = None

    async def create(self, **kwargs: object) -> TournamentLifecycleResult:
        self.calls.append("create")
        self.created_by = kwargs["created_by"]  # type: ignore[assignment]
        self.created_name = kwargs["name"]  # type: ignore[assignment]
        return TournamentLifecycleResult(
            tournament_id=generate_uuid7(), status=TournamentStatus.DRAFT
        )

    async def open_registration(self, tournament_id: UUID) -> TournamentLifecycleResult:
        return self._moved("open_registration", tournament_id, TournamentStatus.REGISTRATION_OPEN)

    async def close_registration(self, tournament_id: UUID) -> TournamentLifecycleResult:
        return self._moved(
            "close_registration", tournament_id, TournamentStatus.REGISTRATION_CLOSED
        )

    async def start(self, tournament_id: UUID) -> TournamentLifecycleResult:
        result = self._moved("start", tournament_id, TournamentStatus.IN_PROGRESS)
        return TournamentLifecycleResult(
            tournament_id=result.tournament_id,
            status=result.status,
            matches_launched=self.launched,
        )

    def _moved(
        self, name: str, tournament_id: UUID, status: TournamentStatus
    ) -> TournamentLifecycleResult:
        self.calls.append(name)
        if self.refuse:
            raise TournamentNotStartable(f"{tournament_id} is not in a state to {name}")
        return TournamentLifecycleResult(tournament_id=tournament_id, status=status)


class _Fixture:
    def __init__(self, **kwargs: object) -> None:
        self.lifecycle = _Lifecycle(**kwargs)  # type: ignore[arg-type]
        self.entries = InMemoryAuditEntries()
        self.unit = NullUnitOfWork()
        self.service = TournamentAdministrationService(
            lifecycle=self.lifecycle,
            audit=AuditRecorder(entries=self.entries, clock=MovableClock(NOW)),
            unit_of_work=self.unit,
        )


class TestEveryCommandIsAudited:
    @pytest.mark.asyncio
    async def test_creation_names_the_administrator_and_records_the_configuration(
        self,
    ) -> None:
        """§4 — `created_by` is the guard's, never the payload's.

        The column is nullable and `None` means "the platform created it".
        A client-supplied value would erase a distinction the schema keeps
        on purpose.
        """
        fixture = _Fixture()
        admin = generate_uuid7()

        created = await fixture.service.create(
            name="Friday Blitz",
            variant=ProductVariant.RUSSIAN_8X8,
            speed_class=SpeedClass.BLITZ,
            capacity=8,
            rated=True,
            registration_deadline=None,
            actor_id=admin,
        )

        assert fixture.lifecycle.created_by == admin
        assert created.status is TournamentStatus.DRAFT

        entry = fixture.entries.rows[0]
        assert entry.action is AuditAction.TOURNAMENT_CREATED
        assert entry.subject_type is AuditSubjectType.TOURNAMENT
        assert entry.actor_id == admin
        assert entry.after["capacity"] == 8
        # The configuration, never the aggregate.
        assert "tournament" not in entry.after
        assert "entrants" not in entry.after

    @pytest.mark.asyncio
    async def test_each_transition_writes_its_own_action_and_commits_once(self) -> None:
        """One audit action per transition, chosen from the state reached
        rather than from the method that was called — so an entry reads the
        same whether the command came from the console or the shell."""
        fixture = _Fixture()
        admin, tournament = generate_uuid7(), generate_uuid7()

        await fixture.service.open_registration(tournament_id=tournament, actor_id=admin)
        await fixture.service.close_registration(tournament_id=tournament, actor_id=admin)
        await fixture.service.start(tournament_id=tournament, actor_id=admin)

        assert [entry.action for entry in fixture.entries.rows] == [
            AuditAction.TOURNAMENT_REGISTRATION_OPENED,
            AuditAction.TOURNAMENT_REGISTRATION_CLOSED,
            AuditAction.TOURNAMENT_STARTED,
        ]
        assert all(entry.outcome is AuditOutcome.SUCCEEDED for entry in fixture.entries.rows)
        # One commit per command — the transition and its entry together.
        assert fixture.unit.commits == 3

    @pytest.mark.asyncio
    async def test_starting_records_what_it_launched(self) -> None:
        """A tournament that reached `in_progress` and launched nothing is a
        bracket that did not materialise — the number is how an operator
        sees that afterwards."""
        fixture = _Fixture(launched=8)
        await fixture.service.start(tournament_id=generate_uuid7(), actor_id=generate_uuid7())

        assert fixture.entries.rows[0].after["matches_launched"] == 8

    @pytest.mark.asyncio
    async def test_a_transition_that_launches_nothing_omits_the_count(self) -> None:
        """A field that is always zero on three of four entries is one a
        reader learns to ignore on the fourth."""
        fixture = _Fixture()
        await fixture.service.open_registration(
            tournament_id=generate_uuid7(), actor_id=generate_uuid7()
        )
        assert "matches_launched" not in fixture.entries.rows[0].after


class TestRefusalsAreRecordedAndCommitNothing:
    @pytest.mark.asyncio
    async def test_a_refused_transition_writes_a_failed_entry_and_rolls_back(self) -> None:
        """A64-024.6's policy, applied unchanged.

        An authenticated administrator asked for a move the tournament was
        not in a state to make. The mutation rolled back; the attempt is on
        the record, in its own transaction, because there is nothing left
        for it to be atomic with.
        """
        fixture = _Fixture(refuse=True)
        admin, tournament = generate_uuid7(), generate_uuid7()

        with pytest.raises(TournamentNotStartable):
            await fixture.service.start(tournament_id=tournament, actor_id=admin)

        assert fixture.unit.rollbacks == 1
        entry = fixture.entries.rows[-1]
        assert entry.action is AuditAction.TOURNAMENT_TRANSITION_REFUSED
        assert entry.outcome is AuditOutcome.FAILED
        assert entry.actor_id == admin
        assert entry.after["expected_from"] == TournamentStatus.REGISTRATION_CLOSED.value
        # Exactly one commit, and it is the refusal's.
        assert fixture.unit.commits == 1

    @pytest.mark.asyncio
    async def test_a_refusal_writes_no_success_entry(self) -> None:
        """The invariant a trail depends on: no entry claims a transition
        that did not happen."""
        fixture = _Fixture(refuse=True)
        with pytest.raises(TournamentNotStartable):
            await fixture.service.open_registration(
                tournament_id=generate_uuid7(), actor_id=generate_uuid7()
            )

        assert not [
            entry for entry in fixture.entries.rows if entry.outcome is AuditOutcome.SUCCEEDED
        ]

    @pytest.mark.asyncio
    async def test_a_failing_audit_rolls_the_transition_back(self) -> None:
        """§13's core invariant, from the other side.

        The lifecycle command succeeded and the audit write failed. Nothing
        commits — so there is no transition anybody can act on that the
        trail does not know about.
        """

        class _BrokenEntries:
            async def append(self, entry: object) -> object:
                raise RuntimeError("the audit table is unreachable")

        fixture = _Fixture()
        fixture.service = TournamentAdministrationService(
            lifecycle=fixture.lifecycle,
            audit=AuditRecorder(entries=_BrokenEntries(), clock=MovableClock(NOW)),  # type: ignore[arg-type]
            unit_of_work=fixture.unit,
        )

        with pytest.raises(RuntimeError, match="unreachable"):
            await fixture.service.start(tournament_id=generate_uuid7(), actor_id=generate_uuid7())

        assert fixture.unit.commits == 0
        assert fixture.unit.rollbacks >= 1


class TestWhatTheSurfaceCannotDo:
    def test_no_request_model_carries_a_state_an_actor_or_a_winner(self) -> None:
        """§20 — the transition is the route, not a field.

        A `status` here would be a caller naming a state; a `winner` or a
        `pairing` would be a caller editing a bracket. None has a field to
        arrive in, so no payload could express one.
        """
        forbidden = {
            "status",
            "state",
            "created_by",
            "actor_id",
            "id",
            "tournament_id",
            "winner",
            "winner_id",
            "pairings",
            "bracket",
            "standings",
            "entrants",
            "format",
        }
        assert not forbidden & set(CreateTournamentRequest.model_fields)

    def test_the_response_carries_no_bracket_or_entrant_data(self) -> None:
        forbidden = {"pairings", "bracket", "standings", "entrants", "winner"}
        assert not forbidden & set(TournamentActionResponse.model_fields)

    def test_every_route_is_guarded_and_no_command_is_a_status_write(self) -> None:
        """§28 — the reachability proof, plus the shape check.

        Every mutation is a `POST` to a named command. A `PATCH` or a `PUT`
        would be a route that takes a state, which is the thing this design
        exists to make unrepresentable.
        """
        from app.modules.admin.presentation.dependencies import require_admin

        assert admin_tournaments_router.routes
        for route in admin_tournaments_router.routes:
            methods: set[str] = getattr(route, "methods", set())
            assert methods <= {"GET", "HEAD", "POST"}, methods

            dependant = getattr(route, "dependant", None)
            assert dependant is not None
            assert require_admin in {sub.call for sub in dependant.dependencies}, getattr(
                route, "path", route
            )

    def test_there_is_no_cancel_publish_or_entrant_route(self) -> None:
        """The three actions this task refused, asserted as absent.

        Each is refused for its own reason — `specs/admin.md` §6.15 — and a
        route that appeared later without that reasoning is what this
        catches.
        """
        paths = {getattr(route, "path", "") for route in admin_tournaments_router.routes}
        for absent in ("cancel", "publish", "rounds", "entrants", "disqualify", "withdraw"):
            assert not any(absent in path for path in paths), absent


class _Headers:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}


class _Identity:
    def __init__(self, account_id: UUID) -> None:
        self.id = account_id


class TestTheHttpSurface:
    @pytest.mark.asyncio
    async def test_the_handlers_pass_the_guards_actor_and_never_cache(self) -> None:
        """The actor reaching the service is `CurrentAdmin.id` — the one
        value a client must not be able to choose."""
        fixture = _Fixture()
        admin = generate_uuid7()
        headers = _Headers()

        created = await create_tournament(
            CreateTournamentRequest(
                name="Friday Blitz",
                variant=ProductVariant.RUSSIAN_8X8,
                speed_class=SpeedClass.BLITZ,
                capacity=8,
            ),
            _Identity(admin),  # type: ignore[arg-type]
            fixture.service,
            headers,  # type: ignore[arg-type]
        )

        assert created.status is TournamentStatus.DRAFT
        assert fixture.lifecycle.created_by == admin
        assert headers.headers["Cache-Control"] == "no-store"

    @pytest.mark.asyncio
    async def test_each_command_route_reaches_its_own_lifecycle_call(self) -> None:
        """Three routes, three commands, in order — so a copy-paste that
        pointed `close` at `open` fails here rather than in production."""
        fixture = _Fixture()
        tournament = generate_uuid7()
        admin = _Identity(generate_uuid7())

        for handler in (open_registration, close_registration, start_tournament):
            await handler(
                tournament,
                admin,  # type: ignore[arg-type]
                fixture.service,
                _Headers(),  # type: ignore[arg-type]
            )

        assert fixture.lifecycle.calls == [
            "open_registration",
            "close_registration",
            "start",
        ]
