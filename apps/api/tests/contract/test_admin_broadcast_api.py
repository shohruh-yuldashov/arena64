"""The admin broadcast API and its delivery — A64-027A §38.

The end-to-end path §38 asks for, over real PostgreSQL and the real router:

    admin UI -> admin API -> authorization -> broadcast row
             -> worker batch -> preference gate -> notification rows

Four things this capability must get right, and each fails in a way nobody
notices from the console:

    authorization   a composer hidden in the frontend is not a guard. One
                    form submission here reaches every inbox on the platform
    idempotency     a double-clicked send that produced two broadcasts would
                    tell everybody twice, and there is no undo
    preferences     an administrator must not be able to reach a player who
                    muted announcements by choosing a dropdown value
    safety          no HTML, no control characters, no URL, and no recipient
                    identity in anything the console can read back

Skipped, not failed, when PostgreSQL is unreachable.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import SystemClock
from app.database.unit_of_work import SessionUnitOfWork
from app.modules.admin.domain.audit import AuditAction
from app.modules.admin.domain.roles import AdminRole
from app.modules.admin.infrastructure.models import AuditEntryModel, RoleAssignmentModel
from app.modules.notifications.application.services.broadcast_expander import BroadcastExpander
from app.modules.notifications.application.services.preference_delivery_policy import (
    PreferenceDeliveryPolicy,
)
from app.modules.notifications.domain.preference import IN_APP_ONLY
from app.modules.notifications.domain.record import (
    NotificationCategory,
    NotificationType,
    payload_of,
)
from app.modules.notifications.infrastructure.models import (
    NotificationBroadcastModel,
    NotificationModel,
    NotificationPreferenceModel,
)
from app.modules.notifications.infrastructure.repositories import (
    SqlAlchemyNotificationRepository,
)
from app.modules.notifications.infrastructure.repositories.broadcast_repository import (
    SqlAlchemyBroadcastRepository,
)
from app.modules.notifications.infrastructure.repositories.preference_repository import (
    SqlAlchemyNotificationPreferenceRepository,
)
from app.modules.notifications.infrastructure.sinks import NullNotificationAnnouncer
from app.modules.users.infrastructure.models import UserModel
from app.modules.users.infrastructure.repositories.audience_directory import (
    SqlAlchemyNotificationAudienceDirectory,
)
from tests.contract.contract_app import build_contract_app, contract_client
from tests.contract.test_matchmaking_queue_api import register as register_account

BASE = "/api/v1/admin/broadcasts"

#: Every identifier the broadcast history must never carry. §20, §23: the
#: console reports how many were named, never whom.
FORBIDDEN_KEYS = frozenset(
    {
        "recipients",
        "recipient_ids",
        "player_id",
        "player_ids",
        "user_id",
        "user_ids",
        "email",
        "username",
        "display_name",
    }
)


def _keys(payload: object) -> set[str]:
    """Every key anywhere in the response, however deeply nested."""
    found: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            found.add(key)
            found |= _keys(value)
    elif isinstance(payload, list):
        for item in payload:
            found |= _keys(item)
    return found


@pytest_asyncio.fixture
async def client(contract_session: AsyncSession):  # type: ignore[no-untyped-def]
    async with contract_client(build_contract_app(contract_session)) as http:
        yield http


async def make_admin(session: AsyncSession, client: AsyncClient):  # type: ignore[no-untyped-def]
    account = await register_account(client, session)
    session.add(
        RoleAssignmentModel(
            id=uuid4(),
            account_id=account.id,
            role=AdminRole.ADMIN,
            granted_by=None,
            granted_at=datetime.now(UTC),
        )
    )
    await session.commit()
    return account


async def make_eligible(session: AsyncSession, client: AsyncClient):  # type: ignore[no-untyped-def]
    """A registered account that may receive an announcement.

    `register_account` already verifies; the explicit update states the two
    predicates the audience rule actually reads, so a test using this
    helper does not depend on a detail of the shared fixture.
    """
    account = await register_account(client, session)
    await session.execute(
        update(UserModel).where(UserModel.id == account.id).values(is_verified=True, is_active=True)
    )
    await session.commit()
    return account


def compose(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "title": "Rejalashtirilgan texnik ishlar",
        "body": "Bugun soat 23:00 dan 23:30 gacha platforma vaqtincha ishlamaydi.",
        "locale": "uz",
        "audience": "all_players",
        "idempotency_key": uuid4().hex,
    }
    body.update(overrides)
    return body


def expander(session: AsyncSession) -> BroadcastExpander:
    """The worker, over the test's own session.

    Assembled from the same parts the composition root uses, so a test that
    passes here is a test of the wiring an operator gets — not of a
    convenient double.
    """
    return BroadcastExpander(
        broadcasts=SqlAlchemyBroadcastRepository(session),
        notifications=SqlAlchemyNotificationRepository(session),
        audience=SqlAlchemyNotificationAudienceDirectory(session),
        policy=PreferenceDeliveryPolicy(
            preferences=SqlAlchemyNotificationPreferenceRepository(
                session, availability=IN_APP_ONLY
            )
        ),
        announcer=NullNotificationAnnouncer(),
        clock=SystemClock(),
        unit_of_work=SessionUnitOfWork(session),
    )


class TestAuthorization:
    """§18, §33 — the guard is the server's."""

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("post", BASE),
            ("get", BASE),
            ("get", f"{BASE}/audience/all_players"),
        ],
    )
    async def test_an_anonymous_caller_is_refused(
        self, client: AsyncClient, method: str, path: str
    ) -> None:
        # `GET` takes no body; the composed one is only meaningful for the
        # `POST`. Passing it to both would be a `TypeError`, not a test.
        call = getattr(client, method)
        response = await (call(path, json=compose()) if method == "post" else call(path))
        assert response.status_code == 401

    async def test_a_normal_account_cannot_broadcast(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """The one that matters. A player who found the route must not be
        able to address the platform."""
        account = await register_account(client, contract_session)
        response = await client.post(BASE, json=compose(), headers=account.auth)
        assert response.status_code == 403

        stored = (await contract_session.scalars(select(NotificationBroadcastModel))).all()
        assert stored == []

    async def test_an_administrator_may(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        admin = await make_admin(contract_session, client)
        response = await client.post(BASE, json=compose(), headers=admin.auth)
        # 202, not 200: the platform has taken the instruction and has not
        # yet carried it out.
        assert response.status_code == 202
        assert response.json()["status"] == "queued"


class TestIdempotency:
    """§18 — one form, one broadcast."""

    async def test_a_repeated_key_returns_the_same_broadcast(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        admin = await make_admin(contract_session, client)
        request = compose()

        first = await client.post(BASE, json=request, headers=admin.auth)
        second = await client.post(BASE, json=request, headers=admin.auth)

        assert first.status_code == 202
        assert second.status_code == 202
        assert first.json()["id"] == second.json()["id"]

        stored = (await contract_session.scalars(select(NotificationBroadcastModel))).all()
        assert len(stored) == 1

    async def test_a_second_submission_cannot_rewrite_the_first(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """`ON CONFLICT DO NOTHING`, not `DO UPDATE`. A resend with edited
        text must not change a broadcast that may already be delivering."""
        admin = await make_admin(contract_session, client)
        key = uuid4().hex

        await client.post(BASE, json=compose(idempotency_key=key), headers=admin.auth)
        second = await client.post(
            BASE,
            json=compose(idempotency_key=key, title="Butunlay boshqa matn"),
            headers=admin.auth,
        )

        assert second.json()["title"] == "Rejalashtirilgan texnik ishlar"


class TestTheContentIsSafe:
    """§16, §33 — what an administrator may write into an inbox."""

    @pytest.mark.parametrize(
        "field,value",
        [
            ("title", "   "),
            ("body", "\t\n "),
            ("title", "Salom\x00dunyo"),
            ("body", "Matn\x1b[31m qizil"),
        ],
    )
    async def test_empty_and_control_characters_are_refused(
        self, client: AsyncClient, contract_session: AsyncSession, field: str, value: str
    ) -> None:
        admin = await make_admin(contract_session, client)
        response = await client.post(BASE, json=compose(**{field: value}), headers=admin.auth)
        assert response.status_code == 422

    async def test_a_url_field_does_not_exist(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """The open-redirect protection, asserted rather than assumed.

        `NavigationTargetType` is a closed set precisely so no administrator
        can write a link into a row, and `extra="forbid"` is what keeps that
        true when somebody adds the field to a form first.
        """
        admin = await make_admin(contract_session, client)
        for field in ("url", "action_url", "link", "image_url", "html"):
            response = await client.post(
                BASE,
                json=compose(**{field: "https://evil.example/steal"}),
                headers=admin.auth,
            )
            assert response.status_code == 422, field

    async def test_an_over_long_body_is_refused(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        admin = await make_admin(contract_session, client)
        response = await client.post(BASE, json=compose(body="x" * 601), headers=admin.auth)
        assert response.status_code == 422

    async def test_a_named_audience_without_recipients_is_a_contradiction(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        admin = await make_admin(contract_session, client)
        response = await client.post(
            BASE,
            json=compose(audience="specific_players", recipients=[]),
            headers=admin.auth,
        )
        assert response.status_code == 422

    async def test_a_platform_wide_send_refuses_a_recipient_list(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """Silently ignoring the list is how an administrator who picked
        three people reaches everybody."""
        admin = await make_admin(contract_session, client)
        response = await client.post(
            BASE,
            json=compose(audience="all_players", recipients=[str(uuid4())]),
            headers=admin.auth,
        )
        assert response.status_code == 422


class TestDelivery:
    """§19 — the request queues; the worker delivers."""

    async def test_the_request_writes_no_notification(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """§19's actual requirement: no user loop inside the HTTP request."""
        admin = await make_admin(contract_session, client)
        await make_eligible(contract_session, client)

        await client.post(BASE, json=compose(), headers=admin.auth)

        rows = (await contract_session.scalars(select(NotificationModel))).all()
        assert rows == []

    async def test_the_worker_writes_one_notification_per_eligible_account(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        admin = await make_admin(contract_session, client)
        reader = await make_eligible(contract_session, client)

        await client.post(BASE, json=compose(), headers=admin.auth)
        written = await expander(contract_session).run_once()
        await contract_session.commit()

        assert written >= 1
        rows = (
            await contract_session.scalars(
                select(NotificationModel).where(NotificationModel.recipient_id == reader.id)
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].type == NotificationType.PLATFORM_ANNOUNCEMENT.value
        assert rows[0].category == NotificationCategory.ANNOUNCEMENT.value

        # The text an administrator wrote, decoded through the same seam a
        # player's inbox uses.
        payload = payload_of(NotificationType.PLATFORM_ANNOUNCEMENT, rows[0].payload)
        assert payload.title == "Rejalashtirilgan texnik ishlar"  # type: ignore[union-attr]

        # The destination is the closed enum's, with no identifier.
        assert rows[0].target_type == "home"
        assert rows[0].target_ref is None

    async def test_a_repeated_batch_writes_nothing(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """The crash-safety claim, exercised: a worker that died after
        writing rows and before recording its cursor is replayed."""
        admin = await make_admin(contract_session, client)
        reader = await make_eligible(contract_session, client)

        response = await client.post(BASE, json=compose(), headers=admin.auth)
        broadcast_id = response.json()["id"]

        await expander(contract_session).run_once()
        await contract_session.commit()

        # Rewind the cursor by hand — exactly the state a crash leaves.
        await contract_session.execute(
            update(NotificationBroadcastModel)
            .where(NotificationBroadcastModel.id == broadcast_id)
            .values(cursor=None, status="sending")
        )
        await contract_session.commit()

        await expander(contract_session).run_once()
        await contract_session.commit()

        rows = (
            await contract_session.scalars(
                select(NotificationModel).where(NotificationModel.recipient_id == reader.id)
            )
        ).all()
        assert len(rows) == 1

    async def test_a_muted_player_receives_nothing(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """§15 — the preference bypass this design exists to prevent.

        `ANNOUNCEMENT` is deliberately absent from `preference.LOCKED`, so a
        player who turned the category off is not reachable by an
        administrator choosing a dropdown value.
        """
        admin = await make_admin(contract_session, client)
        muted = await make_eligible(contract_session, client)

        contract_session.add(
            NotificationPreferenceModel(
                user_id=muted.id,
                category=NotificationCategory.ANNOUNCEMENT.value,
                channel="in_app",
                enabled=False,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        await contract_session.commit()

        await client.post(BASE, json=compose(), headers=admin.auth)
        await expander(contract_session).run_once()
        await contract_session.commit()

        rows = (
            await contract_session.scalars(
                select(NotificationModel).where(NotificationModel.recipient_id == muted.id)
            )
        ).all()
        assert rows == []

    async def test_an_unverified_account_is_not_in_the_audience(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        admin = await make_admin(contract_session, client)
        unverified = await register_account(client, contract_session)
        # `register_account` verifies — A64-021.5H put a later write behind
        # a verified address. Un-verifying here is what makes this test
        # about the audience rule rather than about the helper.
        await contract_session.execute(
            update(UserModel).where(UserModel.id == unverified.id).values(is_verified=False)
        )
        await contract_session.commit()

        await client.post(BASE, json=compose(), headers=admin.auth)
        await expander(contract_session).run_once()
        await contract_session.commit()

        rows = (
            await contract_session.scalars(
                select(NotificationModel).where(NotificationModel.recipient_id == unverified.id)
            )
        ).all()
        assert rows == []

    async def test_a_named_audience_reaches_only_the_named(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        admin = await make_admin(contract_session, client)
        chosen = await make_eligible(contract_session, client)
        bystander = await make_eligible(contract_session, client)

        await client.post(
            BASE,
            json=compose(audience="specific_players", recipients=[str(chosen.id)]),
            headers=admin.auth,
        )
        await expander(contract_session).run_once()
        await contract_session.commit()

        assert await _count_for(contract_session, chosen.id) == 1
        assert await _count_for(contract_session, bystander.id) == 0

    async def test_the_broadcast_completes_and_reports_its_totals(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        admin = await make_admin(contract_session, client)
        await make_eligible(contract_session, client)

        response = await client.post(BASE, json=compose(), headers=admin.auth)
        broadcast_id = response.json()["id"]

        # One pass delivers the only page; the next finds none and finishes.
        await expander(contract_session).run_once()
        await contract_session.commit()
        await expander(contract_session).run_once()
        await contract_session.commit()

        detail = await client.get(f"{BASE}/{broadcast_id}", headers=admin.auth)
        body = detail.json()
        assert body["status"] == "completed"
        assert body["audience_size"] is not None
        assert body["delivered"] >= 1


class TestTheHistoryNamesNobody:
    """§20, §23 — what an operator may read back."""

    async def test_the_history_carries_no_recipient(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        admin = await make_admin(contract_session, client)
        chosen = await make_eligible(contract_session, client)

        await client.post(
            BASE,
            json=compose(audience="specific_players", recipients=[str(chosen.id)]),
            headers=admin.auth,
        )

        response = await client.get(BASE, headers=admin.auth)
        assert response.status_code == 200, response.text
        # The count travels; the identity does not.
        assert response.json()["items"][0]["named_recipients"] == 1
        assert str(chosen.id) not in response.text
        assert "@example.com" not in response.text
        # And no field that could carry one later. A leak arrives as a key
        # somebody added in good faith — "just the recipient ids, for
        # support" — so the shape is asserted, not only this payload.
        assert _keys(response.json()) & FORBIDDEN_KEYS == set()

    async def test_the_broadcast_is_audited_without_naming_recipients(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """§23 — the highest-reach action the console offers is on the
        record, and the record is not a recipient list."""
        admin = await make_admin(contract_session, client)
        chosen = await make_eligible(contract_session, client)

        response = await client.post(
            BASE,
            json=compose(audience="specific_players", recipients=[str(chosen.id)]),
            headers=admin.auth,
        )
        broadcast_id = response.json()["id"]

        entry = await contract_session.scalar(
            select(AuditEntryModel).where(
                AuditEntryModel.action == AuditAction.NOTIFICATION_BROADCAST_SENT.value
            )
        )
        assert entry is not None
        assert entry.actor_id == admin.id
        assert entry.subject_ref == broadcast_id
        assert entry.after["audience"] == "specific_players"
        assert entry.after["named_recipients"] == 1
        assert str(chosen.id) not in str(entry.after)
        # The body is on the broadcast row, which is the system of record.
        assert "body" not in entry.after


async def _count_for(session: AsyncSession, player_id: object) -> int:
    rows = (
        await session.scalars(
            select(NotificationModel).where(NotificationModel.recipient_id == player_id)
        )
    ).all()
    return len(rows)
