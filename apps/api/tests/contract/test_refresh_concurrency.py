"""Concurrent refresh against real PostgreSQL — A64-028.2 §12.

A64-028.1 reproduced the defect these tests now guard: two browser tabs
share one cookie jar, so both present the same refresh token, and the
second arrived after the first had rotated it. Reuse detection burned the
family — including the successor the first tab had just been issued — and
signed both tabs out while raising the platform's only theft alarm.

**Every test here opens its own `AsyncSession`.** `contract_session` binds
one connection inside a transaction it rolls back, which is right for a
request-shaped test and cannot express two callers at once: both would be
the same transaction and neither could see the other's uncommitted work.
So these take the engine and commit for real, cleaning up after themselves.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.config.settings import SessionSettings
from app.core.clock import Clock
from app.database.unit_of_work import SessionUnitOfWork
from app.modules.admin.public import AccountRestriction
from app.modules.auth.application.services.refresh_token_service import RefreshTokenService
from app.modules.auth.application.services.session_service import SessionService
from app.modules.auth.domain.exceptions import (
    ConcurrentRotation,
    ExpiredRefreshToken,
    RevokedSession,
    SessionNotFound,
)
from app.modules.auth.domain.sessions import RevocationReason
from app.modules.auth.infrastructure.repositories.session_repository import (
    SqlAlchemySessionRepository,
)

pytestmark = pytest.mark.asyncio


class _FixedClock(Clock):
    """A clock the test moves, so a grace boundary is exact rather than slept."""

    def __init__(self, at: datetime) -> None:
        self._at = at

    def now(self) -> datetime:
        return self._at

    def advance(self, seconds: float) -> None:
        self._at += timedelta(seconds=seconds)


class _NoRestrictions:
    """Nobody is suspended. The parameter name matters: `AccountRestrictionGate`
    is a `Protocol`, so a fake that renames `player_id` is not one."""

    async def restriction_for(self, player_id: UUID, *, at: datetime) -> AccountRestriction | None:
        return None


def _service(session: AsyncSession, *, clock: Clock, grace_seconds: int = 10) -> SessionService:
    settings = SessionSettings(rotation_grace_seconds=grace_seconds)
    return SessionService(
        sessions=SqlAlchemySessionRepository(session),
        tokens=RefreshTokenService(settings),
        restrictions=_NoRestrictions(),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
        settings=settings,
    )


@pytest_asyncio.fixture
async def account(contract_engine: AsyncEngine) -> AsyncIterator[UUID]:
    """One throwaway account, and every row it owns removed afterwards."""
    user_id = uuid4()
    async with contract_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO users.user (id, email, username, password_hash, "
                "created_at, updated_at) VALUES (:i, :e, :u, 'x', now(), now())"
            ),
            {"i": user_id, "e": f"race-{user_id}@example.test", "u": f"r{str(user_id)[:10]}"},
        )
    yield user_id
    async with contract_engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM auth.user_sessions WHERE user_id = :i"), {"i": user_id}
        )
        await connection.execute(text("DELETE FROM users.user WHERE id = :i"), {"i": user_id})


async def _issue(engine: AsyncEngine, user_id: UUID, clock: Clock) -> tuple[str, UUID]:
    """A signed-in session: the token a browser holds, and its family."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        issued = await _service(session, clock=clock).create_session(user_id)
    return issued.refresh_token, issued.session.token_family


