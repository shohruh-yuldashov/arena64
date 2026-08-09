"""The operator dashboard — A64-024.9.

Tests over the **real** `DashboardService` and the **real** route handler,
with counting fakes in place of storage. What is asserted is the property a
dashboard most easily loses: that its cost is fixed. Every read below is
counted, and the counts must not move when the data grows.

The index plans behind those reads are PostgreSQL's and are asserted in
`tests/contract/test_admin_dashboard.py`, where a fake cannot agree with
itself about them.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from app.core.identifiers import generate_uuid7
from app.modules.admin.application.services.dashboard_service import (
    DAY,
    RECENT_AUDIT_LIMIT,
    WEEK,
    DashboardService,
)
from app.modules.admin.domain.audit import (
    AuditAction,
    AuditActorType,
    AuditEntry,
    AuditOutcome,
    AuditSubjectType,
)
from app.modules.admin.presentation.routers.dashboard import (
    admin_dashboard_router,
    read_dashboard,
)
from app.modules.admin.presentation.schemas.dashboard import (
    AccountsCard,
    ActivityEntry,
    AttentionCard,
    DashboardResponse,
    MatchesCard,
    TournamentsCard,
)
from app.modules.game.public.administration import AdminLiveMatchSummary
from app.modules.notifications.public import AdminDeliveryHealth
from app.modules.tournament.public.administration import AdminLiveTournamentSummary
from app.modules.users.public import AdminUserRecord
from app.modules.users.public.administration import AdminAccountSummary
from tests.fakes.admin_audit import InMemoryAuditEntries
from tests.fakes.presence_redis import MovableClock

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


class _Accounts:
    """The users port, counting every read it is asked for."""

    def __init__(self, *, summary: AdminAccountSummary | None = None) -> None:
        self.summary = summary or AdminAccountSummary(
            registered_last_day=3, registered_last_week=11
        )
        self.summary_calls = 0
        self.windows: list[tuple[datetime, datetime]] = []
        self.batches: list[int] = []
        self.known: dict[UUID, str] = {}

    async def account_summary(
        self, *, since_day: datetime, since_week: datetime
    ) -> AdminAccountSummary:
        self.summary_calls += 1
        self.windows.append((since_day, since_week))
        return self.summary

    async def accounts_by_ids(self, user_ids: list[UUID]) -> dict[UUID, AdminUserRecord]:
        self.batches.append(len(set(user_ids)))
        wanted = set(user_ids)
        return {
            user_id: AdminUserRecord(
                id=user_id,
                username=name,
                email=f"{name}@example.com",
                display_name=None,
                is_active=True,
                is_verified=True,
                created_at=NOW,
            )
            for user_id, name in self.known.items()
            if user_id in wanted
        }

    async def list_accounts(self, **_: object) -> None:  # pragma: no cover
        raise AssertionError("the dashboard must not page accounts to count them")

    async def find_account(self, user_id: UUID) -> None:  # pragma: no cover
        raise AssertionError("the dashboard must not read accounts one at a time")


class _Matches:
    def __init__(self, summary: AdminLiveMatchSummary | None = None) -> None:
        self.summary = summary or AdminLiveMatchSummary(active=4, awaiting_acceptance=2)
        self.calls = 0

    async def live_match_summary(self) -> AdminLiveMatchSummary:
        self.calls += 1
        return self.summary

    async def list_matches(self, **_: object) -> None:  # pragma: no cover
        raise AssertionError("the dashboard must not page matches to count them")

    async def find_match(self, match_id: UUID) -> None:  # pragma: no cover
        raise AssertionError("the dashboard must not read matches one at a time")


class _Tournaments:
    def __init__(self, summary: AdminLiveTournamentSummary | None = None) -> None:
        self.summary = summary or AdminLiveTournamentSummary(registration_open=1, in_progress=2)
        self.calls = 0

    async def live_tournament_summary(self) -> AdminLiveTournamentSummary:
        self.calls += 1
        return self.summary

    async def list_tournaments(self, **_: object) -> None:  # pragma: no cover
        raise AssertionError("the dashboard must not page tournaments to count them")

    async def find_tournament(self, tournament_id: UUID) -> None:  # pragma: no cover
        raise AssertionError("the dashboard must not read tournaments one at a time")


class _Sanctions:
    def __init__(self, effective: int = 5) -> None:
        self.effective = effective
        self.calls = 0

    async def count_effective(self, *, at: datetime) -> int:
        self.calls += 1
        return self.effective


class _Notifications:
    def __init__(self, exhausted: int = 7) -> None:
        self.exhausted = exhausted
        self.calls = 0

    async def delivery_health(self) -> AdminDeliveryHealth:
        self.calls += 1
        return AdminDeliveryHealth(retry_exhausted=self.exhausted)

    async def list_notifications(self, **_: object) -> None:  # pragma: no cover
        raise AssertionError("the dashboard must not page notifications to count them")


class _Fixture:
    def __init__(self, **overrides: object) -> None:
        self.accounts = _Accounts(summary=overrides.get("accounts"))  # type: ignore[arg-type]
        self.matches = _Matches(overrides.get("matches"))  # type: ignore[arg-type]
        self.tournaments = _Tournaments(overrides.get("tournaments"))  # type: ignore[arg-type]
        self.sanctions = _Sanctions(int(overrides.get("effective", 5)))  # type: ignore[call-overload]
        self.notifications = _Notifications(int(overrides.get("exhausted", 7)))  # type: ignore[call-overload]
        self.entries = InMemoryAuditEntries()
        self.clock = MovableClock(NOW)
        self.service = DashboardService(
            accounts=self.accounts,  # type: ignore[arg-type]
            matches=self.matches,  # type: ignore[arg-type]
            tournaments=self.tournaments,  # type: ignore[arg-type]
            sanctions=self.sanctions,  # type: ignore[arg-type]
            notifications=self.notifications,  # type: ignore[arg-type]
            audit=self.entries,
            clock=self.clock,
        )

    def add_entry(self, *, actor_id: UUID | None, at: datetime = NOW) -> AuditEntry:
        entry = AuditEntry(
            id=generate_uuid7(),
            actor_type=(AuditActorType.ADMINISTRATOR if actor_id else AuditActorType.OPERATOR),
            actor_id=actor_id,
            action=AuditAction.SANCTION_APPLIED,
            subject_type=AuditSubjectType.ACCOUNT,
            subject_ref=str(generate_uuid7()),
            outcome=AuditOutcome.SUCCEEDED,
            created_at=at,
        )
        self.entries.rows.append(entry)
        return entry


class TestTheCostIsFixed:
    @pytest.mark.asyncio
    async def test_the_whole_page_is_seven_reads_and_one_batch(self) -> None:
        """The property a dashboard loses first.

        Six summaries and one bounded audit page, plus one batch resolving
        every administrator the activity list names. Asserted by counting,
        because "it felt fast on my machine" is what a dashboard is like
        right before it is not.
        """
        fixture = _Fixture()
        admin = generate_uuid7()
        fixture.accounts.known[admin] = "chief"
        for _ in range(RECENT_AUDIT_LIMIT):
            fixture.add_entry(actor_id=admin)

        page = await _read(fixture)

        assert fixture.accounts.summary_calls == 1
        assert fixture.matches.calls == 1
        assert fixture.tournaments.calls == 1
        assert fixture.sanctions.calls == 1
        assert fixture.notifications.calls == 1
        # Ten entries by one administrator: **one** batch asking for one
        # account, not ten lookups of the same one.
        assert fixture.accounts.batches == [1]
        assert len(page.recent_activity) == RECENT_AUDIT_LIMIT

    @pytest.mark.asyncio
    async def test_the_read_count_does_not_move_when_the_data_grows(self) -> None:
        """The same page against ten times the activity, by ten times as
        many administrators, costs the same number of reads.

        This is the assertion that would fail the day somebody resolves an
        actor per row, or counts a card by paging its console.
        """
        fixture = _Fixture()
        for _ in range(RECENT_AUDIT_LIMIT * 10):
            actor = generate_uuid7()
            fixture.accounts.known[actor] = "admin"
            fixture.add_entry(actor_id=actor)

        await _read(fixture)

        assert fixture.accounts.summary_calls == 1
        assert fixture.matches.calls == 1
        assert fixture.tournaments.calls == 1
        assert fixture.sanctions.calls == 1
        assert fixture.notifications.calls == 1
        assert len(fixture.accounts.batches) == 1
        # The batch is bounded by the activity limit, not by the trail.
        assert fixture.accounts.batches[0] <= RECENT_AUDIT_LIMIT

    @pytest.mark.asyncio
    async def test_the_activity_list_is_bounded_however_long_the_trail_is(self) -> None:
        """`/audit` is the surface for reading the trail; this is a glance."""
        fixture = _Fixture()
        for _ in range(RECENT_AUDIT_LIMIT * 5):
            fixture.add_entry(actor_id=None)

        page = await _read(fixture)

        assert len(page.recent_activity) == RECENT_AUDIT_LIMIT

    @pytest.mark.asyncio
    async def test_an_operator_entry_costs_no_account_lookup(self) -> None:
        """An operator action names nobody, so there is nobody to resolve —
        and the batch must not ask for `None`."""
        fixture = _Fixture()
        for _ in range(3):
            fixture.add_entry(actor_id=None)

        page = await _read(fixture)

        assert fixture.accounts.batches == [0]
        assert [item.actor_username for item in page.recent_activity] == [None] * 3
        assert {item.actor_type for item in page.recent_activity} == {"operator"}


class TestTheFactsAreTruthful:
    @pytest.mark.asyncio
    async def test_zero_is_rendered_as_zero(self) -> None:
        """`0` means zero. It must never be what a failed query looks like.

        The service is atomic — a failing read raises rather than
        contributing a silent zero — so a page that renders is a page whose
        every number was answered.
        """
        fixture = _Fixture(
            accounts=AdminAccountSummary(registered_last_day=0, registered_last_week=0),
            matches=AdminLiveMatchSummary(active=0, awaiting_acceptance=0),
            tournaments=AdminLiveTournamentSummary(registration_open=0, in_progress=0),
            effective=0,
            exhausted=0,
        )

        page = await _read(fixture)

        assert page.accounts.registered_last_day == 0
        assert page.matches.active == 0
        assert page.tournaments.in_progress == 0
        assert page.attention.restrictions_in_force == 0
        assert page.attention.push_deliveries_retry_exhausted == 0
        assert page.recent_activity == []

    @pytest.mark.asyncio
    async def test_a_failing_card_fails_the_page_rather_than_reading_zero(self) -> None:
        """§18 — the alternative is worse than an error.

        A dashboard that showed `0 restrictions` because the query raised
        would tell an operator that nobody is restricted. Atomicity is what
        makes every rendered number a real one.
        """

        class _Broken:
            async def count_effective(self, *, at: datetime) -> int:
                raise RuntimeError("the database is unreachable")

        fixture = _Fixture()
        fixture.service = DashboardService(
            accounts=fixture.accounts,  # type: ignore[arg-type]
            matches=fixture.matches,  # type: ignore[arg-type]
            tournaments=fixture.tournaments,  # type: ignore[arg-type]
            sanctions=_Broken(),  # type: ignore[arg-type]
            notifications=fixture.notifications,  # type: ignore[arg-type]
            audit=fixture.entries,
            clock=fixture.clock,
        )

        with pytest.raises(RuntimeError, match="unreachable"):
            await _read(fixture)

    @pytest.mark.asyncio
    async def test_the_windows_are_a_day_and_a_week_from_the_services_clock(self) -> None:
        """Not the database's `now()` and not the browser's.

        A window computed anywhere else is one a test cannot reproduce and
        an operator cannot explain — and the two windows must be nested, or
        the day count could exceed the week's.
        """
        fixture = _Fixture()
        await _read(fixture)

        since_day, since_week = fixture.accounts.windows[0]
        assert since_day == NOW - DAY
        assert since_week == NOW - WEEK
        assert since_week < since_day

    @pytest.mark.asyncio
    async def test_the_response_states_when_it_was_composed(self) -> None:
        """Nothing on this page streams. A figure with no age invites an
        operator to trust one from twenty minutes ago."""
        fixture = _Fixture()
        page = await _read(fixture)
        assert page.generated_at == NOW


class TestWhatTheSurfaceCannotDo:
    def test_the_dashboard_is_read_only_and_guarded(self) -> None:
        """§15 — the last admin surface that could have grown a mutation.

        Every card links to the console that owns its action; a retry or a
        restrict button beside a count is one clicked without reading which
        row it was.
        """
        from app.modules.admin.presentation.dependencies import require_admin

        assert admin_dashboard_router.routes
        for route in admin_dashboard_router.routes:
            methods: set[str] = getattr(route, "methods", set())
            assert methods <= {"GET", "HEAD"}, methods

            dependant = getattr(route, "dependant", None)
            assert dependant is not None
            assert require_admin in {sub.call for sub in dependant.dependencies}

    def test_no_card_can_carry_private_or_credential_data(self) -> None:
        """§25 — the dashboard shows **less** than the pages it links to.

        No email, no session, no token, no push endpoint, and no audit
        `before`/`after`: a glance surface has no business carrying the
        metadata a reviewer opens `/audit` for.
        """
        forbidden = {
            "email",
            "password_hash",
            "token",
            "access_token",
            "refresh_token",
            "session",
            "ip",
            "endpoint",
            "p256dh",
            "auth",
            "payload",
            "before",
            "after",
            "reasoning",
        }
        for model in (
            AccountsCard,
            MatchesCard,
            TournamentsCard,
            AttentionCard,
            ActivityEntry,
            DashboardResponse,
        ):
            assert not forbidden & set(model.model_fields), model.__name__

    def test_the_response_carries_no_totals_or_trends(self) -> None:
        """§17 — nothing stores a prior period, so nothing may claim one.

        And no unbounded total: `total_users` would be a `COUNT(*)` that
        answers nothing an operator acts on and costs more every day.
        """
        invented = {"total_users", "total_matches", "trend", "change", "delta", "percent"}
        for model in (AccountsCard, MatchesCard, TournamentsCard, AttentionCard):
            assert not invented & set(model.model_fields), model.__name__


class _Headers:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}


class _Identity:
    def __init__(self) -> None:
        self.id = generate_uuid7()


async def _read(fixture: _Fixture) -> DashboardResponse:
    """The **real** handler."""
    return await read_dashboard(
        _Identity(),  # type: ignore[arg-type]
        fixture.service,
        fixture.accounts,  # type: ignore[arg-type]
        _Headers(),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_the_response_is_never_cached() -> None:
    """A privileged overview sitting in a shared cache is a copy of every
    operational number nobody is accounting for."""
    fixture = _Fixture()
    headers = _Headers()
    await read_dashboard(
        _Identity(),  # type: ignore[arg-type]
        fixture.service,
        fixture.accounts,  # type: ignore[arg-type]
        headers,  # type: ignore[arg-type]
    )
    assert headers.headers["Cache-Control"] == "no-store"


@pytest.mark.asyncio
async def test_an_entry_older_than_the_windows_still_appears_in_activity() -> None:
    """The activity list is the newest ten, not the last day's.

    A quiet week must not empty it — an operator glancing at a dashboard
    wants to know what the last thing that happened *was*, whenever it was.
    """
    fixture = _Fixture()
    fixture.add_entry(actor_id=None, at=NOW - timedelta(days=90))

    page = await _read(fixture)
    assert len(page.recent_activity) == 1
