"""The dashboard's reads against real PostgreSQL — A64-024.9.

`tests/unit/test_admin_dashboard.py` counts the reads. What it cannot check
is whether each one is *cheap*, and that is the claim this task actually
makes: six numbers on a page that stays fast as the platform grows.

So this asserts the plans. Every count below must be answered from an index
rather than by scanning a table whose size tracks traffic — and the one read
that does scan is asserted to be the one the spec says scans, so a future
change that quietly adds a second is visible here.

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

from datetime import UTC, datetime, timedelta

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.identifiers import generate_uuid7
from app.modules.admin.infrastructure.repositories import SqlAlchemySanctionRepository
from app.modules.game.infrastructure.repositories.match_record_repository import (
    SqlAlchemyAdministrativeMatchDirectory,
)
from app.modules.notifications.infrastructure.repositories import (
    SqlAlchemyAdministrativeNotificationDirectory,
)
from app.modules.tournament.infrastructure.repositories.admin_directory import (
    SqlAlchemyAdministrativeTournamentDirectory,
)
from app.modules.users.infrastructure.repositories.user_repository import (
    SqlAlchemyAdministrativeUserDirectory,
)

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def accounts(contract_session: AsyncSession) -> SqlAlchemyAdministrativeUserDirectory:
    return SqlAlchemyAdministrativeUserDirectory(contract_session)


async def _plan(session: AsyncSession, statement: str) -> str:
    """The planner's chosen path, with sequential scans discouraged.

    `enable_seqscan = off` is a *penalty*, not a prohibition: PostgreSQL
    still picks a scan when no index can answer the query at all. That is
    precisely what makes this a useful assertion on a small test database —
    a plan that falls back to a scan here is one with no index behind it,
    whatever the row count.
    """
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    rows = await session.execute(text(f"EXPLAIN {statement}"))
    return "\n".join(row[0] for row in rows)


class TestEveryCountedFactHasAnIndex:
    async def test_live_matches_are_counted_from_the_partial_index(
        self, contract_session: AsyncSession
    ) -> None:
        """The claim that lets this card exist at all.

        `ix_match__current_light`/`__current_dark` are partial on exactly
        the two live statuses, so the count reads an index holding only the
        games in flight. Without this the card would scan `game.match` —
        the platform's largest table and a partition candidate — on every
        dashboard load.
        """
        plan = await _plan(
            contract_session,
            "SELECT status, count(*) FROM game.match "
            "WHERE status IN ('pending_acceptance','active') GROUP BY status",
        )
        assert "ix_match__current" in plan, plan
        assert "Seq Scan on match" not in plan, plan

    async def test_effective_restrictions_are_counted_from_the_partial_index(
        self, contract_session: AsyncSession
    ) -> None:
        """`ix_sanction__player_expiry` is partial on `lifted_at IS NULL` —
        `database.md` §12.6's design, because a partial predicate cannot
        contain `now()`. The instant comparison is a filter over the few
        rows it returns."""
        plan = await _plan(
            contract_session,
            "SELECT count(*) FROM admin.sanction WHERE lifted_at IS NULL "
            "AND starts_at <= now() AND (expires_at IS NULL OR expires_at > now())",
        )
        assert "ix_sanction__player_expiry" in plan, plan

    async def test_exhausted_push_deliveries_are_counted_from_the_partial_index(
        self, contract_session: AsyncSession
    ) -> None:
        """`ix_notification_push_delivery__failed` — added by A64-024.7 for
        this exact question, partial on `failed` so it holds only failures
        and not the delivery history."""
        plan = await _plan(
            contract_session,
            "SELECT count(*) FROM notifications.notification_push_delivery "
            "WHERE status = 'failed' AND outcome = 'attempts_exhausted'",
        )
        assert "ix_notification_push_delivery__failed" in plan, plan

    async def test_recent_registrations_are_a_bounded_range_scan(
        self, contract_session: AsyncSession
    ) -> None:
        """One index range from the older window forward, with the shorter
        window as a `FILTER` over rows already in hand.

        Bounded by how many people registered in a week — not by how many
        have ever registered, which is what a `COUNT(*)` would have been.
        """
        plan = await _plan(
            contract_session,
            "SELECT count(*) FILTER (WHERE created_at >= now() - interval '1 day'), "
            "count(*) FROM users.\"user\" WHERE created_at >= now() - interval '7 days'",
        )
        assert "ix_user__created_at_id" in plan, plan
        assert 'Seq Scan on "user"' not in plan, plan

    async def test_the_activity_list_reads_ten_rows_from_the_audit_keyset(
        self, contract_session: AsyncSession
    ) -> None:
        """A `LIMIT` walk down `ix_audit_entry__created_at_id` — the trail
        can grow forever and this stays ten rows."""
        plan = await _plan(
            contract_session,
            "SELECT * FROM admin.audit_entry ORDER BY created_at DESC, id DESC LIMIT 10",
        )
        assert "ix_audit_entry__created_at_id" in plan, plan
        assert "Sort" not in plan, plan

    async def test_the_tournament_count_is_the_one_scan_and_stays_the_only_one(
        self, contract_session: AsyncSession
    ) -> None:
        """The accepted exception, asserted so it cannot quietly acquire a
        sibling.

        `tournaments.tournament` holds one row per tournament ever created
        and grows by operator action rather than by traffic, so the scan is
        measured in microseconds. `specs/admin.md` §6.14 records the
        threshold at which that stops being true and what the fix is —
        this test is what makes the exception visible rather than assumed.
        """
        plan = await _plan(
            contract_session,
            "SELECT status, count(*) FROM tournaments.tournament "
            "WHERE status IN ('registration_open','in_progress') GROUP BY status",
        )
        assert "Seq Scan on tournament" in plan, plan


class TestTheAdaptersAnswerTruthfully:
    async def test_the_account_summary_nests_its_two_windows(
        self, accounts: SqlAlchemyAdministrativeUserDirectory, contract_session: AsyncSession
    ) -> None:
        """The day count is a subset of the week's, and neither counts what
        is outside both.

        Asserted against real rows because the `FILTER` aggregate is the
        one piece of this card that a fake would model as two list
        comprehensions and always get right.
        """
        from app.modules.users.infrastructure.models import UserModel

        for age_days, suffix in ((0, "today"), (3, "midweek"), (30, "old")):
            contract_session.add(
                UserModel(
                    id=generate_uuid7(),
                    username=f"dash_{suffix}",
                    # `username_folded` is a generated column — the database
                    # derives it, and supplying one is refused outright.
                    email=f"dash_{suffix}@example.com",
                    password_hash="x",
                    is_active=True,
                    is_verified=True,
                    created_at=NOW - timedelta(days=age_days),
                )
            )
        await contract_session.flush()

        summary = await accounts.account_summary(
            since_day=NOW - timedelta(days=1), since_week=NOW - timedelta(days=7)
        )

        assert summary.registered_last_day == 1
        assert summary.registered_last_week == 2

    async def test_the_live_summaries_report_zero_rather_than_omitting_a_state(
        self, contract_session: AsyncSession
    ) -> None:
        """A grouped query returns **no row** for a state nothing is in.

        Reading the counts positionally would make an empty platform raise
        or mislabel — this is why both adapters read out of a mapping.
        """
        matches = await SqlAlchemyAdministrativeMatchDirectory(
            contract_session
        ).live_match_summary()
        tournaments = await SqlAlchemyAdministrativeTournamentDirectory(
            contract_session
        ).live_tournament_summary()
        restrictions = await SqlAlchemySanctionRepository(contract_session).count_effective(at=NOW)
        deliveries = await SqlAlchemyAdministrativeNotificationDirectory(
            contract_session
        ).delivery_health()

        assert matches.active == 0
        assert matches.awaiting_acceptance == 0
        assert tournaments.registration_open == 0
        assert tournaments.in_progress == 0
        assert restrictions == 0
        assert deliveries.retry_exhausted == 0