async def _rotate(
    engine: AsyncEngine, token: str, clock: Clock, *, grace_seconds: int = 10
) -> object:
    """One caller's whole refresh, on its own connection. Returns the
    outcome — a token or the exception — so a gather can report both."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        try:
            issued = await _service(
                session, clock=clock, grace_seconds=grace_seconds
            ).rotate_refresh_token(token)
        except Exception as exc:  # noqa: BLE001 — the outcome is the assertion
            return exc
        return issued.refresh_token


async def _family(engine: AsyncEngine, family: UUID) -> list[tuple[bool, str | None]]:
    """Every link in a chain: whether it is live, and why it is not."""
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT revoked_at IS NULL AS live, revoked_reason "
                    "FROM auth.user_sessions WHERE token_family = :f ORDER BY created_at"
                ),
                {"f": family},
            )
        ).all()
    return [(row.live, row.revoked_reason) for row in rows]


# --- case 1 ------------------------------------------------------------------


async def test_one_refresh_rotates_once(contract_engine: AsyncEngine, account: UUID) -> None:
    clock = _FixedClock(datetime.now(UTC))
    token, family = await _issue(contract_engine, account, clock)

    outcome = await _rotate(contract_engine, token, clock)

    assert isinstance(outcome, str)
    assert [live for live, _ in await _family(contract_engine, family)] == [False, True]


# --- cases 2 and 3: the defect A64-028.1 measured ----------------------------


@pytest.mark.parametrize("callers", [2, 3])
async def test_concurrent_refreshes_keep_the_family_alive(
    contract_engine: AsyncEngine, account: UUID, callers: int
) -> None:
    """The whole point of A64-028.2.

    Before the fix this produced `live: 0` — every link revoked, including
    the successor the winner had just been handed, and both tabs signed out.
    """
    clock = _FixedClock(datetime.now(UTC))
    token, family = await _issue(contract_engine, account, clock)

    outcomes = await asyncio.gather(
        *(_rotate(contract_engine, token, clock) for _ in range(callers))
    )

    # Exactly one caller rotates; the rest are told to try again.
    assert sum(isinstance(outcome, str) for outcome in outcomes) == 1
    losers = [outcome for outcome in outcomes if not isinstance(outcome, str)]
    assert all(isinstance(outcome, ConcurrentRotation) for outcome in losers)

    # And the chain still has a usable session — the successor survived.
    links = await _family(contract_engine, family)
    assert sum(live for live, _ in links) == 1
    assert not any(reason == RevocationReason.REUSE_DETECTED.value for _, reason in links)


# --- cases 4, 5, 6: the grace boundary ---------------------------------------


@pytest.mark.parametrize(
    ("elapsed", "graced"),
    [(9.0, True), (10.0, True), (10.001, False)],
    ids=["inside", "exactly at", "outside"],
)
async def test_the_grace_window_has_an_exact_edge(
    contract_engine: AsyncEngine, account: UUID, elapsed: float, graced: bool
) -> None:
    """Injected clock, not `sleep` — the boundary is asserted, not raced."""
    clock = _FixedClock(datetime.now(UTC))
    token, family = await _issue(contract_engine, account, clock)
    assert isinstance(await _rotate(contract_engine, token, clock), str)

    clock.advance(elapsed)
    replay = await _rotate(contract_engine, token, clock)

    if graced:
        assert isinstance(replay, ConcurrentRotation)
        assert sum(live for live, _ in await _family(contract_engine, family)) == 1
    else:
        assert isinstance(replay, RevokedSession)
        links = await _family(contract_engine, family)
        assert sum(live for live, _ in links) == 0
        assert any(reason == RevocationReason.REUSE_DETECTED.value for _, reason in links)


async def test_a_zero_window_never_grants_grace(
    contract_engine: AsyncEngine, account: UUID
) -> None:
    """`rotation_grace_seconds=0` is the kill switch, and it restores
    A64-028.1's behaviour exactly."""
    clock = _FixedClock(datetime.now(UTC))
    token, family = await _issue(contract_engine, account, clock)
    assert isinstance(await _rotate(contract_engine, token, clock, grace_seconds=0), str)

    replay = await _rotate(contract_engine, token, clock, grace_seconds=0)

    assert isinstance(replay, RevokedSession)
    assert sum(live for live, _ in await _family(contract_engine, family)) == 0


# --- case 7: real theft ------------------------------------------------------


async def test_a_kept_token_still_burns_the_family(
    contract_engine: AsyncEngine, account: UUID
) -> None:
    """Case B of §4, and the property that must never be traded away.

    An old token replayed after its successor has itself been used is a
    credential somebody kept. It is refused, and the chain dies.
    """
    clock = _FixedClock(datetime.now(UTC))
    stolen, family = await _issue(contract_engine, account, clock)

    successor = await _rotate(contract_engine, stolen, clock)
    assert isinstance(successor, str)
    clock.advance(60)
    assert isinstance(await _rotate(contract_engine, successor, clock), str)

    replay = await _rotate(contract_engine, stolen, clock)

    assert isinstance(replay, RevokedSession)
    links = await _family(contract_engine, family)
    assert sum(live for live, _ in links) == 0
    assert any(reason == RevocationReason.REUSE_DETECTED.value for _, reason in links)


