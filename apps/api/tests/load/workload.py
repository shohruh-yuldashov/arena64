"""Test accounts, matches and the shapes a scenario drives — §48, §51, §56.

Everything here creates its **own** data and can be run again without
cleaning up first: a benchmark that needs a hand-prepared database is a
benchmark nobody reruns, and one that needs production data is not allowed
to exist (§51).

Passwords are a constant in this file on purpose. It is a load harness that
only ever talks to a local instance, and the alternative — a generated
secret written somewhere — is a secret in a file. The accounts it makes are
named so an operator can see what they are and delete them.
"""

import asyncio
import secrets
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

#: Every account this harness creates. Visible, greppable and deletable.
PREFIX = "loadtest"
PASSWORD = "LoadTest1!aaaa"


@dataclass(frozen=True, slots=True)
class Player:
    """One signed-in test account, ready to be driven."""

    user_id: str
    username: str
    email: str
    access_token: str

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}


async def _with_backoff(
    call: Callable[[], Awaitable[httpx.Response]], attempts: int = 6
) -> httpx.Response:
    """Setup only, and the distinction matters — A64-028.5 §28.

    The rate limiter is **not** disabled for these measurements. But a
    harness on one machine is one IP, and creating a few hundred accounts to
    *prepare* a scenario trips the login limit before the scenario has
    begun. Waiting is the honest response: the limiter is doing its job, and
    the fixture is not the thing being measured.

    Never used inside a measured operation. A scenario that retried its own
    `429`s would report the limiter's patience as the platform's latency.
    """
    delay = 0.5
    response = await call()
    for _ in range(attempts - 1):
        if response.status_code != 429:
            break
        await asyncio.sleep(delay)
        delay *= 2
        response = await call()
    response.raise_for_status()
    return response


async def sign_up(client: httpx.AsyncClient, tag: str) -> Player:
    """Registers and signs in one account. Idempotent by construction — the
    tag carries entropy, so a rerun never collides with the last one."""
    username = f"{PREFIX}{tag}"
    email = f"{username}@example.test"

    created = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "username": username, "password": PASSWORD},
    )
    created.raise_for_status()
    user_id = created.json()["data"]["id"]

    signed_in = await _with_backoff(
        lambda: client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    )
    return Player(
        user_id=user_id,
        username=username,
        email=email,
        access_token=signed_in.json()["data"]["access_token"],
    )


async def cohort(base_url: str, size: int, *, prefix: str = "") -> list[Player]:
    """`size` fresh accounts, created concurrently.

    Concurrently because creating 200 accounts one at a time is a minute of
    Argon2 the measurement does not need — and because it is itself a
    reasonable first look at whether registration serialises.
    """
    async with httpx.AsyncClient(base_url=base_url, timeout=60.0) as client:
        run = secrets.token_hex(3)
        import asyncio

        return list(
            await asyncio.gather(
                *(sign_up(client, f"{prefix}{run}{index}") for index in range(size))
            )
        )


async def ws_ticket(client: httpx.AsyncClient, player: Player) -> str:
    """A one-time handshake ticket. Short-lived, so it is taken immediately
    before the socket it is for."""
    response = await client.post("/api/v1/auth/ws-ticket", headers=player.auth)
    response.raise_for_status()
    ticket: str = response.json()["data"]["ticket"]
    return ticket


def frame(kind: str, channel: str, payload: dict[str, Any], request_id: str | None = None) -> str:
    """One protocol frame, as `GatewayMessage.to_json` writes it."""
    import json

    envelope: dict[str, Any] = {"v": 1, "type": kind, "channel": channel, "payload": payload}
    if request_id is not None:
        envelope["request_id"] = request_id
    return json.dumps(envelope, separators=(",", ":"))


#: The opening moves of a Russian draughts game, in order, alternating
#: sides — §13's "deterministic legal move generator".
#:
#: A random path benchmarks *rejection*, which is a different and much
#: cheaper code path than a move that validates, applies, commits, writes
#: the outbox and fans out. These are legal from the standard opening and
#: are what a real game's first plies look like.
OPENING: tuple[tuple[str, str], ...] = (
    ("c3", "d4"),
    ("f6", "e5"),
    ("b2", "c3"),
    ("g7", "f6"),
    ("a1", "b2"),
    ("h8", "g7"),
)


async def seeded_cohort(size: int, *, prefix: str = "") -> list[Player]:
    """`size` accounts, created in PostgreSQL and given real access tokens.

    ## Why not the register and login endpoints — A64-028.5 §28, §51

    Because the rate limiter refuses, and it is right to. Even the
    `development` profile allows **10 registrations per IP per hour** and
    **20 logins per IP per 15 minutes**, and a harness on one machine is one
    IP. A cohort of 200 through those endpoints is not slow — it is
    impossible, and making it possible would mean turning the limiter off,
    which §28 forbids and which would make every number afterwards a
    measurement of a platform nobody runs.

    So the accounts are inserted and the tokens are minted with the
    application's **own** `JwtTokenProvider` and its configured signing key.

    **Nothing under test is weakened by this.** These are real tokens: the
    API verifies the signature, the expiry, the type and the subject on
    every request exactly as it does for a token from `/auth/login`, and
    every rate limit on every measured endpoint still applies. What is
    skipped is the account *factory*, which is fixture setup and is not
    among the things being measured. Login throughput itself is measured
    separately, at the scale the limiter permits.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.config.settings import get_settings
    from app.core.clock import SystemClock
    from app.modules.auth.domain.tokens import TokenType
    from app.modules.auth.infrastructure.jwt_token_provider import JwtTokenProvider

    settings = get_settings()
    run = secrets.token_hex(3)
    engine = create_async_engine(settings.postgres.dsn.get_secret_value())
    provider = JwtTokenProvider(settings.jwt, SystemClock())

    players: list[Player] = []
    try:
        async with engine.begin() as connection:
            for index in range(size):
                username = f"{PREFIX}{prefix}{run}{index}"
                user_id = str(uuid.uuid4())
                await connection.execute(
                    text(
                        "INSERT INTO users.user (id, email, username, password_hash, is_verified) "
                        "VALUES (:i, :e, :u, 'x', true)"
                    ),
                    {"i": user_id, "e": f"{username}@example.test", "u": username},
                )
                token, _ = provider.issue(
                    subject=user_id,
                    token_type=TokenType.ACCESS,
                    lifetime_seconds=3600,
                )
                players.append(
                    Player(
                        user_id=user_id,
                        username=username,
                        email=f"{username}@example.test",
                        access_token=token,
                    )
                )
    finally:
        await engine.dispose()
    return players
