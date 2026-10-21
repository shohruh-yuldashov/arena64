"""`RedisExternalSignInStore`, against real Redis — A64-031.E1.

The properties that make the store safe with two API replicas: a `state`
is spent exactly once even when two callbacks race for it, every record
expires, and nothing the browser holds appears in the keyspace.

Skipped, not failed, when Redis is unreachable.
"""

import asyncio
import hashlib
from uuid import uuid4

from redis.asyncio import Redis

from app.modules.auth.domain.google_sign_in import PendingPurpose, PendingSignIn, StartedSignIn
from app.modules.auth.infrastructure.external_sign_in_store import RedisExternalSignInStore

STATE = "the-state-the-browser-holds"
DIGEST = hashlib.sha256(STATE.encode()).digest()


def _started() -> StartedSignIn:
    return StartedSignIn(
        binding_digest=hashlib.sha256(b"binding").digest(),
        code_verifier="v" * 43,
        nonce="nonce-value",
        next_path="/play",
    )


async def test_a_state_round_trips_and_is_spent_once(contract_redis: Redis) -> None:
    store = RedisExternalSignInStore(contract_redis)
    await store.save_started(DIGEST, _started(), ttl_seconds=600)

    first = await store.take_started(DIGEST)
    second = await store.take_started(DIGEST)

    assert first == _started()
    assert second is None


async def test_two_racing_callbacks_get_one_state(contract_redis: Redis) -> None:
    store = RedisExternalSignInStore(contract_redis)
    await store.save_started(DIGEST, _started(), ttl_seconds=600)

    taken = await asyncio.gather(*(store.take_started(DIGEST) for _ in range(10)))

    assert sum(record is not None for record in taken) == 1


async def test_every_record_expires(contract_redis: Redis) -> None:
    store = RedisExternalSignInStore(contract_redis)
    await store.save_started(DIGEST, _started(), ttl_seconds=600)
    pending = PendingSignIn(
        purpose=PendingPurpose.LINK_ACCOUNT, subject="sub", email="a@gmail.com", user_id=uuid4()
    )
    await store.save_pending(DIGEST, pending, ttl_seconds=900)

    ttls = [await contract_redis.ttl(key) for key in await contract_redis.keys("oidc:*")]

    assert len(ttls) == 2
    assert all(0 < ttl <= 900 for ttl in ttls)


async def test_the_keyspace_holds_digests_not_what_the_browser_holds(
    contract_redis: Redis,
) -> None:
    store = RedisExternalSignInStore(contract_redis)
    await store.save_started(DIGEST, _started(), ttl_seconds=600)

    keys = [key.decode() for key in await contract_redis.keys("*")]

    assert keys == [f"oidc:v1:state:{DIGEST.hex()}"]
    assert all(STATE not in key for key in keys)


async def test_a_pending_identity_survives_a_read_and_not_a_discard(
    contract_redis: Redis,
) -> None:
    store = RedisExternalSignInStore(contract_redis)
    pending = PendingSignIn(
        purpose=PendingPurpose.CREATE_ACCOUNT,
        subject="sub",
        email="new@gmail.com",
        next_path="/tournaments",
    )
    await store.save_pending(DIGEST, pending, ttl_seconds=900)

    assert await store.read_pending(DIGEST) == pending
    assert await store.read_pending(DIGEST) == pending
    await store.discard_pending(DIGEST)
    assert await store.read_pending(DIGEST) is None


async def test_a_record_another_build_wrote_is_no_record(contract_redis: Redis) -> None:
    await contract_redis.set(f"oidc:v1:state:{DIGEST.hex()}", b"{not json", ex=60)

    assert await RedisExternalSignInStore(contract_redis).take_started(DIGEST) is None
