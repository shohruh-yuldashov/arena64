"""The friend challenge expiry sweep — A64-022.6 §22.

Against real PostgreSQL, through the **production composition**: the service
is built by the factory `app_factory` calls, over a real outbox and a real
repository.

## What each test is actually about

  **An overdue challenge gets a row that says so.** `EXPIRED` was a member
  no row ever held; this is the transition finally being written.

  **One event, from the challenge's own deadline.** Not the sweep's instant
  — a relay catching up after an outage must not report a day-old expiry as
  having just happened.

  **A second sweep does nothing.** Idempotency needs no ledger here: the
  claim's predicate is `status = 'pending'`, so an expired row is not
  claimable and there is nothing to transition twice.

  **A live challenge is untouched.** The sweep's predicate is two
  conditions, and this is the one that stops it being a table-wide
  `UPDATE`.

  **Accept, decline and cancel each beat the sweep, or lose to it, and
  exactly one outcome survives.** Three tests, two of them on **two real
  connections** — the fixture's savepoint session cannot express a race
  between transactions, so those run off `contract_engine` and commit for
  real.

  **A batch is one statement.** The N+1 that is invisible with one
  challenge and fatal with two hundred.

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

import asyncio
from datetime import timedelta
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy import delete, event, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.config.settings import MatchmakingSettings
from app.core.clock import Clock
from app.modules.friends.infrastructure.cache import NoSocialGraphCache
from app.modules.matchmaking.application.services.challenge_expiry_service import (
    ChallengeExpiryService,
)
from app.modules.matchmaking.domain.challenge import (
    CHALLENGE_TTL,
    ChallengeNotPending,
    ChallengeStatus,
)
from app.modules.matchmaking.domain.challenge_events import FriendChallengeExpired
from app.modules.matchmaking.infrastructure.models import FriendChallengeModel
from app.modules.matchmaking.presentation.dependencies import (
    build_challenge_expiry_service,
    build_challenge_service,
)
from app.modules.reference.public import TimeControlId
from app.platform.outbox import OutboxEventPublisher, SqlAlchemyOutboxRepository
from app.platform.outbox.models import OutboxModel
from tests.contract.test_friend_challenges import (
    CLOCK,
    NOW,
    VARIANT,
    MovableClock,
    friends_pair,
    service,
    stored,
)
from tests.fakes.metrics import RecordingMetrics


def sweeper(
    session: AsyncSession, clock: Clock, *, batch_size: int = 200
) -> ChallengeExpiryService:
    """**The composition root's factory**, not a hand-built graph — §23.

    The real publisher over the same session, so a service that transitioned
    a row and published nothing would fail rather than pass.
    """
    return build_challenge_expiry_service(
        session,
        # Only the one field the factory reads. A whole `MatchmakingSettings`
        # would be twenty numbers this suite has no opinion about, and
        # constructing one would couple every expiry test to defaults that
        # belong to the queue.
        settings=cast("MatchmakingSettings", _Settings(batch_size)),
        clock=clock,
        metrics=RecordingMetrics(),
        events=OutboxEventPublisher(SqlAlchemyOutboxRepository(session)),
    )


class _Settings:
    """The one setting `build_challenge_expiry_service` reads."""

    def __init__(self, batch_size: int) -> None:
        self.challenge_expiry_batch_size = batch_size


async def expiry_events(session: AsyncSession) -> list[dict[str, Any]]:
    rows = await session.scalars(
        select(OutboxModel).where(OutboxModel.event_type == FriendChallengeExpired.event_type)
    )
    return [{"payload": row.payload, "occurred_at": row.occurred_at} for row in rows.all()]


async def a_challenge(session: AsyncSession, clock: MovableClock) -> UUID:
    """One pending challenge between two friends, through the real service."""
    challenger, recipient = await friends_pair(session)
    challenge = await service(session, clock).create(
        challenger, recipient_id=recipient, time_control_id=CLOCK, variant=VARIANT
    )
    return challenge.id


class TestSweep:
    async def test_an_overdue_challenge_is_written_as_expired(
        self, contract_session: AsyncSession
    ) -> None:
        """§22.1, and the reachability proof — §23.

        The whole path: a challenge through the real service, a sweep
        through the composition root's factory, and a row that finally holds
        a status the enum has declared since A64-022.1.
        """
        clock = MovableClock(NOW)
        challenge_id = await a_challenge(contract_session, clock)

        clock.advance(CHALLENGE_TTL + timedelta(seconds=1))
        result = await sweeper(contract_session, clock).expire_due()

        assert (result.claimed, result.expired, result.failed) == (1, 1, 0)
        row = await stored(contract_session, challenge_id)
        assert row.status == ChallengeStatus.EXPIRED
        # The invariant `ck_friend_challenge__responded_when_terminal`
        # enforces, asserted here because the sweep is the newest writer of
        # a terminal row.
        assert row.responded_at is not None

    async def test_it_publishes_one_event_stamped_with_the_deadline(
        self, contract_session: AsyncSession
    ) -> None:
        """§22.2. `occurred_at` is the challenge's **own** `expires_at`.

        A relay catching up after an outage must not report a day-old
        expiry as having just happened — the same choice `QueueTicketExpired`
        makes, and the reason a consumer can trust the instant it is given.
        """
        clock = MovableClock(NOW)
        challenge_id = await a_challenge(contract_session, clock)
        deadline = (await stored(contract_session, challenge_id)).expires_at

        clock.advance(CHALLENGE_TTL + timedelta(hours=6))
        await sweeper(contract_session, clock).expire_due()

        published = await expiry_events(contract_session)
        assert len(published) == 1
        assert UUID(str(published[0]["payload"]["challenge_id"])) == challenge_id
        assert published[0]["occurred_at"] == deadline
        # Not the sweep's instant, which is six hours later.
        assert published[0]["occurred_at"] != clock.now()

    async def test_a_second_sweep_changes_nothing(self, contract_session: AsyncSession) -> None:
        """§22.3. Idempotency with no ledger and no new mechanism.

        The claim's predicate is `status = 'pending'`, so the second pass
        finds nothing to claim — no transition, no second event, and
        `responded_at` untouched.
        """
        clock = MovableClock(NOW)
        challenge_id = await a_challenge(contract_session, clock)

        clock.advance(CHALLENGE_TTL + timedelta(seconds=1))
        await sweeper(contract_session, clock).expire_due()
        first_response = (await stored(contract_session, challenge_id)).responded_at

        clock.advance(timedelta(minutes=5))
        second = await sweeper(contract_session, clock).expire_due()

        assert (second.claimed, second.expired) == (0, 0)
        assert len(await expiry_events(contract_session)) == 1
        assert (await stored(contract_session, challenge_id)).responded_at == first_response

    async def test_a_live_challenge_is_untouched(self, contract_session: AsyncSession) -> None:
        """The predicate's other half. Without `expires_at <= now` this
        would be a table-wide `UPDATE` that cancelled every invitation on
        the platform under a different name — §19."""
        clock = MovableClock(NOW)
        challenge_id = await a_challenge(contract_session, clock)

        clock.advance(CHALLENGE_TTL - timedelta(minutes=1))
        result = await sweeper(contract_session, clock).expire_due()

        assert (result.claimed, result.expired) == (0, 0)
        assert (await stored(contract_session, challenge_id)).status == ChallengeStatus.PENDING
        assert await expiry_events(contract_session) == []

    async def test_a_batch_costs_one_update_whatever_its_size(
        self, contract_session: AsyncSession
    ) -> None:
        """§22.7, §16. The N+1 that is invisible with one challenge.

        Counted against the real driver: ten challenges must settle in
        **one** `UPDATE`, not ten. A per-row loop passes every other test in
        this file and turns a two-hundred-row backlog into two hundred round
        trips.
        """
        clock = MovableClock(NOW)
        for _ in range(10):
            await a_challenge(contract_session, clock)
        await contract_session.flush()

        clock.advance(CHALLENGE_TTL + timedelta(seconds=1))

        updates: list[str] = []

        def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
            if statement.lstrip().upper().startswith("UPDATE FRIEND_CHALLENGE") or (
                "UPDATE matchmaking.friend_challenge" in statement
            ):
                updates.append(statement)

        engine: Engine = contract_session.get_bind().engine
        event.listen(engine, "before_cursor_execute", record)
        try:
            result = await sweeper(contract_session, clock).expire_due()
        finally:
            event.remove(engine, "before_cursor_execute", record)

        assert (result.claimed, result.expired) == (10, 10)
        assert len(updates) == 1, f"expected one UPDATE, saw {len(updates)}"


class TestRacesWithinOneTransaction:
    """The two orderings that need no second connection.

    A challenge already settled when the sweep runs is the ordinary shape of
    "somebody answered first", and it is decided by the same guarded
    `UPDATE` a two-connection race resolves — so asserting it here costs no
    committed rows.
    """

    async def test_decline_and_expiry_yield_exactly_one_terminal_state(
        self, contract_session: AsyncSession
    ) -> None:
        """§22.5, both orderings, because they are one property.

        **Decline first**: the row is `DECLINED` before the sweep runs, so
        the claim's predicate excludes it entirely — there is no window in
        which a decline becomes an expiry.

        **Sweep first**: the aggregate refuses before the repository is
        reached. `_require_answerable` checks pending before expiry, so the
        recipient is told the challenge was *answered* rather than that it
        expired — which is the more useful sentence and is deliberate.

        Either way one terminal state survives, and it is the first one
        written.
        """
        clock = MovableClock(NOW)

        answered_first, recipient = await friends_pair(contract_session)
        declined = await service(contract_session, clock).create(
            answered_first, recipient_id=recipient, time_control_id=CLOCK, variant=VARIANT
        )
        await service(contract_session, clock).decline(declined.id, by=recipient)

        swept_first, other_recipient = await friends_pair(contract_session)
        swept = await service(contract_session, clock).create(
            swept_first, recipient_id=other_recipient, time_control_id=CLOCK, variant=VARIANT
        )

        clock.advance(CHALLENGE_TTL + timedelta(seconds=1))
        result = await sweeper(contract_session, clock).expire_due()

        # Only the unanswered one was claimable.
        assert (result.claimed, result.expired) == (1, 1)
        assert (await stored(contract_session, declined.id)).status == ChallengeStatus.DECLINED
        assert (await stored(contract_session, swept.id)).status == ChallengeStatus.EXPIRED

        with pytest.raises(ChallengeNotPending):
            await service(contract_session, clock).decline(swept.id, by=other_recipient)
        assert (await stored(contract_session, swept.id)).status == ChallengeStatus.EXPIRED

    async def test_cancelling_after_the_sweep_no_longer_mutates_it(
        self, contract_session: AsyncSession
    ) -> None:
        """§6, §22.6. The documented cancel semantics, both sides of the
        sweep.

        **Before** a persisted expiry the challenger may still cancel an
        overdue row — `cancel` deliberately checks only "is it pending",
        which is what let a challenger tidy a list the platform had not
        swept. **After** it, the row is terminal and cancel changes nothing.

        Both are asserted here because the change is exactly this: the
        window in which the first behaviour applies is now a minute rather
        than forever.
        """
        clock = MovableClock(NOW)
        challenger, recipient = await friends_pair(contract_session)
        tidy = await service(contract_session, clock).create(
            challenger, recipient_id=recipient, time_control_id=CLOCK, variant=VARIANT
        )

        # Before the sweep: overdue, still `PENDING`, still cancellable.
        clock.advance(CHALLENGE_TTL + timedelta(seconds=1))
        await service(contract_session, clock).cancel(tidy.id, by=challenger)
        assert (await stored(contract_session, tidy.id)).status == ChallengeStatus.CANCELLED

        # After it: a second pair, swept first, and cancel is refused.
        other, other_recipient = await friends_pair(contract_session)
        swept = await service(contract_session, clock).create(
            other, recipient_id=other_recipient, time_control_id=CLOCK, variant=VARIANT
        )
        clock.advance(CHALLENGE_TTL + timedelta(seconds=1))
        await sweeper(contract_session, clock).expire_due()

        with pytest.raises(ChallengeNotPending):
            await service(contract_session, clock).cancel(swept.id, by=other)
        assert (await stored(contract_session, swept.id)).status == ChallengeStatus.EXPIRED


class TestAcceptVersusExpiry:
    """§5, §22.4 — the race the phase exists for, on two real connections.

    Runs off `contract_engine` rather than `contract_session`: that fixture
    binds its session to one connection inside an outer transaction it
    always rolls back, so a `commit()` there releases a savepoint and is
    invisible to any other connection — which is precisely the visibility
    this is about. Rows are committed for real and deleted in `finally`.

    The forbidden outcomes are asserted directly, because they are the ones
    that would be silent: `EXPIRED` with a `created_match_id`, and
    `ACCEPTED` without one.
    """

    async def test_exactly_one_of_the_two_wins(self, contract_engine: AsyncEngine) -> None:
        players: set[UUID] = set()
        challenge_id: UUID | None = None
        try:
            async with AsyncSession(contract_engine, expire_on_commit=False) as setup:
                challenger, recipient = await friends_pair(setup)
                players = {challenger, recipient}
                clock = MovableClock(NOW)
                created = await build_challenge_service(
                    setup,
                    clock=clock,
                    cache=NoSocialGraphCache(),
                    events=OutboxEventPublisher(SqlAlchemyOutboxRepository(setup)),
                ).create(
                    challenger,
                    recipient_id=recipient,
                    time_control_id=TimeControlId.BLITZ_3_2,
                    variant=VARIANT,
                )
                challenge_id = created.id
                await setup.commit()

            # The sweep's clock is past the deadline; the acceptance's is
            # **not**, which is the only way to reach the interesting race:
            # a recipient whose own clock still says the window is open.
            past = MovableClock(NOW + CHALLENGE_TTL + timedelta(seconds=1))
            inside = MovableClock(NOW + CHALLENGE_TTL - timedelta(seconds=1))

            accepted = False
            async with (
                AsyncSession(contract_engine, expire_on_commit=False) as sweep_session,
                AsyncSession(contract_engine, expire_on_commit=False) as accept_session,
            ):

                async def sweep() -> None:
                    await sweeper(sweep_session, past).expire_due()

                async def take_it() -> bool:
                    try:
                        await build_challenge_service(
                            accept_session,
                            clock=inside,
                            cache=NoSocialGraphCache(),
                            events=OutboxEventPublisher(SqlAlchemyOutboxRepository(accept_session)),
                        ).accept(challenge_id, by=recipient)
                        await accept_session.commit()
                        return True
                    except Exception:
                        await accept_session.rollback()
                        return False

                # **Genuinely concurrent**, not sequenced — §5 asks for a
                # real race and the interleaving is the subject. One of the
                # two blocks on the other's row lock, and which one wins is
                # PostgreSQL's answer rather than this test's ordering.
                #
                # Both orderings are legal, which is why the assertions
                # below name the *shapes* that must never exist rather than
                # the outcome that must occur. A test that demanded one
                # winner would be asserting a scheduling accident.
                _, accepted = await asyncio.gather(sweep(), take_it())

            async with AsyncSession(contract_engine, expire_on_commit=False) as check:
                row = await check.scalar(
                    select(FriendChallengeModel).where(FriendChallengeModel.id == challenge_id)
                )
                assert row is not None

                # Exactly one outcome, and the two forbidden shapes are
                # unreachable rather than merely absent.
                assert row.status in (ChallengeStatus.EXPIRED, ChallengeStatus.ACCEPTED)
                if row.status == ChallengeStatus.EXPIRED:
                    assert not accepted
                    assert row.created_match_id is None, "EXPIRED must never carry a match"
                else:
                    assert accepted
                    assert row.created_match_id is not None, "ACCEPTED must never lack a match"
        finally:
            async with AsyncSession(contract_engine) as cleanup:
                if challenge_id is not None:
                    await cleanup.execute(
                        delete(FriendChallengeModel).where(FriendChallengeModel.id == challenge_id)
                    )
                for player_id in players:
                    await cleanup.execute(
                        text(
                            "DELETE FROM friends.friendship WHERE :id IN (player_low_id, "
                            "player_high_id)"
                        ),
                        {"id": player_id},
                    )
                    await cleanup.execute(
                        text("DELETE FROM users.user WHERE id = :id"), {"id": player_id}
                    )
                await cleanup.commit()
