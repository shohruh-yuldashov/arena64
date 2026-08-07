"""The password reset flow end to end — real PostgreSQL, real Argon2id,
real JWTs, real SHA-256, real `ConsoleEmailProvider`.

`tests/unit/test_password_reset_service.py` covers the orchestration with
fakes and `tests/unit/test_password_reset_api.py` covers the HTTP surface.
This file covers the thing neither can: that register -> sign in ->
forget -> reset -> sign in again composes into a flow that actually works
against real storage.

That distinction earns its runtime here more than almost anywhere else on
the platform, because this flow is the first one that spans **four**
independently-built pieces that were each green against a stub of the
others:

    the reset token is written by one repository and read by another
      code path;
    the password is hashed by `auth` and stored by `users` through a
      port neither module's tests exercise together;
    the sessions are revoked by a service that reached this flow through
      a dependency factory, and a factory that built a *second*
      `SessionService` on a different unit of work would revoke into a
      transaction nobody commits;
    and the token the person actually receives comes out of an email,
      not out of a return value.

The last one is the reason this file reads the token out of the log rather
than out of `IssuedResetToken`: a link that is correct in the service and
malformed in the message is a bug the service tests are structurally
unable to see.

**No `dependency_overrides` anywhere in this file.** The app is the real
one, wired through the real composition root, with only the database
session redirected into the test's rolled-back transaction. Overriding a
service here would put the thing under test behind a fake.

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

import logging
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.contract.contract_app import build_contract_app, contract_client

REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
REFRESH_URL = "/api/v1/auth/refresh"
ME_URL = "/api/v1/auth/me"
FORGOT_URL = "/api/v1/auth/password/forgot"
RESET_URL = "/api/v1/auth/password/reset"

OLD_PASSWORD = "CorrectHorse1!"
NEW_PASSWORD = "BrandNewHorse2!"


@pytest_asyncio.fixture
async def client(contract_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """The production app over the test's rolled-back transaction.

    Everything the endpoints reach — the hasher, the token provider, the
    repositories, the composition root — is what ships; only `lifespan`'s
    state is stood in for (`tests/contract/contract_app.py`).

    `httpx.AsyncClient` over ASGI rather than `fastapi.TestClient`, for the
    event-loop reason `tests/contract/test_auth_api.py` documents at
    length."""
    async with contract_client(build_contract_app(contract_session)) as http:
        yield http


def credentials() -> dict[str, str]:
    """A unique account per call — the tests share one database and the
    unique constraints are real."""
    suffix = uuid4().hex[:10]
    return {
        "username": f"player{suffix}",
        "email": f"{suffix}@example.com",
        "password": OLD_PASSWORD,
    }


async def register(client: AsyncClient, body: dict[str, str] | None = None) -> dict[str, str]:
    account = body or credentials()
    response = await client.post(REGISTER_URL, json=account)
    assert response.status_code == 201, response.text
    return account


async def sign_in(client: AsyncClient, *, email: str, password: str) -> dict[str, Any]:
    response = await client.post(LOGIN_URL, json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    tokens: dict[str, Any] = response.json()["data"]
    return tokens


def reset_token_from_log(caplog: pytest.LogCaptureFixture) -> str:
    """Reads the reset token out of `ConsoleEmailProvider`'s output.

    That provider logging the link is the whole reason it exists — it is
    the development transport, and one that redacted the token would print
    a message nobody can act on. Here it doubles as the only way a test can
    obtain the token the way a *person* does: out of the delivered message,
    rather than out of the service's return value.

    Scans for the reset page specifically, not merely `token=`, because
    registration has already delivered a *verification* link into the same
    log by the time most of these tests run — and picking the wrong one up
    is precisely the confusion this whole flow must not make.
    """
    for record in reversed(caplog.records):
        message = record.getMessage()
        if "reset-password" in message and "token=" in message:
            return message.split("token=")[1].split()[0]
    raise AssertionError("no password reset link was delivered")


async def request_reset(client: AsyncClient, caplog: pytest.LogCaptureFixture, email: str) -> str:
    with caplog.at_level(logging.WARNING):
        response = await client.post(FORGOT_URL, json={"email": email})
    assert response.status_code == 204, response.text
    return reset_token_from_log(caplog)


class TestTheHappyPath:
    async def test_a_person_who_forgot_their_password_can_sign_in_again(
        self, client: AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The whole flow, in the order a person performs it. If this
        passes and everything else in this file fails, the feature
        works."""
        account = await register(client)

        token = await request_reset(client, caplog, account["email"])
        reset = await client.post(RESET_URL, json={"token": token, "password": NEW_PASSWORD})

        assert reset.status_code == 204, reset.text
        tokens = await sign_in(client, email=account["email"], password=NEW_PASSWORD)
        assert tokens["access_token"]

    async def test_the_old_password_stops_working(
        self, client: AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Against real Argon2id. A reset that left the old credential
        verifiable would be no reset at all, and only a real hasher can
        prove the stored hash actually changed."""
        account = await register(client)
        token = await request_reset(client, caplog, account["email"])

        await client.post(RESET_URL, json={"token": token, "password": NEW_PASSWORD})

        response = await client.post(
            LOGIN_URL, json={"email": account["email"], "password": OLD_PASSWORD}
        )
        assert response.status_code == 401
        assert response.json()["code"] == "invalid_credentials"

    async def test_the_new_password_is_stored_hashed(
        self,
        client: AsyncClient,
        caplog: pytest.LogCaptureFixture,
        contract_session: AsyncSession,
    ) -> None:
        """Read straight out of the table. The password must not be
        recoverable from a database read — a backup, a replica, a support
        query (§14.1)."""
        account = await register(client)
        token = await request_reset(client, caplog, account["email"])

        await client.post(RESET_URL, json={"token": token, "password": NEW_PASSWORD})

        stored = (
            await contract_session.execute(
                text('SELECT password_hash FROM users."user" WHERE email = :email'),
                {"email": account["email"]},
            )
        ).scalar_one()
        assert NEW_PASSWORD not in stored
        assert stored.startswith("$argon2id$")

    async def test_the_raw_token_is_never_stored(
        self,
        client: AsyncClient,
        caplog: pytest.LogCaptureFixture,
        contract_session: AsyncSession,
    ) -> None:
        """§4.5: "a database read ... must not yield a working password
        reset". Asserted against the table rather than against the entity,
        because the entity having no such field proves nothing about what
        the repository wrote."""
        account = await register(client)
        token = await request_reset(client, caplog, account["email"])

        hits = (
            await contract_session.execute(
                text(
                    "SELECT count(*) FROM auth.password_reset_tokens "
                    "WHERE encode(token_hash, 'escape') LIKE :fragment"
                ),
                {"fragment": f"%{token[:12]}%"},
            )
        ).scalar()
        assert hits == 0


class TestSessionInvalidation:
    async def test_every_refresh_token_stops_working(
        self, client: AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The requirement that spans the most machinery: two real
        sessions, a real reset, and two real refresh attempts against the
        real session table."""
        account = await register(client)
        phone = await sign_in(client, email=account["email"], password=OLD_PASSWORD)
        laptop = await sign_in(client, email=account["email"], password=OLD_PASSWORD)
        token = await request_reset(client, caplog, account["email"])

        await client.post(RESET_URL, json={"token": token, "password": NEW_PASSWORD})

        for device in (phone, laptop):
            response = await client.post(
                REFRESH_URL, json={"refresh_token": device["refresh_token"]}
            )
            assert response.status_code == 401, response.text
            assert response.json()["code"] == "invalid_session"

    async def test_the_sessions_are_recorded_as_revoked_for_a_password_change(
        self,
        client: AsyncClient,
        caplog: pytest.LogCaptureFixture,
        contract_session: AsyncSession,
    ) -> None:
        account = await register(client)
        await sign_in(client, email=account["email"], password=OLD_PASSWORD)
        token = await request_reset(client, caplog, account["email"])

        await client.post(RESET_URL, json={"token": token, "password": NEW_PASSWORD})

        reasons = (
            (
                await contract_session.execute(
                    text(
                        "SELECT DISTINCT s.revoked_reason FROM auth.user_sessions s "
                        'JOIN users."user" u ON u.id = s.user_id WHERE u.email = :email'
                    ),
                    {"email": account["email"]},
                )
            )
            .scalars()
            .all()
        )
        assert list(reasons) == ["password_change"]

    async def test_a_signed_in_session_elsewhere_is_untouched(
        self, client: AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Blast radius, against the real table. Revoking with a missing
        predicate would sign the whole platform out, and would look
        correct in any single-account test."""
        victim = await register(client)
        bystander = await register(client)
        theirs = await sign_in(client, email=bystander["email"], password=OLD_PASSWORD)
        token = await request_reset(client, caplog, victim["email"])

        await client.post(RESET_URL, json={"token": token, "password": NEW_PASSWORD})

        response = await client.post(REFRESH_URL, json={"refresh_token": theirs["refresh_token"]})
        assert response.status_code == 200, response.text

    async def test_the_reset_issues_no_new_session(
        self, client: AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Control of an inbox is not knowledge of a password. The client's
        next call is `POST /auth/login`."""
        account = await register(client)
        token = await request_reset(client, caplog, account["email"])

        response = await client.post(RESET_URL, json={"token": token, "password": NEW_PASSWORD})

        assert response.status_code == 204
        assert response.content == b""


class TestTokenLifecycle:
    async def test_a_used_token_is_refused(
        self, client: AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        account = await register(client)
        token = await request_reset(client, caplog, account["email"])
        await client.post(RESET_URL, json={"token": token, "password": NEW_PASSWORD})

        replay = await client.post(RESET_URL, json={"token": token, "password": "ThirdChoice3!"})

        assert replay.status_code == 422
        assert replay.json()["code"] == "invalid_reset_token"

    async def test_a_replay_does_not_change_the_password_again(
        self, client: AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The one that matters most. An attacker replaying a captured link
        must not be able to overwrite the password the legitimate owner
        just set — proved by signing in with the owner's choice
        afterwards."""
        account = await register(client)
        token = await request_reset(client, caplog, account["email"])
        await client.post(RESET_URL, json={"token": token, "password": NEW_PASSWORD})

        await client.post(RESET_URL, json={"token": token, "password": "AttackerChoice3!"})

        await sign_in(client, email=account["email"], password=NEW_PASSWORD)
        refused = await client.post(
            LOGIN_URL, json={"email": account["email"], "password": "AttackerChoice3!"}
        )
        assert refused.status_code == 401

    async def test_asking_twice_kills_the_first_link(
        self, client: AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The partial unique index doing its job through the whole
        stack."""
        account = await register(client)
        first = await request_reset(client, caplog, account["email"])
        second = await request_reset(client, caplog, account["email"])
        assert first != second

        stale = await client.post(RESET_URL, json={"token": first, "password": NEW_PASSWORD})

        assert stale.status_code == 422
        assert stale.json()["code"] == "invalid_reset_token"

    async def test_the_newest_link_still_works(
        self, client: AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        account = await register(client)
        await request_reset(client, caplog, account["email"])
        newest = await request_reset(client, caplog, account["email"])

        response = await client.post(RESET_URL, json={"token": newest, "password": NEW_PASSWORD})

        assert response.status_code == 204, response.text

    async def test_a_reset_leaves_no_live_token_behind(
        self,
        client: AsyncClient,
        caplog: pytest.LogCaptureFixture,
        contract_session: AsyncSession,
    ) -> None:
        account = await register(client)
        token = await request_reset(client, caplog, account["email"])

        await client.post(RESET_URL, json={"token": token, "password": NEW_PASSWORD})

        live = (
            await contract_session.execute(
                text(
                    "SELECT count(*) FROM auth.password_reset_tokens t "
                    'JOIN users."user" u ON u.id = t.user_id '
                    "WHERE u.email = :email AND t.used_at IS NULL"
                ),
                {"email": account["email"]},
            )
        ).scalar()
        assert live == 0

    async def test_an_unknown_token_is_refused(self, client: AsyncClient) -> None:
        response = await client.post(
            RESET_URL, json={"token": "never-issued-by-anyone", "password": NEW_PASSWORD}
        )

        assert response.status_code == 422
        assert response.json()["code"] == "invalid_reset_token"

    async def test_a_verification_token_cannot_reset_a_password(
        self, client: AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The two credentials live in different tables and are different
        types. Registration delivers a verification link; presenting it
        here must fail, or every unverified account would be resettable by
        anyone who saw the welcome email."""
        with caplog.at_level(logging.WARNING):
            await register(client)

        verification = next(
            record.getMessage().split("token=")[1].split()[0]
            for record in reversed(caplog.records)
            if "verify-email" in record.getMessage()
        )

        response = await client.post(
            RESET_URL, json={"token": verification, "password": NEW_PASSWORD}
        )

        assert response.status_code == 422
        assert response.json()["code"] == "invalid_reset_token"


class TestEnumerationResistance:
    async def test_an_unknown_address_answers_identically(self, client: AsyncClient) -> None:
        account = await register(client)

        known = await client.post(FORGOT_URL, json={"email": account["email"]})
        unknown = await client.post(FORGOT_URL, json={"email": f"{uuid4().hex}@example.com"})

        assert known.status_code == unknown.status_code == 204
        assert known.content == unknown.content == b""

    async def test_an_unknown_address_writes_no_token(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        before = (
            await contract_session.execute(text("SELECT count(*) FROM auth.password_reset_tokens"))
        ).scalar()

        await client.post(FORGOT_URL, json={"email": f"{uuid4().hex}@example.com"})

        after = (
            await contract_session.execute(text("SELECT count(*) FROM auth.password_reset_tokens"))
        ).scalar()
        assert after == before


class TestPasswordPolicy:
    async def test_a_weak_password_is_refused(
        self, client: AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        account = await register(client)
        token = await request_reset(client, caplog, account["email"])

        response = await client.post(RESET_URL, json={"token": token, "password": "weak"})

        assert response.status_code == 422

    async def test_the_same_policy_registration_enforces(
        self, client: AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """One policy, not two. A password this endpoint accepts must be
        one registration would have accepted — otherwise the weaker rule is
        reachable by anyone with an inbox."""
        weak = "nospecials123"
        rejected_at_registration = await client.post(
            REGISTER_URL, json={**credentials(), "password": weak}
        )
        assert rejected_at_registration.status_code == 422

        account = await register(client)
        token = await request_reset(client, caplog, account["email"])

        rejected_at_reset = await client.post(RESET_URL, json={"token": token, "password": weak})
        assert rejected_at_reset.status_code == 422
        assert rejected_at_reset.json()["code"] == rejected_at_registration.json()["code"]

    async def test_a_weak_password_leaves_the_link_usable(
        self, client: AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        account = await register(client)
        token = await request_reset(client, caplog, account["email"])
        await client.post(RESET_URL, json={"token": token, "password": "weak"})

        retry = await client.post(RESET_URL, json={"token": token, "password": NEW_PASSWORD})

        assert retry.status_code == 204, retry.text

    async def test_the_rejected_password_is_absent_from_the_response(
        self, client: AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        account = await register(client)
        token = await request_reset(client, caplog, account["email"])

        response = await client.post(RESET_URL, json={"token": token, "password": "hunter2"})

        assert "hunter2" not in response.text


class TestUnverifiedAndInactiveAccounts:
    async def test_an_unverified_account_can_still_reset(
        self, client: AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Registration leaves `is_verified` false, so every account in
        this file is unverified — which is the case worth stating
        explicitly rather than relying on. Refusing here would strand
        anybody who registered, never clicked the verification link, and
        then forgot their password."""
        account = await register(client)
        token = await request_reset(client, caplog, account["email"])

        response = await client.post(RESET_URL, json={"token": token, "password": NEW_PASSWORD})

        assert response.status_code == 204, response.text
        tokens = await sign_in(client, email=account["email"], password=NEW_PASSWORD)
        me = await client.get(ME_URL, headers={"Authorization": f"Bearer {tokens['access_token']}"})
        assert me.json()["data"]["is_verified"] is False

    async def test_a_deactivated_account_gets_no_link(
        self,
        client: AsyncClient,
        caplog: pytest.LogCaptureFixture,
        contract_session: AsyncSession,
    ) -> None:
        """Somebody who cannot sign in must not have their credential
        rotated by a stranger who knows their address."""
        account = await register(client)
        await contract_session.execute(
            text('UPDATE users."user" SET is_active = false WHERE email = :email'),
            {"email": account["email"]},
        )

        with caplog.at_level(logging.WARNING):
            response = await client.post(FORGOT_URL, json={"email": account["email"]})

        assert response.status_code == 204
        assert response.content == b""
        with pytest.raises(AssertionError):
            reset_token_from_log(caplog)


class TestLogging:
    async def test_the_raw_token_never_reaches_an_application_log(
        self, client: AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """`ConsoleEmailProvider` logs the link on purpose — it *is* the
        transport, and it refuses to construct in a production-like
        environment. Nothing else may, which is what this asserts: the
        token appears in the provider's record and in no other."""
        account = await register(client)
        token = await request_reset(client, caplog, account["email"])

        with caplog.at_level(logging.DEBUG):
            await client.post(RESET_URL, json={"token": token, "password": NEW_PASSWORD})

        leaked = [
            record.name
            for record in caplog.records
            if token in record.getMessage()
            and record.name != "app.platform.email.console"
        ]
        assert leaked == []

    async def test_the_new_password_never_reaches_a_log(
        self, client: AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        account = await register(client)
        token = await request_reset(client, caplog, account["email"])

        with caplog.at_level(logging.DEBUG):
            await client.post(RESET_URL, json={"token": token, "password": NEW_PASSWORD})

        assert NEW_PASSWORD not in caplog.text
