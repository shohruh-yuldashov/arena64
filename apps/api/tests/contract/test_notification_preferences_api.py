"""Notification preferences, end to end — A64-021.3 §32.

Three claims, and each needs a real database and the real application graph.

  **The matrix a client receives is the matrix the backend enforces.** The
  read returns every pair with its default resolved, a save round-trips, and
  a second read agrees — so a client never has to reimplement
  `default_enabled` and the two can never drift.

  **A refusal is a refusal on the wire.** A locked change answers `422` with
  the code that says *which* kind of locked, and the table did not move.
  §5 is explicit that the frontend must not be the thing enforcing this, and
  a hand-written request reaches the same check a form does.

  **§10 is the one that matters.** A muted category produces **no durable
  row and no realtime announcement** — not a row that is filtered on read.
  It is asserted through `build_durable_notification_writer`, the factory
  `app_factory` itself calls, so a pass here is a pass on the deployed
  worker path (§33).

The relay is driven explicitly rather than by the timer, and the helpers are
shared with `test_notifications_api.py` for the same reason: staging a real
friend request is the only honest way to produce a notification, because
there is no endpoint that creates one.

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

from collections.abc import AsyncIterator
from typing import Any

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.notifications.domain.preference import DeliveryChannel
from app.modules.notifications.domain.record import NotificationCategory
from app.modules.notifications.infrastructure import SqlAlchemyNotificationRepository
from tests.contract.contract_app import build_contract_app, contract_client
from tests.contract.test_notifications_api import (
    NOTIFICATIONS_URL,
    REQUESTS_URL,
    UNREAD_URL,
    RecordingAnnouncer,
    drain,
    register,
)

PREFERENCES_URL = f"{NOTIFICATIONS_URL}/preferences"


@pytest_asyncio.fixture
async def client(contract_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """The production app, with the **real** event publisher."""
    async with contract_client(build_contract_app(contract_session)) as http:
        yield http


def cell(body: dict[str, Any], category: str, channel: str) -> dict[str, Any]:
    return next(
        setting
        for setting in body["settings"]
        if setting["category"] == category and setting["channel"] == channel
    )


class TestReadingAndWriting:
    async def test_the_matrix_is_complete_and_a_save_round_trips(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """§32.1. Every pair, defaults resolved, and a change that survives.

        The response of the `PATCH` is asserted to equal the response of the
        `GET` that follows it, which is §17's actual claim: a save costs one
        request because what it returns *is* what a fresh read would say.
        """
        alice = await register(client, contract_session)

        initial = (await client.get(PREFERENCES_URL, headers=alice.auth)).json()["data"]
        # Every category on every channel, with nothing stored — a player
        # who has never opened this screen still receives the whole grid.
        #
        # Derived rather than hardcoded: the count was `12` and A64-027A's
        # fifth category made it `15`, which is the grid growing correctly
        # rather than a regression. A literal here fails on the *right*
        # change and says nothing about the invariant.
        assert len(initial["settings"]) == len(NotificationCategory) * len(DeliveryChannel)
        assert {(setting["category"], setting["channel"]) for setting in initial["settings"]} == {
            (category.value, channel.value)
            for category in NotificationCategory
            for channel in DeliveryChannel
        }
        assert cell(initial, "social", "in_app")["enabled"] is True
        assert cell(initial, "social", "email") == {
            "category": "social",
            "channel": "email",
            "enabled": False,
            "available": False,
            "editable": False,
            "locked_reason": "channel_unavailable",
        }

        saved = await client.patch(
            PREFERENCES_URL,
            headers=alice.auth,
            json={"changes": [{"category": "social", "channel": "in_app", "enabled": False}]},
        )
        assert saved.status_code == 200, saved.text

        reread = (await client.get(PREFERENCES_URL, headers=alice.auth)).json()["data"]
        assert saved.json()["data"] == reread
        assert cell(reread, "social", "in_app")["enabled"] is False
        # Only the named pair moved. A `PATCH` that reset the rest would be
        # a save that undoes a switch the client never rendered.
        assert cell(reread, "game", "in_app")["enabled"] is True

    async def test_an_announcement_is_on_by_default_and_a_player_may_mute_it(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """A64-027A, ADR-006 — the safety property of the fifth category.

        An administrative broadcast reaches a player because they have not
        said otherwise, not because the platform refuses to let them. If
        this cell were ever locked, one dropdown value in the console would
        reach every muted inbox — which is why `ANNOUNCEMENT` is absent from
        `preference.LOCKED` and why that absence is asserted here rather
        than trusted.
        """
        alice = await register(client, contract_session)

        initial = (await client.get(PREFERENCES_URL, headers=alice.auth)).json()["data"]
        announcement = cell(initial, "announcement", "in_app")
        assert announcement["enabled"] is True
        assert announcement["editable"] is True
        assert announcement["locked_reason"] is None

        saved = await client.patch(
            PREFERENCES_URL,
            headers=alice.auth,
            json={"changes": [{"category": "announcement", "channel": "in_app", "enabled": False}]},
        )
        assert saved.status_code == 200, saved.text
        assert cell(saved.json()["data"], "announcement", "in_app")["enabled"] is False

    async def test_a_locked_change_is_refused_and_nothing_is_written(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """§32.2. One illegal change rejects the whole request.

        The legal half of the batch is deliberately listed *first*, so a
        service that validated as it wrote would have committed it before
        reaching the refusal — and the re-read would show it.
        """
        alice = await register(client, contract_session)

        refused = await client.patch(
            PREFERENCES_URL,
            headers=alice.auth,
            json={
                "changes": [
                    {"category": "game", "channel": "in_app", "enabled": False},
                    {"category": "system", "channel": "in_app", "enabled": False},
                ]
            },
        )

        assert refused.status_code == 422, refused.text
        assert refused.json()["code"] == "notification_preference_locked"

        after = (await client.get(PREFERENCES_URL, headers=alice.auth)).json()["data"]
        assert cell(after, "game", "in_app")["enabled"] is True
        assert cell(after, "system", "in_app")["enabled"] is True


class TestPreferencesGovernDelivery:
    async def test_a_muted_category_produces_no_row_and_no_announcement(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """§32.3, and the claim the whole phase rests on — §10.

        Bob mutes social in-app, Alice sends him a friend request, and the
        relay runs the production sink graph over the resulting event.

        Four things must all be true, and "filtered on read" satisfies only
        the first: his list is empty, his badge is zero, **the table holds no
        row**, and **no realtime frame was published**. The last two are what
        distinguish prevention from hiding.

        Verified to fail without the filter: removing the policy check from
        `DurableNotificationWriter.deliver` makes this test red and leaves
        the other two green, which is the shape a regression test should
        have — the assertion is about the suppression and nothing else.
        """
        alice, bob = (
            await register(client, contract_session),
            await register(client, contract_session),
        )

        muted = await client.patch(
            PREFERENCES_URL,
            headers=bob.auth,
            json={"changes": [{"category": "social", "channel": "in_app", "enabled": False}]},
        )
        assert muted.status_code == 200, muted.text

        sent = await client.post(REQUESTS_URL, headers=alice.auth, json={"player_id": str(bob.id)})
        assert sent.status_code == 201, sent.text

        announcer = RecordingAnnouncer(contract_session)
        await drain(contract_session, announcer)

        assert (await client.get(NOTIFICATIONS_URL, headers=bob.auth)).json()["data"][
            "entries"
        ] == []
        assert (await client.get(UNREAD_URL, headers=bob.auth)).json()["data"]["unread_count"] == 0
        # Not written and then hidden: the table itself is empty for him.
        assert (
            await SqlAlchemyNotificationRepository(contract_session).list_for(
                bob.id, after=None, limit=50
            )
        ).entries == []
        # And nothing was pushed at him either.
        assert [a for a in announcer.announced if a.recipient_id == bob.id] == []