# --- cases 8, 9, 10 ----------------------------------------------------------


async def test_a_signed_out_token_is_never_graced(
    contract_engine: AsyncEngine, account: UUID
) -> None:
    """Case C. A sign-out is deliberate, so its token stays refused however
    recently it happened — the grace tests the *reason*, not only the clock."""
    clock = _FixedClock(datetime.now(UTC))
    token, family = await _issue(contract_engine, account, clock)

    async with AsyncSession(contract_engine, expire_on_commit=False) as session:
        await _service(session, clock=clock).revoke_by_refresh_token(token)

    replay = await _rotate(contract_engine, token, clock)

    assert isinstance(replay, RevokedSession)
    assert sum(live for live, _ in await _family(contract_engine, family)) == 0


async def test_an_expired_token_is_rejected(contract_engine: AsyncEngine, account: UUID) -> None:
    """Case D — expiry is not a race, whatever the clock says about grace."""
    clock = _FixedClock(datetime.now(UTC))
    token, _ = await _issue(contract_engine, account, clock)

    clock.advance(timedelta(days=31).total_seconds())

    assert isinstance(await _rotate(contract_engine, token, clock), ExpiredRefreshToken)


async def test_an_unknown_token_is_rejected(contract_engine: AsyncEngine, account: UUID) -> None:
    """Case E — a token from no family at all."""
    clock = _FixedClock(datetime.now(UTC))
    await _issue(contract_engine, account, clock)

    outcome = await _rotate(contract_engine, "not-a-token-this-server-ever-issued", clock)

    assert isinstance(outcome, SessionNotFound)


# --- case 11 -----------------------------------------------------------------


async def test_a_concurrent_sign_out_does_not_grant_grace(
    contract_engine: AsyncEngine, account: UUID
) -> None:
    """Sign-out and refresh at once. Whichever order they land in, the
    session must end up signed out — the grace must not resurrect it."""
    clock = _FixedClock(datetime.now(UTC))
    token, family = await _issue(contract_engine, account, clock)

    async def sign_out() -> object:
        async with AsyncSession(contract_engine, expire_on_commit=False) as session:
            try:
                await _service(session, clock=clock).revoke_by_refresh_token(token)
            except Exception as exc:  # noqa: BLE001 — either order is legitimate
                return exc
            return None

    refresh, revoke = await asyncio.gather(_rotate(contract_engine, token, clock), sign_out())

    # Either order is correct, and both are asserted rather than assumed:
    # the sign-out won and the refresh was refused, or the refresh won and
    # the sign-out found the row already rotated.
    if isinstance(refresh, str):
        assert revoke is None or isinstance(revoke, ConcurrentRotation | RevokedSession)
    else:
        assert isinstance(refresh, ConcurrentRotation | RevokedSession)

    # What must never happen either way is a *reuse* burn: neither party
    # replayed anything, and a sign-out racing a refresh is not theft.
    links = await _family(contract_engine, family)
    assert not any(reason == RevocationReason.REUSE_DETECTED.value for _, reason in links)


# --- §10: the security signal ------------------------------------------------


async def test_a_benign_race_raises_no_theft_alarm(
    contract_engine: AsyncEngine, account: UUID, caplog: pytest.LogCaptureFixture
) -> None:
    """`refresh_token_reuse_detected` is the only evidence of a replayed
    token this platform produces. A64-028.1's defect made it fire on two
    ordinary tabs, which is what makes an alert on its rate unusable —
    so the signal's *silence* here is as load-bearing as the session state.
    """
    clock = _FixedClock(datetime.now(UTC))
    token, _ = await _issue(contract_engine, account, clock)

    with caplog.at_level("INFO"):
        outcomes = await asyncio.gather(*(_rotate(contract_engine, token, clock) for _ in range(2)))

    assert sum(isinstance(outcome, str) for outcome in outcomes) == 1
    messages = [record.message for record in caplog.records]
    assert "refresh_token_reuse_detected" not in messages
    assert "refresh_rotation_conflict" in messages


