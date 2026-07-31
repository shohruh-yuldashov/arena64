"""`Argon2idPasswordHasher` — the real thing, not a stub.

Deliberately exercises actual Argon2id rather than mocking it, because
every property worth asserting here (salting, the encoded parameters, not
blocking the loop) is a property of the real implementation. The cost is
a handful of ~20ms hashes, which is acceptable for a suite this small and
is why the *service* tests use a stub instead.
"""

import asyncio
import time

import pytest
from argon2 import PasswordHasher as Argon2PasswordHasher

from app.config.settings import AuthSettings
from app.modules.auth.infrastructure import Argon2idPasswordHasher

PASSWORD = "CorrectHorse1!"


@pytest.fixture
def hasher() -> Argon2idPasswordHasher:
    return Argon2idPasswordHasher(AuthSettings())


class TestHash:
    async def test_produces_an_argon2id_encoding(self, hasher: Argon2idPasswordHasher) -> None:
        encoded = await hasher.hash(PASSWORD)
        # Not argon2i, not argon2d — the variant matters for password
        # storage, and the encoding is where it is recorded.
        assert encoded.startswith("$argon2id$")

    async def test_never_contains_the_plaintext(self, hasher: Argon2idPasswordHasher) -> None:
        encoded = await hasher.hash(PASSWORD)
        assert PASSWORD not in encoded

    async def test_is_salted_so_identical_passwords_differ(
        self, hasher: Argon2idPasswordHasher
    ) -> None:
        # Without a per-hash salt, a stolen database could be attacked with
        # one precomputed table for every user at once.
        assert await hasher.hash(PASSWORD) != await hasher.hash(PASSWORD)

    async def test_embeds_the_configured_parameters(self, hasher: Argon2idPasswordHasher) -> None:
        """database.md §14.2's "per-row parameters" requirement, satisfied
        by the encoding itself rather than by companion columns."""
        settings = AuthSettings()
        encoded = await hasher.hash(PASSWORD)

        assert f"m={settings.argon2_memory_cost_kib}" in encoded
        assert f"t={settings.argon2_time_cost}" in encoded
        assert f"p={settings.argon2_parallelism}" in encoded

    async def test_the_result_verifies_against_the_reference_library(
        self, hasher: Argon2idPasswordHasher
    ) -> None:
        """Proves the output is a real, checkable Argon2id hash rather than
        merely a plausible-looking string — and that A64-011.2's login will
        be able to verify what registration wrote."""
        encoded = await hasher.hash(PASSWORD)
        assert Argon2PasswordHasher().verify(encoded, PASSWORD) is True

    async def test_a_wrong_password_does_not_verify(self, hasher: Argon2idPasswordHasher) -> None:
        from argon2.exceptions import VerifyMismatchError

        encoded = await hasher.hash(PASSWORD)
        with pytest.raises(VerifyMismatchError):
            Argon2PasswordHasher().verify(encoded, "WrongHorse1!")

    async def test_custom_parameters_are_honoured(self) -> None:
        settings = AuthSettings(argon2_time_cost=1, argon2_memory_cost_kib=8192)
        encoded = await Argon2idPasswordHasher(settings).hash(PASSWORD)

        assert "t=1" in encoded
        assert "m=8192" in encoded


class TestDoesNotBlockTheEventLoop:
    async def test_the_loop_keeps_running_during_concurrent_hashes(self) -> None:
        """The reason `hash` is async at all.

        Argon2id burns ~20ms of solid CPU per call. Run inline it would
        freeze the event loop — and with it every concurrent request,
        including live match traffic whose whole budget is 25ms
        (system-design.md CP-1). This asserts the loop still makes
        progress while eight hashes are in flight.
        """
        hasher = Argon2idPasswordHasher(AuthSettings())
        ticks = 0

        async def heartbeat() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.001)
                ticks += 1

        beat = asyncio.create_task(heartbeat())
        await asyncio.sleep(0.01)
        ticks = 0

        started = time.perf_counter()
        await asyncio.gather(*[hasher.hash(f"Password{index}!") for index in range(8)])
        elapsed_ms = (time.perf_counter() - started) * 1000

        beat.cancel()

        # A blocked loop ticks exactly zero times; measured directly, the
        # inline version scores 0 and this one scores dozens. The threshold
        # is deliberately far below what was observed (~77% of ideal) so
        # this asserts "the loop ran" rather than pinning a timing number
        # that would be flaky on a loaded machine.
        assert elapsed_ms > 5, "hashes finished too fast to be meaningful"
        assert ticks >= 5, f"event loop appears blocked: only {ticks} ticks in {elapsed_ms:.0f}ms"
