"""Friend challenges, end to end — A64-022.1 §24, §25.

Against real PostgreSQL and through the composition root's own factory, so
what is exercised is the graph that ships: `build_challenge_service` names
`SqlAlchemyChallengeRepository`, `CachedSocialGraphReader`,
`PairingExclusionService` and `SqlAlchemyTimeControlCatalogue`, and the
service itself holds only ports.

That matters more here than usual. A manually assembled `ChallengeService`
would prove the aggregate works and nothing about whether anything can reach
it — and this phase has no HTTP surface, so the factory *is* the reachability
proof (§25).

## What is substituted, and what is not

The **social graph is real**: friendships and blocks are written through
`friends`' own repositories, so "these two are friends" is a row rather than
a stub's opinion. Only the cache decorator is given a no-op, because a
contract suite must not need Redis and a cache changes how an answer is
served rather than what it is.

## What is not tested here

Acceptance. There is no `accept` in this phase — `domain-model.md` §10.3
requires it to create a match in the same transaction, and A64-022.3 owns
both halves. A test asserting a status this build cannot produce would be
asserting a decision rather than a behaviour.

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.exceptions import ConflictError, NotFoundError
from app.modules.friends.infrastructure.cache import NoSocialGraphCache
from app.modules.game.public import ProductVariant
from app.modules.matchmaking.application.services.challenge_service import (
    ChallengeInvalidTimeControl,
    ChallengeNotFriends,
    ChallengeService,
)
from app.modules.matchmaking.domain.challenge import (
    CHALLENGE_TTL,
    ChallengeExpired,
    ChallengeForbidden,
    ChallengeNotPending,
    ChallengeSelfNotAllowed,
    ChallengeStatus,
)
from app.modules.matchmaking.infrastructure.models import FriendChallengeModel
from app.modules.matchmaking.infrastructure.repositories.challenge_repository import (
    SqlAlchemyChallengeRepository,
)
from app.modules.matchmaking.presentation.dependencies import build_challenge_service
from app.modules.reference.public import TimeControlId
from app.platform.outbox import OutboxEventPublisher, SqlAlchemyOutboxRepository

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
VARIANT = ProductVariant.RUSSIAN_8X8
CLOCK = TimeControlId.BLITZ_3_2


class MovableClock(Clock):
    """A clock a test advances, so a 24-hour window is a microsecond."""

    def __init__(self, at: datetime) -> None:
        self._at = at

    def now(self) -> datetime:
        return self._at

    def advance(self, by: timedelta) -> None:
        self._at += by


def service(session: AsyncSession, clock: Clock | None = None) -> ChallengeService:
    """**The composition root's factory**, not a hand-built graph — §25.

    `NoSocialGraphCache` rather than the Redis decorator: a contract suite
    must not need Redis, and the cache changes how a read is served rather
    than what it answers.
    """
    return build_challenge_service(
        session,
        clock=clock or MovableClock(NOW),
        cache=NoSocialGraphCache(),
        # The **real** publisher over the same session — A64-022.2 stages
        # every lifecycle event in the challenge's own transaction, so a
        # no-op here would let a service that never published pass.
        events=OutboxEventPublisher(SqlAlchemyOutboxRepository(session)),
    )


async def player(session: AsyncSession) -> UUID:
    """One account, written directly.

    Registration goes through `auth` and costs a password hash per player;
    this suite needs identities that the friendship rows can point at, and
    `friends` holds no foreign key to `users` (DM-06) — so the row exists for
    the readers that resolve a player, not for the challenge itself.
    """
    player_id = uuid4()
    suffix = player_id.hex[:10]
    await session.execute(
        text(
            "INSERT INTO users.user (id, username, email, password_hash, "
            "is_active, is_verified, created_at, updated_at) "
            "VALUES (:id, :username, :email, 'x', true, true, now(), now())"
        ),
        {"id": player_id, "username": f"ch{suffix}", "email": f"{suffix}@example.com"},
    )
    return player_id


async def befriend(session: AsyncSession, a: UUID, b: UUID) -> None:
    """A real friendship row, through `friends`' own storage shape.

    `friendship` stores the pair ordered, which is why the ids are sorted
    here — the same normalisation `uq_friendship__pair` enforces.
    """
    low, high = sorted((a, b), key=str)
    await session.execute(
        text(
            "INSERT INTO friends.friendship (id, player_low_id, player_high_id, created_at) "
            "VALUES (:id, :low, :high, now())"
        ),
        {"id": uuid4(), "low": low, "high": high},
    )


async def block(session: AsyncSession, blocker: UUID, blocked: UUID) -> None:
    await session.execute(
        text(
            "INSERT INTO friends.blocked_player (id, blocker_id, blocked_id, created_at) "
            "VALUES (:id, :blocker, :blocked, now())"
        ),
        {"id": uuid4(), "blocker": blocker, "blocked": blocked},
    )


async def friends_pair(session: AsyncSession) -> tuple[UUID, UUID]:
    first, second = await player(session), await player(session)
    await befriend(session, first, second)
    return first, second


async def stored(session: AsyncSession, challenge_id: UUID) -> FriendChallengeModel:
    row = await session.scalar(
        select(FriendChallengeModel).where(FriendChallengeModel.id == challenge_id)
    )
    assert row is not None
    return row


class TestCreating:
    async def test_a_friend_may_be_challenged(self, contract_session: AsyncSession) -> None:
        """The happy path, through the real factory and real friendship rows.

        Asserts the **stored row** rather than the returned aggregate: what
        A64-022.3 will read is the row, and a service that returned a correct
        object and wrote a wrong one would pass a weaker test.
        """
        challenger, recipient = await friends_pair(contract_session)

        challenge = await service(contract_session).create(
            challenger, recipient_id=recipient, time_control_id=CLOCK, variant=VARIANT
        )

        row = await stored(contract_session, challenge.id)
        assert row.status is ChallengeStatus.PENDING
        assert (row.challenger_id, row.recipient_id) == (challenger, recipient)
        assert row.time_control_id is CLOCK
        # The window is the platform's, from the injected clock — never a
        # database default and never the client's idea of now.
        assert row.expires_at == NOW + CHALLENGE_TTL
        assert row.responded_at is None
        # **Never set in this phase.** A64-022.3 writes it in the same
        # transaction that creates the match.
        assert row.created_match_id is None

    async def test_a_player_cannot_challenge_themselves(
        self, contract_session: AsyncSession
    ) -> None:
        """Refused in the aggregate, before any reader is touched — it is the
        one rule that needs nothing but the two ids."""
        alone = await player(contract_session)

        with pytest.raises(ChallengeSelfNotAllowed):
            await service(contract_session).create(
                alone, recipient_id=alone, time_control_id=CLOCK, variant=VARIANT
            )

    async def test_a_stranger_cannot_be_challenged(self, contract_session: AsyncSession) -> None:
        """Friendship is the server's answer, from `friends`' own reader.

        No friendship row, so no challenge — and a frontend that decided
        otherwise cannot make this succeed, which is the point of asking the
        graph here rather than trusting a request.
        """
        challenger, stranger = await player(contract_session), await player(contract_session)

        with pytest.raises(ChallengeNotFriends):
            await service(contract_session).create(
                challenger, recipient_id=stranger, time_control_id=CLOCK, variant=VARIANT
            )

    async def test_a_block_is_refused_indistinguishably_from_a_stranger(
        self, contract_session: AsyncSession
    ) -> None:
        """**BL-2, FR-2 and `domain-model.md` §10.3.**

        A blocked player must not learn they were blocked, so the refusal is
        the same *type* and the same *sentence* as "you are not friends" —
        and this asserts both, because an error message that differed would
        be the disclosure however the code was named.

        The friendship row is left in place so that only the block can be
        what refuses this. Without it the test would pass for the wrong
        reason.
        """
        challenger, recipient = await friends_pair(contract_session)
        await block(contract_session, recipient, challenger)

        with pytest.raises(ChallengeNotFriends) as blocked:
            await service(contract_session).create(
                challenger, recipient_id=recipient, time_control_id=CLOCK, variant=VARIANT
            )

        other, stranger = await player(contract_session), await player(contract_session)
        with pytest.raises(ChallengeNotFriends) as not_friends:
            await service(contract_session).create(
                other, recipient_id=stranger, time_control_id=CLOCK, variant=VARIANT
            )
        assert str(blocked.value) == str(not_friends.value)

    async def test_a_time_control_the_platform_does_not_offer_is_refused(
        self, contract_session: AsyncSession
    ) -> None:
        """Validated against the **active catalogue**, not the enum.

        A member of `TimeControlId` may be retired, and an enum check would
        keep offering it forever. Retiring one here proves the catalogue is
        what answers — and that rows already referencing it stay readable,
        which is why `is_active` exists rather than a delete.
        """
        challenger, recipient = await friends_pair(contract_session)
        await contract_session.execute(
            text("UPDATE reference.time_control SET is_active = false WHERE id = :id"),
            {"id": CLOCK.value},
        )

        with pytest.raises(ChallengeInvalidTimeControl):
            await service(contract_session).create(
                challenger, recipient_id=recipient, time_control_id=CLOCK, variant=VARIANT
            )

    async def test_one_live_challenge_per_pair_whichever_direction(
        self, contract_session: AsyncSession
    ) -> None:
        """**§6, policy A, and the reason it is an index rather than a check.**

        The second attempt is the *opposite direction* — the case a plain
        unique on `(challenger_id, recipient_id)` would permit and a
        service-level check would lose the race on. It is refused by
        `uq_friend_challenge__live_pair`, which is keyed on the unordered
        pair.
        """
        first, second = await friends_pair(contract_session)
        challenges = service(contract_session)
        await challenges.create(first, recipient_id=second, time_control_id=CLOCK, variant=VARIANT)

        with pytest.raises(ConflictError):
            await challenges.create(
                second, recipient_id=first, time_control_id=CLOCK, variant=VARIANT
            )


class TestAnswering:
    async def test_only_the_recipient_may_decline(self, contract_session: AsyncSession) -> None:
        """Two assertions, and the second is the one worth having: a
        challenger who could decline their own challenge would be cancelling
        it under a name that reads differently in a history."""
        challenger, recipient = await friends_pair(contract_session)
        challenges = service(contract_session)
        challenge = await challenges.create(
            challenger, recipient_id=recipient, time_control_id=CLOCK, variant=VARIANT
        )

        with pytest.raises(ChallengeForbidden):
            await challenges.decline(challenge.id, by=challenger)

        settled = await challenges.decline(challenge.id, by=recipient)
        assert settled.status is ChallengeStatus.DECLINED
        row = await stored(contract_session, challenge.id)
        assert row.responded_at is not None

    async def test_only_the_challenger_may_cancel(self, contract_session: AsyncSession) -> None:
        challenger, recipient = await friends_pair(contract_session)
        challenges = service(contract_session)
        challenge = await challenges.create(
            challenger, recipient_id=recipient, time_control_id=CLOCK, variant=VARIANT
        )

        with pytest.raises(ChallengeForbidden):
            await challenges.cancel(challenge.id, by=recipient)

        settled = await challenges.cancel(challenge.id, by=challenger)
        assert settled.status is ChallengeStatus.CANCELLED

    async def test_a_stranger_gets_not_found_rather_than_forbidden(
        self, contract_session: AsyncSession
    ) -> None:
        """**§21's IDOR rule.**

        A challenge somebody is not part of is *not found*, so an identifier
        cannot be probed for existence. `ChallengeForbidden` is reserved for
        the two people who genuinely are parties and picked the wrong verb.
        """
        challenger, recipient = await friends_pair(contract_session)
        outsider = await player(contract_session)
        challenges = service(contract_session)
        challenge = await challenges.create(
            challenger, recipient_id=recipient, time_control_id=CLOCK, variant=VARIANT
        )

        with pytest.raises(NotFoundError):
            await challenges.decline(challenge.id, by=outsider)

    async def test_an_expired_challenge_cannot_be_declined_but_can_be_cancelled(
        self, contract_session: AsyncSession
    ) -> None:
        """Expiry is server-authoritative and read at answer time.

        The asymmetry is deliberate: answering an expired invitation is
        meaningless, but *cancelling* one is somebody tidying a list, and
        refusing that would leave a row they cannot clear until a sweep they
        cannot see runs (A64-022.6).
        """
        challenger, recipient = await friends_pair(contract_session)
        clock = MovableClock(NOW)
        challenges = service(contract_session, clock)
        challenge = await challenges.create(
            challenger, recipient_id=recipient, time_control_id=CLOCK, variant=VARIANT
        )

        clock.advance(CHALLENGE_TTL + timedelta(seconds=1))

        with pytest.raises(ChallengeExpired):
            await challenges.decline(challenge.id, by=recipient)

        settled = await challenges.cancel(challenge.id, by=challenger)
        assert settled.status is ChallengeStatus.CANCELLED

    async def test_a_second_answer_is_refused_and_the_first_stands(
        self, contract_session: AsyncSession
    ) -> None:
        """§19's decline-versus-cancel, **sequentially**.

        The service re-reads inside its unit of work, so the second actor's
        aggregate is already `DECLINED` and the *domain* refuses it. That is
        the layer that should answer when there is no race: the caller is
        told the challenge was answered, which is what happened.
        """
        challenger, recipient = await friends_pair(contract_session)
        challenges = service(contract_session)
        challenge = await challenges.create(
            challenger, recipient_id=recipient, time_control_id=CLOCK, variant=VARIANT
        )

        await challenges.decline(challenge.id, by=recipient)

        with pytest.raises(ChallengeNotPending):
            await challenges.cancel(challenge.id, by=challenger)

        row = await stored(contract_session, challenge.id)
        assert row.status is ChallengeStatus.DECLINED

    async def test_a_stale_aggregate_cannot_overwrite_a_settled_challenge(
        self, contract_session: AsyncSession
    ) -> None:
        """**§19's actual race, at the layer that has to survive it.**

        The test above is the sequential case, where the re-read catches it.
        A genuine race has no re-read to catch it: two workers each read
        `PENDING`, each produce a valid terminal aggregate, and both try to
        write. Neither is wrong, so the aggregate cannot arbitrate — the
        database has to.

        Reproduced by holding the stale aggregate rather than by threading,
        because two connections inside one rolled-back transaction cannot see
        each other: a threaded version here would test the fixture. What is
        asserted is the guard `save` puts on `status = 'pending'` — the
        second `UPDATE` matches no row, and its caller is told rather than
        silently overwriting the first outcome.
        """
        challenger, recipient = await friends_pair(contract_session)
        challenges = service(contract_session)
        pending = await challenges.create(
            challenger, recipient_id=recipient, time_control_id=CLOCK, variant=VARIANT
        )
        await challenges.decline(pending.id, by=recipient)

        # The other actor's view, from before the decline landed.
        stale = pending.cancel(by=challenger, at=NOW)
        repository = SqlAlchemyChallengeRepository(contract_session)

        with pytest.raises(ConflictError):
            await repository.save(stale)

        row = await stored(contract_session, pending.id)
        assert row.status is ChallengeStatus.DECLINED

    async def test_a_settled_pair_may_challenge_again(self, contract_session: AsyncSession) -> None:
        """The live-pair rule is about the **live** state.

        A plain unique would have meant two friends could challenge each
        other once ever, which is why the index is partial — and this is the
        assertion that would fail if the `WHERE` were dropped.
        """
        first, second = await friends_pair(contract_session)
        challenges = service(contract_session)
        first_challenge = await challenges.create(
            first, recipient_id=second, time_control_id=CLOCK, variant=VARIANT
        )
        await challenges.decline(first_challenge.id, by=second)

        again = await challenges.create(
            second, recipient_id=first, time_control_id=CLOCK, variant=VARIANT
        )

        assert again.status is ChallengeStatus.PENDING
