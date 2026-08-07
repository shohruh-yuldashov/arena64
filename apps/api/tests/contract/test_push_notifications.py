"""Web Push, end to end — A64-021.6 §30.

Against real PostgreSQL and through the real application, with a **stub push
service** standing in for Mozilla's or Google's. §28 forbids an automated
test contacting a real one, and the substitution is the narrowest thing that
makes that true: one `httpx.AsyncClient` whose transport answers from a
script, injected into the real `WebPushProvider`. Everything above it — the
router, the ownership rules, the fan-out, the claim, the retry schedule and
the revocation — is what ships.

What this leaves unproven is stated plainly, because §28 requires it: it
does **not** show that a real push service accepts these bytes, and it
cannot. What it does show is that the bytes are the ones RFC 8291 specifies
— `tests/unit/test_web_push_protocol.py` decrypts them as a browser would —
and that every status code a real service can answer with leads to the right
state here.

## The VAPID pair is generated per run, never committed

A key pair in a repository is a key pair somebody eventually deploys. This
suite needs only that *a* valid pair exists.

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.app_factory import create_app
from app.config.settings import get_settings
from app.core.clock import SystemClock
from app.core.identifiers import generate_uuid7
from app.database.unit_of_work import SessionUnitOfWork
from app.modules.notifications.application.ports import DuePushDelivery
from app.modules.notifications.application.services.preference_delivery_policy import (
    PreferenceDeliveryPolicy,
)
from app.modules.notifications.application.services.push_delivery_service import (
    PushDeliveryService,
)
from app.modules.notifications.domain.preference import ChannelAvailability, DeliveryChannel
from app.modules.notifications.domain.push_delivery import (
    PushDeliveryOutcome,
    PushDeliveryStatus,
)
from app.modules.notifications.domain.record import NotificationType
from app.modules.notifications.infrastructure.models import (
    NotificationPushDeliveryModel,
    PushSubscriptionModel,
)
from app.modules.notifications.infrastructure.repositories import (
    SqlAlchemyNotificationPreferenceRepository,
    SqlAlchemyPushDeliveryRepository,
    SqlAlchemyPushSubscriptionRepository,
)
from app.platform.metrics import NullMetrics
from app.platform.push import VapidKeyPair, VapidSigner, WebPushProvider, generate_key_pair
from tests.contract.contract_app import build_contract_app, contract_client

REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
SUBSCRIBE_URL = "/api/v1/notifications/push/subscriptions"
REMOVE_URL = "/api/v1/notifications/push/subscriptions/remove"
STATUS_URL = "/api/v1/notifications/push/status"
PREFERENCES_URL = "/api/v1/notifications/preferences"
PASSWORD = "CorrectHorse1!"

#: A well-formed browser key, as `pushManager.subscribe()` produces one.
#:
#: A **real** P-256 point rather than sixty-five arbitrary bytes: the
#: encryption verifies curve membership, so a made-up key is refused and
#: every delivery test would assert the wrong thing.
_BROWSER_PUBLIC = generate_key_pair()[1]

#: 16 bytes, base64url — the length RFC 8291 fixes for the auth secret.
_BROWSER_AUTH = "MDEyMzQ1Njc4OWFiY2RlZg"

PUSHABLE = NotificationType.TOURNAMENT_ROUND_PUBLISHED


@pytest.fixture(scope="session")
def vapid() -> tuple[str, str]:
    """One pair for the session. Generated, never committed."""
    return generate_key_pair()


@pytest.fixture
def app(vapid: tuple[str, str], monkeypatch: pytest.MonkeyPatch) -> Any:
    """The production app, with push configured.

    Through the **environment** rather than by patching afterwards, for the
    reason `test_avatar_api.py` gives: availability is read at construction
    and threaded into the preference repository, so an app built without the
    keys reports push unavailable however the settings are patched later.
    """
    private, public = vapid
    monkeypatch.setenv("VAPID_PRIVATE_KEY", private)
    monkeypatch.setenv("VAPID_PUBLIC_KEY", public)
    get_settings.cache_clear()
    yield create_app()
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def client(app: FastAPI, contract_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async with contract_client(build_contract_app(contract_session, app=app)) as http:
        yield http


@pytest_asyncio.fixture
async def unconfigured_client(contract_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """The same application with **no** VAPID pair configured."""
    get_settings.cache_clear()
    async with contract_client(build_contract_app(contract_session)) as http:
        yield http
    get_settings.cache_clear()


class StubPushService:
    """A push service that answers from a script — §28's substitution.

    Records the requests so a test can assert on what was *sent* as well as
    on the outcome, and answers a queued status per call so one test can make
    the first attempt fail and the second succeed.
    """

    def __init__(self, *statuses: int) -> None:
        self._statuses = list(statuses)
        self.requests: list[httpx.Request] = []

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self._statuses.pop(0) if self._statuses else 201)

    def provider(self, vapid: tuple[str, str]) -> WebPushProvider:
        """The **real** provider, over a stubbed transport."""
        private, public = vapid
        return WebPushProvider(
            signer=VapidSigner(
                VapidKeyPair.from_base64(
                    private_key=private,
                    public_key=public,
                    subject="mailto:no-reply@arena64.gg",
                )
            ),
            client=httpx.AsyncClient(transport=httpx.MockTransport(self._handle)),
        )


async def register(client: AsyncClient, session: AsyncSession) -> dict[str, Any]:
    """A verified account with a session — push registration requires one."""
    suffix = uuid4().hex[:10]
    account = {
        "username": f"push{suffix}",
        "email": f"{suffix}@example.com",
        "password": PASSWORD,
    }
    created = await client.post(REGISTER_URL, json=account)
    assert created.status_code == 201, created.text
    user_id = UUID(created.json()["data"]["id"])

    # Verified out of band, exactly as `app.operator.accounts verify` does.
    # Every outward-facing write requires it since A64-021.5H, and a push
    # subscription is one (§24).
    await session.execute(
        text("UPDATE users.user SET is_verified = true WHERE id = :id"), {"id": user_id}
    )

    signed_in = await client.post(LOGIN_URL, json={"email": account["email"], "password": PASSWORD})
    assert signed_in.status_code == 200, signed_in.text
    return {"token": signed_in.json()["data"]["access_token"], "id": user_id, **account}


def auth(account: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {account['token']}"}


def subscription(endpoint: str | None = None) -> dict[str, str]:
    return {
        "endpoint": endpoint or f"https://push.example.com/wpush/{uuid4().hex}",
        "p256dh": _BROWSER_PUBLIC,
        "auth": _BROWSER_AUTH,
    }


async def enable_push(client: AsyncClient, account: dict[str, Any]) -> None:
    """Turns tournament push on for this account.

    Every delivery test needs it, because push defaults to **off** — §
    `domain.preference._default_for`: in-app on, everything else off. A
    channel that interrupts somebody has to be asked for, and a suite that
    delivered without this would be testing a default the platform does not
    have.
    """
    enabled = await client.patch(
        PREFERENCES_URL,
        json={"changes": [{"category": "tournament", "channel": "push", "enabled": True}]},
        headers=auth(account),
    )
    assert enabled.status_code == 200, enabled.text


def worker(
    session: AsyncSession,
    provider: WebPushProvider | None,
    *,
    max_attempts: int = 5,
) -> PushDeliveryService:
    """The real delivery service over the test's session.

    Assembled here rather than through `build_push_delivery_service` for one
    reason: that factory takes a `PushSettings`, and every figure this suite
    needs to vary is on it. Every collaborator is the production one.
    """
    return PushDeliveryService(
        deliveries=SqlAlchemyPushDeliveryRepository(session),
        subscriptions=SqlAlchemyPushSubscriptionRepository(session),
        policy=PreferenceDeliveryPolicy(
            preferences=SqlAlchemyNotificationPreferenceRepository(
                session, availability=ChannelAvailability.of(DeliveryChannel.PUSH)
            )
        ),
        provider=provider,
        metrics=NullMetrics(),
        unit_of_work=SessionUnitOfWork(session),
        clock=SystemClock(),
        availability=ChannelAvailability.of(DeliveryChannel.PUSH),
        batch_size=20,
        max_attempts=max_attempts,
        retry_base_seconds=60,
        retry_max_seconds=3600,
        ttl_seconds=3600,
    )


async def owe_push(
    session: AsyncSession, *, recipient_id: UUID, subscription_ids: list[UUID]
) -> UUID:
    """One notification owed a push on each of these devices.

    Enqueued through the **repository**, which is the same statement the
    durable writer runs — including its `ON CONFLICT DO NOTHING`, which is
    the idempotency one test below exercises directly.
    """
    notification_id = generate_uuid7()
    await SqlAlchemyPushDeliveryRepository(session).enqueue(
        [
            DuePushDelivery(
                notification_id=notification_id,
                subscription_id=subscription_id,
                recipient_id=recipient_id,
                notification_type=PUSHABLE,
                attempt_count=0,
            )
            for subscription_id in subscription_ids
        ],
        at=SystemClock().now(),
    )
    return notification_id


async def live_subscription_ids(session: AsyncSession, user_id: UUID) -> list[UUID]:
    rows = await session.scalars(
        select(PushSubscriptionModel.id).where(
            PushSubscriptionModel.user_id == user_id,
            PushSubscriptionModel.revoked_at.is_(None),
        )
    )
    return list(rows)


async def delivery_row(
    session: AsyncSession, notification_id: UUID, subscription_id: UUID
) -> NotificationPushDeliveryModel:
    row = await session.scalar(
        select(NotificationPushDeliveryModel).where(
            NotificationPushDeliveryModel.notification_id == notification_id,
            NotificationPushDeliveryModel.subscription_id == subscription_id,
        )
    )
    assert row is not None
    return row


class TestRegistration:
    async def test_a_verified_account_registers_its_browser(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """The whole registration path, and the assertion that matters most
        is about the **response**: it carries an id and nothing else.

        An endpoint or a key echoed back would put a bearer capability in a
        response body, a browser cache and any proxy that logs one — §25.
        """
        account = await register(client, contract_session)

        created = await client.post(SUBSCRIBE_URL, json=subscription(), headers=auth(account))

        assert created.status_code == 201, created.text
        body = created.json()["data"]
        assert set(body) == {"id"}
        assert "push.example.com" not in created.text

    async def test_a_supplied_user_id_is_refused(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """§3, §4: the account comes from the session and there is no field
        to override it with.

        `extra="forbid"` makes the attempt a `422` rather than a silently
        ignored field — a client that thought it was subscribing somebody
        else should be told it was not, and a test asserting only that the
        *effect* was absent would pass against a model that quietly dropped
        it.
        """
        account = await register(client, contract_session)
        victim = await register(client, contract_session)

        refused = await client.post(
            SUBSCRIBE_URL,
            json={**subscription(), "user_id": str(victim["id"])},
            headers=auth(account),
        )

        assert refused.status_code == 422, refused.text

    async def test_several_browsers_are_several_devices_and_one_browser_is_one(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """§9, and both halves of what the endpoint upsert means.

        Three browsers are three devices — a laptop, a phone and a second
        browser each owe their own push. The **fourth** registration repeats
        the third's endpoint, which is what a client does on every app
        start, and must not become a fourth device: two rows for one browser
        would push to it twice.
        """
        account = await register(client, contract_session)
        browsers = [subscription() for _ in range(3)]

        for browser in [*browsers, browsers[-1]]:
            created = await client.post(SUBSCRIBE_URL, json=browser, headers=auth(account))
            assert created.status_code == 201, created.text

        status = await client.get(STATUS_URL, headers=auth(account))
        assert status.json()["data"]["device_count"] == 3


class TestAccountSwitch:
    async def test_a_shared_browser_stops_reaching_the_previous_account(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """**§23, and the most consequential test in this suite.**

        Two people, one laptop. The first signs out and the second signs in,
        and the browser re-registers the endpoint it still holds. From that
        moment the first account must have nothing that can reach it.

        Asserted from *both* sides, because either alone would pass against a
        broken implementation: the endpoint must belong to the second account
        (or the first would keep being pushed), and the first must have no
        live device at all (or a duplicate row would push to the same
        browser twice, once per owner).
        """
        first = await register(client, contract_session)
        second = await register(client, contract_session)
        shared = subscription()

        await client.post(SUBSCRIBE_URL, json=shared, headers=auth(first))
        await client.post(SUBSCRIBE_URL, json=shared, headers=auth(second))

        assert await live_subscription_ids(contract_session, first["id"]) == []
        assert len(await live_subscription_ids(contract_session, second["id"])) == 1

    async def test_signing_out_removes_this_browser_and_no_other(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """§22, §23. The sign-out path removes the browser that is signing
        out — never the account's other devices, which belong to a phone
        somebody is not holding."""
        account = await register(client, contract_session)
        here = subscription()
        elsewhere = subscription()
        await client.post(SUBSCRIBE_URL, json=here, headers=auth(account))
        await client.post(SUBSCRIBE_URL, json=elsewhere, headers=auth(account))

        removed = await client.post(
            REMOVE_URL, json={"endpoint": here["endpoint"]}, headers=auth(account)
        )

        assert removed.status_code == 204
        assert len(await live_subscription_ids(contract_session, account["id"])) == 1

    async def test_removing_somebody_elses_endpoint_answers_the_same_and_does_nothing(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """An endpoint is a bearer capability, so "is this one registered to
        another account" must not be answerable. A `404` here would be an
        enumeration oracle for exactly that."""
        owner = await register(client, contract_session)
        stranger = await register(client, contract_session)
        theirs = subscription()
        await client.post(SUBSCRIBE_URL, json=theirs, headers=auth(owner))

        answered = await client.post(
            REMOVE_URL, json={"endpoint": theirs["endpoint"]}, headers=auth(stranger)
        )

        assert answered.status_code == 204
        assert len(await live_subscription_ids(contract_session, owner["id"])) == 1


class TestAvailability:
    async def test_push_is_unavailable_without_a_key_pair(
        self, unconfigured_client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """§6. The settings screen must not show a working switch behind a
        server that cannot deliver, and the API refuses a subscription
        either way — a stored capability nothing will use is a row that
        looks like a working device and is not."""
        account = await register(unconfigured_client, contract_session)

        status = await unconfigured_client.get(STATUS_URL, headers=auth(account))
        refused = await unconfigured_client.post(
            SUBSCRIBE_URL, json=subscription(), headers=auth(account)
        )

        body = status.json()["data"]
        assert body["available"] is False
        assert body["vapid_public_key"] is None
        assert refused.status_code == 422

    async def test_the_public_key_is_served_and_the_private_one_never_is(
        self, client: AsyncClient, contract_session: AsyncSession, vapid: tuple[str, str]
    ) -> None:
        """A browser cannot subscribe without the public key, so it has to be
        served. The private half must appear in no response at all — it is
        the value that lets anybody push to every subscription this platform
        holds."""
        private, public = vapid
        account = await register(client, contract_session)

        status = await client.get(STATUS_URL, headers=auth(account))

        assert status.json()["data"]["vapid_public_key"] == public
        assert private not in status.text


class TestDelivery:
    async def test_every_device_is_pushed_and_recorded(
        self, client: AsyncClient, contract_session: AsyncSession, vapid: tuple[str, str]
    ) -> None:
        """The delivery path, over two devices.

        The assertion on the **request** is the one that would catch a
        transport regression no status code reports: the body must be
        `aes128gcm`, and the `Authorization` header must be a VAPID
        assertion. A push service that received neither would refuse, and a
        test asserting only `status == sent` would not know.
        """
        account = await register(client, contract_session)
        await enable_push(client, account)
        await client.post(SUBSCRIBE_URL, json=subscription(), headers=auth(account))
        await client.post(SUBSCRIBE_URL, json=subscription(), headers=auth(account))
        devices = await live_subscription_ids(contract_session, account["id"])
        notification_id = await owe_push(
            contract_session, recipient_id=account["id"], subscription_ids=devices
        )

        service = StubPushService(201, 201)
        result = await worker(contract_session, service.provider(vapid)).deliver_once()

        assert result.outcomes == {PushDeliveryOutcome.DELIVERED: 2}
        assert len(service.requests) == 2
        assert {request.headers["content-encoding"] for request in service.requests} == {
            "aes128gcm"
        }
        assert all(
            request.headers["authorization"].startswith("vapid t=") for request in service.requests
        )
        for device in devices:
            row = await delivery_row(contract_session, notification_id, device)
            assert row.status == PushDeliveryStatus.SENT.value

    async def test_a_gone_subscription_is_revoked_and_not_retried(
        self, client: AsyncClient, contract_session: AsyncSession, vapid: tuple[str, str]
    ) -> None:
        """§17. `410` is the ordinary end of a browser's life — cleared site
        data, an uninstalled PWA, a revoked permission — and one dead device
        must be cleaned up automatically rather than retried forever.

        Both halves are asserted because either alone is a bug: the row must
        stop being due (or the worker retries a device that is gone) and the
        subscription must stop being live (or every future notification
        enqueues a delivery to it).
        """
        account = await register(client, contract_session)
        await enable_push(client, account)
        await client.post(SUBSCRIBE_URL, json=subscription(), headers=auth(account))
        (device,) = await live_subscription_ids(contract_session, account["id"])
        notification_id = await owe_push(
            contract_session, recipient_id=account["id"], subscription_ids=[device]
        )

        await worker(contract_session, StubPushService(410).provider(vapid)).deliver_once()

        row = await delivery_row(contract_session, notification_id, device)
        assert row.outcome == PushDeliveryOutcome.SUBSCRIPTION_GONE.value
        assert row.next_attempt_at is None
        assert await live_subscription_ids(contract_session, account["id"]) == []

    async def test_a_transient_failure_is_retried_later(
        self, client: AsyncClient, contract_session: AsyncSession, vapid: tuple[str, str]
    ) -> None:
        """§18. A `503` is the push service's fault, not the device's: the
        row stays owed with a later `next_attempt_at`, and the subscription
        is untouched — revoking a live browser because a service had a bad
        minute would lose it permanently."""
        account = await register(client, contract_session)
        await enable_push(client, account)
        await client.post(SUBSCRIBE_URL, json=subscription(), headers=auth(account))
        (device,) = await live_subscription_ids(contract_session, account["id"])
        notification_id = await owe_push(
            contract_session, recipient_id=account["id"], subscription_ids=[device]
        )

        await worker(contract_session, StubPushService(503).provider(vapid)).deliver_once()

        row = await delivery_row(contract_session, notification_id, device)
        assert row.outcome == PushDeliveryOutcome.RETRYABLE_FAILURE.value
        assert row.status == PushDeliveryStatus.PENDING.value
        assert row.next_attempt_at is not None
        assert len(await live_subscription_ids(contract_session, account["id"])) == 1

    async def test_a_muted_preference_sends_nothing(
        self, client: AsyncClient, contract_session: AsyncSession, vapid: tuple[str, str]
    ) -> None:
        """§14. The preference is read at **delivery** time, so somebody who
        mutes push after a round is published is not pushed — which only
        holds because the row exists and the send-time check refuses it.

        Asserted on the stub rather than only on the outcome: `skipped` with
        a request having been made would mean the platform contacted a push
        service for somebody who asked it not to.
        """
        account = await register(client, contract_session)
        await enable_push(client, account)
        await client.post(SUBSCRIBE_URL, json=subscription(), headers=auth(account))
        (device,) = await live_subscription_ids(contract_session, account["id"])
        # Turned back off, which is the case §14 is about: the round was
        # already published when they changed their mind.
        muted = await client.patch(
            PREFERENCES_URL,
            json={"changes": [{"category": "tournament", "channel": "push", "enabled": False}]},
            headers=auth(account),
        )
        assert muted.status_code == 200, muted.text
        notification_id = await owe_push(
            contract_session, recipient_id=account["id"], subscription_ids=[device]
        )

        service = StubPushService(201)
        await worker(contract_session, service.provider(vapid)).deliver_once()

        assert service.requests == []
        row = await delivery_row(contract_session, notification_id, device)
        assert row.outcome == PushDeliveryOutcome.SKIPPED_PREFERENCE.value

    async def test_a_redelivered_event_does_not_push_twice(
        self, client: AsyncClient, contract_session: AsyncSession, vapid: tuple[str, str]
    ) -> None:
        """§19. The relay redelivers on retry, so the fan-out runs again for
        a notification already stored. The key `(notification, subscription)`
        makes that a no-op at the database rather than a check somebody
        remembered to write — and the proof is that a second worker pass
        sends nothing, because there is nothing new to claim."""
        account = await register(client, contract_session)
        await enable_push(client, account)
        await client.post(SUBSCRIBE_URL, json=subscription(), headers=auth(account))
        (device,) = await live_subscription_ids(contract_session, account["id"])
        notification_id = await owe_push(
            contract_session, recipient_id=account["id"], subscription_ids=[device]
        )

        # The relay, retrying: the same notification, the same device.
        again = await SqlAlchemyPushDeliveryRepository(contract_session).enqueue(
            [
                DuePushDelivery(
                    notification_id=notification_id,
                    subscription_id=device,
                    recipient_id=account["id"],
                    notification_type=PUSHABLE,
                    attempt_count=0,
                )
            ],
            at=SystemClock().now(),
        )

        service = StubPushService(201, 201)
        first = await worker(contract_session, service.provider(vapid)).deliver_once()
        second = await worker(contract_session, service.provider(vapid)).deliver_once()

        assert again == 0
        assert first.claimed == 1
        assert second.claimed == 0
        assert len(service.requests) == 1
