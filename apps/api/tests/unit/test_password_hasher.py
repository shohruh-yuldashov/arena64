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
from app.core.exceptions import PermanentInfrastructureError
from app.modules.auth.infrastructure import Argon2idPasswordHasher, build_password_hasher

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


class TestVerify:
    async def test_accepts_the_password_the_hash_was_made_from(
        self, hasher: Argon2idPasswordHasher
    ) -> None:
        encoded = await hasher.hash(PASSWORD)
        assert await hasher.verify(encoded, PASSWORD) is True

    async def test_returns_false_for_a_wrong_password(self, hasher: Argon2idPasswordHasher) -> None:
        """`False`, not an exception.

        A wrong password is the most ordinary outcome this platform has,
        and `AuthenticationService` must treat it identically to "no such
        account" — which is a `None`, not an exception. Symmetry in the
        return type is what keeps the two branches symmetric in the
        caller.
        """
        encoded = await hasher.hash(PASSWORD)
        assert await hasher.verify(encoded, "WrongHorse1!") is False

    async def test_verifies_a_hash_made_at_older_parameters(
        self, hasher: Argon2idPasswordHasher
    ) -> None:
        """The property that makes raising Argon2's cost possible without a
        mass password reset: the parameters travel in the encoding, so a
        hasher configured for today verifies what yesterday wrote."""
        old = Argon2idPasswordHasher(AuthSettings(argon2_time_cost=1, argon2_memory_cost_kib=8192))
        encoded = await old.hash(PASSWORD)

        assert await hasher.verify(encoded, PASSWORD) is True

    @pytest.mark.parametrize(
        "corrupt",
        [
            pytest.param("", id="empty"),
            pytest.param("not-a-hash", id="not-argon2"),
            pytest.param("$argon2id$v=19$m=19456,t=2,p=1$truncated", id="truncated"),
            pytest.param("$bcrypt$2b$12$abcdefghijklmnopqrstuv", id="wrong-algorithm"),
        ],
    )
    async def test_a_corrupt_stored_hash_raises_rather_than_failing_the_login(
        self, hasher: Argon2idPasswordHasher, corrupt: str
    ) -> None:
        """A database holding something that is not a credential is a
        defect, not a wrong password.

        Reported as "invalid credentials" instead, it would leave someone
        permanently unable to sign in with their correct password while
        every dashboard showed an ordinary failure rate — the failure mode
        nobody finds for months.
        """
        with pytest.raises(PermanentInfrastructureError):
            await hasher.verify(corrupt, PASSWORD)


class TestNeedsRehash:
    async def test_false_for_a_hash_at_the_current_parameters(
        self, hasher: Argon2idPasswordHasher
    ) -> None:
        encoded = await hasher.hash(PASSWORD)
        assert await hasher.needs_rehash(encoded) is False

    async def test_true_for_a_hash_made_at_weaker_parameters(
        self, hasher: Argon2idPasswordHasher
    ) -> None:
        """database.md §14.2's rehash-on-login, in one assertion: without
        this returning `True`, raising the configured cost would apply to
        new accounts only and every existing one would stay at the old
        parameters forever."""
        old = Argon2idPasswordHasher(AuthSettings(argon2_time_cost=1, argon2_memory_cost_kib=8192))
        encoded = await old.hash(PASSWORD)

        assert await hasher.needs_rehash(encoded) is True


class TestDummyHash:
    async def test_is_a_real_hash_at_the_current_parameters(
        self, hasher: Argon2idPasswordHasher
    ) -> None:
        """It must cost what a real verification costs, which means it must
        carry the same parameters — a cheaper dummy would make an unknown
        address measurably faster than a known one, which is the entire
        thing it exists to prevent."""
        settings = AuthSettings()
        dummy = await hasher.dummy_hash()

        assert dummy.startswith("$argon2id$")
        assert f"m={settings.argon2_memory_cost_kib}" in dummy
        assert f"t={settings.argon2_time_cost}" in dummy

    async def test_is_memoised_within_an_instance(self, hasher: Argon2idPasswordHasher) -> None:
        """Not an optimisation: derived afresh per call, an unknown-address
        sign-in would spend two Argon2 operations against a known
        address's one — the same timing leak, doubled and sign-flipped."""
        assert await hasher.dummy_hash() == await hasher.dummy_hash()

    async def test_differs_between_instances(self) -> None:
        """Random per instance, so the dummy is not a platform-wide
        constant an attacker could ever have precomputed against."""
        first = await Argon2idPasswordHasher(AuthSettings()).dummy_hash()
        second = await Argon2idPasswordHasher(AuthSettings()).dummy_hash()

        assert first != second

    async def test_nothing_plausible_verifies_against_it(
        self, hasher: Argon2idPasswordHasher
    ) -> None:
        dummy = await hasher.dummy_hash()

        assert await hasher.verify(dummy, PASSWORD) is False
        assert await hasher.verify(dummy, "") is False


class TestBuildPasswordHasher:
    def test_returns_the_same_instance_for_the_same_parameters(self) -> None:
        """The memoised dummy hash only works if the instance is shared —
        see `build_password_hasher`."""
        settings = AuthSettings()
        assert build_password_hasher(settings) is build_password_hasher(settings)

    def test_returns_a_different_instance_for_different_parameters(self) -> None:
        """Otherwise raising the configured cost would silently keep
        handing out a hasher at the old one."""
        weak = build_password_hasher(AuthSettings(argon2_time_cost=1, argon2_memory_cost_kib=8192))
        strong = build_password_hasher(AuthSettings(argon2_time_cost=3))

        assert weak is not strong
