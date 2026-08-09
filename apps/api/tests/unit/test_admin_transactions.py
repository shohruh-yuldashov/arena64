"""Every admin mutation commits its unit of work — A64-024 regression.

This suite exists because all of them stopped short of it, and nothing
noticed for six phases.

## What went wrong

`repositories.md` §5.1 is explicit: *"Exiting the scope without an explicit
commit rolls back — a forgotten commit loses work loudly instead of
committing partial work quietly."* `SessionUnitOfWork.__aexit__` therefore
rolls back on an exception and does **nothing** on success; the commit is
the service's to call, exactly as `UserService._set_active` calls it.

Every admin service opened the scope and never called it. Each one flushed
its rows, logged success, returned a value describing what it had written,
and then lost the transaction when the session closed. `python -m
app.operator.admin grant` printed *"granted admin to …"* against a database
that ended the command with zero rows.

## Why the existing tests could not see it

The unit tests inject a null unit of work with no commit semantics at all.
The contract tests are worse than silent: `conftest.py` runs each test
inside an outer transaction with `join_transaction_mode="create_savepoint"`,
so a **flush is visible to the same session without any commit**. Asserting
"the four writes are all there" through the session that wrote them proves
they were flushed, and says nothing about whether they would survive.

Even the rollback test passed for the wrong reason: it asserted that a
failure left nothing behind, which is trivially true when success also
leaves nothing behind.

## What this asserts instead

That each service **calls `commit()`**, once, inside the scope. That is not
testing an implementation detail — §5.1 makes the explicit commit the
contract, so calling it is the observable behaviour that separates "wrote"
from "appeared to write". A spy is the only place it is observable without a
second database session, which the contract fixture cannot provide.
"""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.core.identifiers import generate_uuid7
from app.modules.admin.application.services import (
    AdminRoleService,
    AuditRecorder,
    ModerationService,
    NotificationOperationsService,
)
from app.modules.admin.domain.exceptions import (
    LastAdministrator,
    RetryUnavailable,
    SelfSanction,
)
from app.modules.admin.domain.moderation import ModerationCategory
from app.modules.admin.domain.roles import AdminRole
from tests.fakes.admin_audit import InMemoryAuditEntries
from tests.fakes.moderation import (
    InMemoryModerationCases,
    InMemorySanctions,
    RecordingSessionRevoker,
)
from tests.fakes.notification_operations import InMemoryNotificationDirectory
from tests.fakes.presence_redis import MovableClock
from tests.unit.test_admin_authorization import InMemoryRoleAssignments, NullUnitOfWork

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def _recorder(entries: InMemoryAuditEntries) -> AuditRecorder:
    return AuditRecorder(entries=entries, clock=MovableClock(NOW))


class TestTheRoleServiceCommits:
    @pytest.mark.asyncio
    async def test_a_bootstrap_grant_commits(self) -> None:
        """The exact path `python -m app.operator.admin grant` takes, and the
        one that reported success against an unchanged database."""
        unit = NullUnitOfWork()
        service = AdminRoleService(
            assignments=InMemoryRoleAssignments(),
            audit=_recorder(InMemoryAuditEntries()),
            unit_of_work=unit,  # type: ignore[arg-type]
            clock=MovableClock(NOW),
        )

        await service.bootstrap(account_id=generate_uuid7(), role=AdminRole.ADMIN)

        assert unit.commits == 1
        assert unit.rollbacks == 0

    @pytest.mark.asyncio
    async def test_a_grant_and_a_revocation_each_commit_once(self) -> None:
        unit = NullUnitOfWork()
        assignments = InMemoryRoleAssignments()
        service = AdminRoleService(
            assignments=assignments,
            audit=_recorder(InMemoryAuditEntries()),
            unit_of_work=unit,  # type: ignore[arg-type]
            clock=MovableClock(NOW),
        )
        first, second = generate_uuid7(), generate_uuid7()

        await service.bootstrap(account_id=first, role=AdminRole.ADMIN)
        await service.grant(account_id=second, role=AdminRole.ADMIN, granted_by=first)
        await service.revoke(account_id=first, role=AdminRole.ADMIN, revoked_by=second)

        assert unit.commits == 3

    @pytest.mark.asyncio
    async def test_a_refused_grant_commits_nothing(self) -> None:
        """The fail-safe still has to work: a refusal must not commit, and
        must not be mistaken for one that did."""
        unit = NullUnitOfWork()
        assignments = InMemoryRoleAssignments()
        service = AdminRoleService(
            assignments=assignments,
            audit=_recorder(InMemoryAuditEntries()),
            unit_of_work=unit,  # type: ignore[arg-type]
            clock=MovableClock(NOW),
        )
        only = generate_uuid7()
        await service.bootstrap(account_id=only, role=AdminRole.ADMIN)
        commits_after_setup = unit.commits

        with pytest.raises(LastAdministrator):
            await service.revoke(account_id=only, role=AdminRole.ADMIN, revoked_by=None)

        assert unit.commits == commits_after_setup