async def test_real_reuse_still_raises_the_alarm(
    contract_engine: AsyncEngine, account: UUID, caplog: pytest.LogCaptureFixture
) -> None:
    """And the alarm still works, or the change would have bought silence
    rather than accuracy."""
    clock = _FixedClock(datetime.now(UTC))
    stolen, _ = await _issue(contract_engine, account, clock)
    assert isinstance(await _rotate(contract_engine, stolen, clock), str)
    clock.advance(60)

    with caplog.at_level("INFO"):
        assert isinstance(await _rotate(contract_engine, stolen, clock), RevokedSession)

    messages = [record.message for record in caplog.records]
    assert "refresh_token_reuse_detected" in messages
    assert "refresh_rotation_conflict" not in messages


async def test_only_a_rotation_is_ever_a_race(contract_engine: AsyncEngine, account: UUID) -> None:
    """The `revoked_reason` guard, isolated — §4 cases C and E.

    The other two conditions cannot reach this one on their own. Within a
    chain only one link is live at a time, so a token revoked by sign-out,
    suspension or a password change normally leaves its family with nothing
    live and is refused by the liveness test before the reason is consulted
    — which is why removing the reason check breaks no other test here.

    So the state is **built rather than driven**: a family holding one live
    link and one revoked *by the player*. That combination is one no path in
    this codebase produces today, and the guard is what keeps it refused if
    one ever does. A grace window that reads "recently revoked" instead of
    "recently rotated" would hand a deliberate sign-out straight back.
    """
    clock = _FixedClock(datetime.now(UTC))
    signed_out, family = await _issue(contract_engine, account, clock)

    # A second, live link in the same family, and the first marked as an
    # ordinary sign-out a moment ago — well inside any grace window.
    async with AsyncSession(contract_engine, expire_on_commit=False) as session:
        await _service(session, clock=clock).create_session(account)
        await session.execute(
            text(
                "UPDATE auth.user_sessions SET token_family = :f "
                "WHERE user_id = :u AND revoked_at IS NULL"
            ),
            {"f": family, "u": account},
        )
        await session.execute(
            text(
                "UPDATE auth.user_sessions SET revoked_at = :t, revoked_reason = 'player' "
                "WHERE token_family = :f AND revoked_at IS NULL "
                "AND id = (SELECT id FROM auth.user_sessions WHERE token_family = :f "
                "ORDER BY created_at LIMIT 1)"
            ),
            {"t": clock.now(), "f": family},
        )
        await session.commit()

    replay = await _rotate(contract_engine, signed_out, clock)

    assert isinstance(replay, RevokedSession)
    links = await _family(contract_engine, family)
    assert any(reason == RevocationReason.REUSE_DETECTED.value for _, reason in links)


async def test_a_replay_after_sign_out_alarms_even_inside_the_window(
    contract_engine: AsyncEngine, account: UUID, caplog: pytest.LogCaptureFixture
) -> None:
    """The liveness guard, isolated.

    Rotate, sign out, then replay the *rotated* token a second later. The
    reason says `rotated` and the clock is well inside the grace window, so
    the first two conditions both say "race" — and it is not one, because
    the chain it belongs to is gone. Somebody presenting a link of a chain
    the user has already signed out of is exactly the case the alarm is for.
    """
    clock = _FixedClock(datetime.now(UTC))
    stolen, family = await _issue(contract_engine, account, clock)

    successor = await _rotate(contract_engine, stolen, clock)
    assert isinstance(successor, str)
    async with AsyncSession(contract_engine, expire_on_commit=False) as session:
        await _service(session, clock=clock).revoke_by_refresh_token(successor)

    clock.advance(1)
    with caplog.at_level("INFO"):
        replay = await _rotate(contract_engine, stolen, clock)

    assert isinstance(replay, RevokedSession)
    assert "refresh_token_reuse_detected" in [record.message for record in caplog.records]
    assert sum(live for live, _ in await _family(contract_engine, family)) == 0
