"""`RedisExternalSignInStore` — where a Google sign-in waits, A64-031.E1.

## The keyspace

    oidc:v1:state:<hex digest>     ->  StartedSignIn as JSON, TTL 10 min
    oidc:v1:pending:<hex digest>   ->  PendingSignIn as JSON, TTL 15 min

Registered in `caching.md` §3 beside `wsticket:`, on the same terms:
versioned (C-2), expiring (C-3), written with its expiry in one `SET ... EX`
(C-4), and keyed by the SHA-256 of what the browser holds — so a read of
Redis yields digests, and a digest cannot be presented to the callback.

## `GETDEL` for state, `GET` for pending

A `state` is spent by its callback, atomically, for the reason
`RedisWebSocketTicketStore` spends a ticket that way: two callbacks with
one `state` on two replicas resolve so that exactly one proceeds. A
pending identity is read without spending, because a taken username or a
mistyped password must not send the person back to Google — and the step
that finally uses it is guarded by the database's unique constraints.

## Which role, and failure

`cache`, like WebSocket tickets: losing a record costs the person one more
click on "Continue with Google", so it is expendable. A Redis failure
**propagates**: a sign-in whose state could not be stored cannot finish,
and treating an unreachable store as "no such state" is the safe reading
anyway — it fails closed.
"""

import json
import logging
from typing import Final
from uuid import UUID

from redis.asyncio import Redis

from app.modules.auth.domain.google_sign_in import PendingPurpose, PendingSignIn, StartedSignIn

logger = logging.getLogger(__name__)

KEY_VERSION: Final = "v1"
_STATE_PREFIX: Final = f"oidc:{KEY_VERSION}:state:"
_PENDING_PREFIX: Final = f"oidc:{KEY_VERSION}:pending:"


class RedisExternalSignInStore:
    """`ExternalSignInStore` over the `cache` role."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def save_started(
        self, digest: bytes, started: StartedSignIn, *, ttl_seconds: int
    ) -> None:
        value = {
            "binding": started.binding_digest.hex(),
            "verifier": started.code_verifier,
            "nonce": started.nonce,
            "next": started.next_path,
        }
        await self._redis.set(_STATE_PREFIX + digest.hex(), json.dumps(value), ex=ttl_seconds)

    async def take_started(self, digest: bytes) -> StartedSignIn | None:
        raw = await self._redis.getdel(_STATE_PREFIX + digest.hex())
        if raw is None:
            return None
        try:
            value = json.loads(raw)
            return StartedSignIn(
                binding_digest=bytes.fromhex(value["binding"]),
                code_verifier=value["verifier"],
                nonce=value["nonce"],
                next_path=value["next"],
            )
        except (ValueError, KeyError, TypeError):
            # A record this build cannot read was written by another build
            # mid-deploy. One failed sign-in, not an exception — and no part
            # of the value is logged.
            logger.warning("external_sign_in_state_malformed")
            return None

    async def save_pending(
        self, digest: bytes, pending: PendingSignIn, *, ttl_seconds: int
    ) -> None:
        value = {
            "purpose": pending.purpose.value,
            "subject": pending.subject,
            "email": pending.email,
            "user_id": None if pending.user_id is None else str(pending.user_id),
            "next": pending.next_path,
        }
        await self._redis.set(_PENDING_PREFIX + digest.hex(), json.dumps(value), ex=ttl_seconds)

    async def read_pending(self, digest: bytes) -> PendingSignIn | None:
        raw = await self._redis.get(_PENDING_PREFIX + digest.hex())
        if raw is None:
            return None
        try:
            value = json.loads(raw)
            user_id = value["user_id"]
            return PendingSignIn(
                purpose=PendingPurpose(value["purpose"]),
                subject=value["subject"],
                email=value["email"],
                user_id=None if user_id is None else UUID(user_id),
                next_path=value["next"],
            )
        except (ValueError, KeyError, TypeError):
            logger.warning("external_sign_in_pending_malformed")
            return None

    async def discard_pending(self, digest: bytes) -> None:
        await self._redis.delete(_PENDING_PREFIX + digest.hex())


__all__ = ["KEY_VERSION", "RedisExternalSignInStore"]