class TestModerationCommits:
    @pytest.mark.asyncio
    async def test_a_restriction_commits_its_case_sanction_and_audit(self) -> None:
        """The claim `specs/admin.md` §6.12 makes — "all four commit together
        or none does" — had no commit behind it at all."""
        unit = NullUnitOfWork()
        service = _moderation(unit)

        await service.suspend(
            player_id=generate_uuid7(),
            category=ModerationCategory.ABUSE,
            reasoning="Repeated abuse after a warning.",
            expires_at=None,
            actor_id=generate_uuid7(),
            administrators=[generate_uuid7(), generate_uuid7()],
        )

        assert unit.commits == 1

    @pytest.mark.asyncio
    async def test_a_restore_commits(self) -> None:
        unit = NullUnitOfWork()
        service = _moderation(unit)
        player, admin = generate_uuid7(), generate_uuid7()
        await service.suspend(
            player_id=player,
            category=ModerationCategory.ABUSE,
            reasoning="Repeated abuse after a warning.",
            expires_at=None,
            actor_id=admin,
            administrators=[admin, generate_uuid7()],
        )

        await service.restore(player_id=player, actor_id=admin)

        assert unit.commits == 2

    @pytest.mark.asyncio
    async def test_a_refusal_commits_its_failed_audit_entry(self) -> None:
        """The `FAILED` entry is its own transaction, so it needs its own
        commit — and without one the record of a refused attempt was lost
        exactly like the successes."""
        unit = NullUnitOfWork()
        service = _moderation(unit)
        admin = generate_uuid7()

        with pytest.raises(SelfSanction):
            await service.suspend(
                player_id=admin,
                category=ModerationCategory.ABUSE,
                reasoning="Repeated abuse after a warning.",
                expires_at=None,
                actor_id=admin,
                administrators=[admin, generate_uuid7()],
            )

        assert unit.commits == 1


class TestNotificationOperationsCommit:
    @pytest.mark.asyncio
    async def test_a_retry_commits(self) -> None:
        unit = NullUnitOfWork()
        directory = InMemoryNotificationDirectory()
        record, delivery = _exhausted(directory)
        service = NotificationOperationsService(
            deliveries=directory,
            audit=_recorder(InMemoryAuditEntries()),
            unit_of_work=unit,  # type: ignore[arg-type]
            clock=MovableClock(NOW),
        )

        await service.retry_delivery(
            notification_id=record,
            subscription_id=delivery,
            actor_id=generate_uuid7(),
        )

        assert unit.commits == 1

    @pytest.mark.asyncio
    async def test_a_refused_retry_commits_only_its_failed_entry(self) -> None:
        unit = NullUnitOfWork()
        directory = InMemoryNotificationDirectory()
        service = NotificationOperationsService(
            deliveries=directory,
            audit=_recorder(InMemoryAuditEntries()),
            unit_of_work=unit,  # type: ignore[arg-type]
            clock=MovableClock(NOW),
        )
        missing = generate_uuid7()

        with pytest.raises(RetryUnavailable):
            await service.retry_delivery(
                notification_id=missing,
                subscription_id=generate_uuid7(),
                actor_id=generate_uuid7(),
            )
        # The mutation rolled back; the refusal record is a separate scope.
        assert unit.commits == 0
        assert unit.rollbacks == 1

        await service.record_refusal(
            notification_id=missing, actor_id=generate_uuid7(), refusal="delivery_not_retryable"
        )
        assert unit.commits == 1


def _moderation(unit: NullUnitOfWork) -> ModerationService:
    return ModerationService(
        cases=InMemoryModerationCases(),
        sanctions=InMemorySanctions(),
        sessions=RecordingSessionRevoker(),
        audit=_recorder(InMemoryAuditEntries()),
        unit_of_work=unit,  # type: ignore[arg-type]
        clock=MovableClock(NOW),
    )


def _exhausted(directory: InMemoryNotificationDirectory) -> tuple[UUID, UUID]:
    """One notification owed a push that ran out of retries."""
    from app.modules.notifications.domain.push_delivery import (
        PushDeliveryOutcome,
        PushDeliveryStatus,
    )
    from app.modules.notifications.domain.record import (
        NavigationTargetType,
        NotificationCategory,
        NotificationType,
    )
    from app.modules.notifications.public import (
        AdminNotificationRecord,
        AdminPushDelivery,
    )

    record = AdminNotificationRecord(
        id=generate_uuid7(),
        recipient_id=generate_uuid7(),
        type=NotificationType.TOURNAMENT_ROUND_PUBLISHED,
        category=NotificationCategory.TOURNAMENT,
        target_type=NavigationTargetType.TOURNAMENT,
        target_ref=str(generate_uuid7()),
        source_event_id=generate_uuid7(),
        created_at=NOW,
        read_at=None,
        push_capable=True,
    )
    delivery = AdminPushDelivery(
        subscription_id=generate_uuid7(),
        status=PushDeliveryStatus.FAILED,
        outcome=PushDeliveryOutcome.ATTEMPTS_EXHAUSTED,
        attempt_count=5,
        next_attempt_at=None,
        last_attempt_at=NOW,
        delivered_at=None,
        created_at=NOW,
        subscription_created_at=NOW,
        subscription_last_seen_at=NOW,
        subscription_revoked_at=None,
    )
    directory.add(record, [delivery])
    return record.id, delivery.subscription_id
