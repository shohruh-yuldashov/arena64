"""The six-digit verification flow, end to end — A64-021.5H §31.

Against real PostgreSQL and through the real application, with the code read
out of the delivered message the way a person reads it. §8 forbids an
automated test contacting a mail provider; `ConsoleEmailProvider` is the
development transport and its log is the inbox.

## What is asserted, and what is deliberately not

The **state machine**: only the latest code works, five wrong guesses end
the challenge, a malformed field costs nothing, expiry is its own answer,
and a second success is not an error.

The **policy**: an unverified session may finish verifying and may do
nothing else that reaches another player.

Not the copy, not the HTML, and not that `secrets` is random. The first two
belong to the template's own unit tests and the third is not a property a
test can establish.

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

import logging
import re
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth.domain.otp import OTP_MAX_ATTEMPTS
from app.modules.auth.infrastructure.models import EmailVerificationTokenModel
from tests.contract.contract_app import build_contract_app, contract_client

REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
VERIFY_CODE_URL = "/api/v1/auth/email/verify-code"
RESEND_CODE_URL = "/api/v1/auth/email/resend-code"
ME_URL = "/api/v1/auth/me"
PASSWORD = "CorrectHorse1!"


@pytest.fixture
async def client(contract_session: AsyncSession) -> Any:
    async with contract_client(build_contract_app(contract_session)) as http:
        yield http


def code_from_log(caplog: pytest.LogCaptureFixture) -> str:
    """The six digits, out of the delivered message.

    The **last** message, because a resend supersedes the code before it and
    only the latest one works (§2).
    """
    for record in reversed(caplog.records):
        match = re.search(r"^\s{4}(\d{6})$", record.getMessage(), re.M)
        if match:
            return match.group(1)
    raise AssertionError("no verification code was delivered")


async def register(client: AsyncClient, caplog: pytest.LogCaptureFixture) -> dict[str, Any]:
    """A new account, its session, and the code it was sent."""
    suffix = uuid4().hex[:8]
    body = {
        "username": f"player{suffix}",
        "email": f"{suffix}@example.com",
        "password": PASSWORD,
    }
    with caplog.at_level(logging.WARNING):
        created = await client.post(REGISTER_URL, json=body)
    assert created.status_code == 201, created.text

    signed_in = await client.post(LOGIN_URL, json={"email": body["email"], "password": PASSWORD})
    assert signed_in.status_code == 200, signed_in.text
    return {
        "id": created.json()["data"]["id"],
        "email": body["email"],
        "code": code_from_log(caplog),
        "auth": {"Authorization": f"Bearer {signed_in.json()['data']['access_token']}"},
    }


def wrong(code: str) -> str:
    """A well-formed code that is not this one."""
    return "000000" if code != "000000" else "111111"


class TestRegistration:
    async def test_registration_leaves_the_account_unverified_with_a_live_code(
        self, client: AsyncClient, caplog: pytest.LogCaptureFixture, contract_session: AsyncSession
    ) -> None:
        """§31.1. A code was issued and the account is not yet verified.

        The stored row is asserted rather than only the response, because
        the claim is that a *challenge exists* — and because it is the one
        place that proves the code was not stored in a form anybody could
        read back.
        """
        account = await register(client, caplog)

        me = await client.get(ME_URL, headers=account["auth"])
        assert me.json()["data"]["is_verified"] is False

        row = await contract_session.scalar(
            select(EmailVerificationTokenModel).where(
                EmailVerificationTokenModel.user_id == account["id"]
            )
        )
        assert row is not None
        assert (row.kind, row.attempt_count, row.used_at) == ("otp", 0, None)
        # The code is nowhere in the row — what is stored is a keyed
        # verifier, and 32 bytes of it.
        assert account["code"].encode() not in row.token_hash
        assert len(row.token_hash) == 32


class TestVerifying:
    async def test_the_delivered_code_verifies_the_account(
        self, client: AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """§31.3. The whole point, once."""
        account = await register(client, caplog)

        response = await client.post(
            VERIFY_CODE_URL, json={"code": account["code"]}, headers=account["auth"]
        )

        assert response.status_code == 200, response.text
        assert response.json()["data"]["is_verified"] is True
        # Read back through a different endpoint, so this is the committed
        # row rather than the response the write path happened to build.
        me = await client.get(ME_URL, headers=account["auth"])
        assert me.json()["data"]["is_verified"] is True

    async def test_verifying_twice_succeeds_twice(
        self, client: AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """§31.7, §23. Idempotent, deliberately.

        A code submitted in a second tab after the first one verified is not
        a mistake the person made, and answering `422` for the state they
        wanted would report a race as their error.
        """
        account = await register(client, caplog)
        await client.post(VERIFY_CODE_URL, json={"code": account["code"]}, headers=account["auth"])

        again = await client.post(
            VERIFY_CODE_URL, json={"code": account["code"]}, headers=account["auth"]
        )

        assert again.status_code == 200, again.text
        assert again.json()["data"]["is_verified"] is True

    async def test_only_the_latest_code_works(
        self, client: AsyncClient, caplog: pytest.LogCaptureFixture, contract_session: AsyncSession
    ) -> None:
        """§31.2. A resend destroys the code before it.

        The cooldown is stepped over by ageing the stored row rather than by
        sleeping: §6.4 rules out a suite that depends on wall-clock time,
        and the row is where the cooldown is measured from.
        """
        account = await register(client, caplog)
        original = account["code"]
        await _age_challenge(contract_session, account["id"], by=timedelta(minutes=2))

        with caplog.at_level(logging.WARNING):
            resent = await client.post(RESEND_CODE_URL, headers=account["auth"])
        assert resent.status_code == 202, resent.text
        newest = code_from_log(caplog)
        assert newest != original

        stale = await client.post(VERIFY_CODE_URL, json={"code": original}, headers=account["auth"])
        assert stale.status_code == 422
        assert stale.json()["code"] == "email_verification_code_invalid"

        current = await client.post(VERIFY_CODE_URL, json={"code": newest}, headers=account["auth"])
        assert current.status_code == 200, current.text

    async def test_an_expired_code_says_so(
        self, client: AsyncClient, caplog: pytest.LogCaptureFixture, contract_session: AsyncSession
    ) -> None:
        """§31.6. Its own answer, because the recovery differs: retyping is
        pointless and the client should offer a resend."""
        account = await register(client, caplog)
        await _age_challenge(contract_session, account["id"], by=timedelta(minutes=30))

        response = await client.post(
            VERIFY_CODE_URL, json={"code": account["code"]}, headers=account["auth"]
        )

        assert response.status_code == 422
        assert response.json()["code"] == "email_verification_code_expired"


class TestAttempts:
    async def test_a_wrong_code_costs_one_attempt_and_a_malformed_one_costs_none(
        self, client: AsyncClient, caplog: pytest.LogCaptureFixture, contract_session: AsyncSession
    ) -> None:
        """§31.4, §10. The distinction is what stops a client's own bug
        locking somebody out of their account."""
        account = await register(client, caplog)

        malformed = await client.post(
            VERIFY_CODE_URL, json={"code": "abc"}, headers=account["auth"]
        )
        assert malformed.status_code == 422
        assert await _attempts(contract_session, account["id"]) == 0

        wrong_code = await client.post(
            VERIFY_CODE_URL, json={"code": wrong(account["code"])}, headers=account["auth"]
        )
        assert wrong_code.json()["code"] == "email_verification_code_invalid"
        assert await _attempts(contract_session, account["id"]) == 1

    async def test_the_challenge_dies_at_the_attempt_limit(
        self, client: AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """§31.5. Five guesses, and then the **correct** code stops working.

        Asserting on the right code is the whole test: a limit that merely
        rejected further wrong guesses would leave the challenge alive, and
        five-in-a-million becomes ten and then a hundred.
        """
        account = await register(client, caplog)

        for _ in range(OTP_MAX_ATTEMPTS):
            await client.post(
                VERIFY_CODE_URL,
                json={"code": wrong(account["code"])},
                headers=account["auth"],
            )

        correct = await client.post(
            VERIFY_CODE_URL, json={"code": account["code"]}, headers=account["auth"]
        )

        assert correct.status_code == 422
        assert correct.json()["code"] == "email_verification_code_invalid"
        me = await client.get(ME_URL, headers=account["auth"])
        assert me.json()["data"]["is_verified"] is False


class TestResend:
    async def test_a_resend_inside_the_cooldown_is_refused_with_retry_after(
        self, client: AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """§31, §11. The server is the authority, and it says how long.

        `409` with `Retry-After`, which is the shape this platform already
        uses for a cooldown — the matchmaking decline bar — because the
        caller did nothing wrong and no budget was spent.
        """
        account = await register(client, caplog)

        response = await client.post(RESEND_CODE_URL, headers=account["auth"])

        assert response.status_code == 409
        assert response.json()["code"] == "email_verification_resend_too_soon"
        assert int(response.headers["retry-after"]) > 0

    async def test_resending_after_verification_is_refused_clearly(
        self, client: AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A verified account has nothing to send.

        Safe to say plainly here and not on the anonymous `/email/resend`:
        the caller has already proved they are this account, so the answer
        tells them about themselves.
        """
        account = await register(client, caplog)
        await client.post(VERIFY_CODE_URL, json={"code": account["code"]}, headers=account["auth"])

        response = await client.post(RESEND_CODE_URL, headers=account["auth"])

        assert response.status_code == 409
        assert response.json()["code"] == "email_already_verified"


class TestVerifiedUserPolicy:
    async def test_an_unverified_session_may_finish_verifying_and_nothing_else(
        self, client: AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """§31.9, §31.10 together, because they are one claim.

        The guard is only correct if **both** halves hold: a write that
        reaches another player is refused, and the endpoints the
        verification screen itself needs are not. Asserting either alone
        would pass with a guard on everything or a guard on nothing.
        """
        account = await register(client, caplog)
        stranger = await register(client, caplog)

        blocked = {
            "friend request": await client.post(
                "/api/v1/friends/requests",
                headers=account["auth"],
                json={"player_id": stranger["id"]},
            ),
            "queue": await client.post(
                "/api/v1/matchmaking/queue",
                headers=account["auth"],
                json={"variant": "russian_8x8", "time_control": "blitz_3_2", "rated": True},
            ),
            "profile edit": await client.patch(
                "/api/v1/profile", headers=account["auth"], json={"display_name": "Nope"}
            ),
        }
        for what, response in blocked.items():
            assert response.status_code == 403, f"{what}: {response.text}"
            assert response.json()["code"] == "email_verification_required", what

        # And the verification screen still works — reading own state,
        # asking for another code, and submitting one.
        assert (await client.get(ME_URL, headers=account["auth"])).status_code == 200
        assert (
            await client.get("/api/v1/notifications", headers=account["auth"])
        ).status_code == 200
        verified = await client.post(
            VERIFY_CODE_URL, json={"code": account["code"]}, headers=account["auth"]
        )
        assert verified.status_code == 200, verified.text

        # Verified, the same write is allowed through.
        allowed = await client.patch(
            "/api/v1/profile", headers=account["auth"], json={"display_name": "Now"}
        )
        assert allowed.status_code == 200, allowed.text


async def _age_challenge(session: AsyncSession, user_id: str, *, by: timedelta) -> None:
    """Moves a challenge back in time.

    Ageing the **row** rather than sleeping or injecting a clock, because
    the row is where both the expiry and the cooldown are measured from —
    which is the property under test in the two callers.
    """
    # Computed in Python and written as literals. SQL arithmetic would be
    # tidier and does not survive the column's `UtcDateTime` decorator,
    # which binds a `timedelta` as a timestamp.
    row = await session.scalar(
        select(EmailVerificationTokenModel).where(
            EmailVerificationTokenModel.user_id == user_id,
            EmailVerificationTokenModel.used_at.is_(None),
        )
    )
    assert row is not None
    await session.execute(
        update(EmailVerificationTokenModel)
        .where(EmailVerificationTokenModel.id == row.id)
        .values(created_at=row.created_at - by, expires_at=row.expires_at - by)
    )
    await session.flush()


async def _attempts(session: AsyncSession, user_id: str) -> int:
    total = await session.scalar(
        select(EmailVerificationTokenModel.attempt_count).where(
            EmailVerificationTokenModel.user_id == user_id,
            EmailVerificationTokenModel.used_at.is_(None),
        )
    )
    return int(total or 0)
