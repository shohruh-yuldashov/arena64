"""The timing side of account enumeration, measured rather than asserted
in prose.

`test_authentication_service.py` proves an unknown address and a wrong
password return the same type, code and message. That covers everything a
client can *read*. It does not cover the clock, and the clock is where
this leak actually lives: Argon2id costs ~20ms by design, so a sign-in
short-circuited at "no such account" answers in about 1ms. Anyone with a
list of addresses and a stopwatch can then sort them into "has an account
here" and "does not", from an endpoint whose responses are byte-identical.

So this module runs the **real** hasher — a stub would make both paths
instant and prove nothing — and compares the two distributions. It is the
only test in the suite that asserts on wall-clock time, and it is written
to be slow-but-honest rather than fast-and-decorative:

  - **medians, not means**, because one descheduled iteration on a loaded
    CI box would drag a mean far more than the effect being measured;
  - **a power check**, asserting the paths cost far more than a bare
    lookup does — without it, a test comparing two numbers that are both
    ~0 would pass forever after someone deleted the dummy verification;
  - **a generous bound**, because the assertion worth making is "these are
    the same operation", not a pinned percentage that turns a busy
    machine into a red build.
"""

import statistics
import time
from datetime import UTC, datetime
from types import TracebackType
from typing import Self

import pytest
from pydantic import SecretStr

from app.config.settings import AuthSettings
from app.core.enums import Locale
from app.modules.auth.application.commands import AuthenticateUser
from app.modules.auth.application.services import AuthenticationService
from app.modules.auth.domain.exceptions import InvalidCredentials
from app.modules.auth.infrastructure import Argon2idPasswordHasher
from app.modules.users.application.services import UserService
from app.modules.users.application.services.user_credential_service import UserCredentialService
from app.modules.users.domain.entities import User
from app.modules.users.domain.validators import _known_timezones, validate_timezone
from app.modules.users.domain.value_objects import Email, Timezone, Username
from tests.fakes.user_repository import FakeUserRepository

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
EMAIL = "player.one@example.com"
UNKNOWN_EMAIL = "nobody@example.com"
PASSWORD = "CorrectHorse1!"
WRONG_PASSWORD = "WrongHorse9?"

SAMPLES = 7
"""Odd, so the median is an observed value rather than an interpolation,
and small enough that the module stays around a second: each sample is two
real Argon2id operations."""

MAX_RELATIVE_DIFFERENCE = 0.35
"""The two medians must land within 35% of each other.

Measured on an idle machine they agree to within a few percent — this is
loose on purpose. The failure it must catch is a *short-circuit*, which
does not shave 35% off the unknown-address path; it removes ~95% of it.
Anything tighter would be measuring the machine, not the code.
"""


class _FixedClock:
    def now(self) -> datetime:
        return _NOW


class _NullUnitOfWork:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


@pytest.fixture(scope="module")
def hasher() -> Argon2idPasswordHasher:
    return Argon2idPasswordHasher(AuthSettings())


@pytest.fixture
async def credentials(hasher: Argon2idPasswordHasher) -> UserCredentialService:
    user = User.create(
        username=Username("player_one"),
        email=Email(EMAIL),
        password_hash=await hasher.hash(PASSWORD),
        preferred_language=Locale.EN,
        timezone=Timezone("UTC"),
        created_at=_NOW,
    )
    users = UserService(
        users=FakeUserRepository([user]),
        unit_of_work=_NullUnitOfWork(),
        clock=_FixedClock(),
    )
    return UserCredentialService(users)


@pytest.fixture
async def service(
    credentials: UserCredentialService, hasher: Argon2idPasswordHasher
) -> AuthenticationService:
    # Warms the memoised dummy hash. Left cold, the very first
    # unknown-address attempt would pay for two Argon2 operations and skew
    # the first sample — which is a real property of the running system
    # (once per process, ever) but not the one under test here.
    await hasher.dummy_hash()
    return AuthenticationService(
        credentials=credentials, password_hasher=hasher, clock=_FixedClock()
    )


async def _median_seconds(service: AuthenticationService, *, email: str, password: str) -> float:
    samples: list[float] = []
    for _ in range(SAMPLES):
        started = time.perf_counter()
        with pytest.raises(InvalidCredentials):
            await service.authenticate(AuthenticateUser(email=email, password=SecretStr(password)))
        samples.append(time.perf_counter() - started)
    return statistics.median(samples)


class TestUnknownAddressCostsTheSameAsAWrongPassword:
    async def test_the_two_failure_paths_take_comparable_time(
        self, service: AuthenticationService
    ) -> None:
        known = await _median_seconds(service, email=EMAIL, password=WRONG_PASSWORD)
        unknown = await _median_seconds(service, email=UNKNOWN_EMAIL, password=WRONG_PASSWORD)

        difference = abs(unknown - known) / known
        assert difference < MAX_RELATIVE_DIFFERENCE, (
            f"account enumeration by timing: an unknown address took "
            f"{unknown * 1000:.1f}ms against {known * 1000:.1f}ms for a known one "
            f"({difference:.0%} apart). Something is short-circuiting the "
            f"dummy verification in AuthenticationService."
        )

    async def test_the_measurement_could_detect_a_short_circuit(
        self, service: AuthenticationService, credentials: UserCredentialService
    ) -> None:
        """Proves the assertion above has teeth.

        A test comparing two numbers that are both approximately zero
        passes forever, including on the day someone deletes the dummy
        verification. This measures what a short-circuited path *would*
        cost — the bare repository lookup, with no hashing — and asserts
        the real path costs far more, which is what makes a 35% bound a
        meaningful check rather than a formality.
        """
        started = time.perf_counter()
        for _ in range(SAMPLES):
            assert await credentials.find_credentials_by_email(UNKNOWN_EMAIL) is None
        lookup_only = (time.perf_counter() - started) / SAMPLES

        measured = await _median_seconds(service, email=UNKNOWN_EMAIL, password=WRONG_PASSWORD)

        assert measured > lookup_only * 10, (
            f"an unknown-address sign-in took {measured * 1000:.2f}ms against "
            f"{lookup_only * 1000:.2f}ms for a bare lookup — too close for the "
            f"timing assertion above to mean anything."
        )


class TestRowMappingCostsTheSameOnEveryCall:
    """The other half of the leak, and the half a fake repository hides.

    `SqlAlchemyUserRepository._to_domain` builds a `Timezone` for every row
    it maps, and `validate_timezone` used to rebuild the whole IANA name
    set each time — 10.4ms, on the read path of every login for an address
    that exists. `tests/contract/test_login_endpoint.py` asserts the
    end-to-end consequence against real PostgreSQL; this pins the cause,
    so a regression names the function rather than a millisecond count.
    """

    def test_the_timezone_set_is_built_once_per_process(self) -> None:
        validate_timezone("UTC")
        before = _known_timezones.cache_info()

        for _ in range(50):
            validate_timezone("Europe/London")

        after = _known_timezones.cache_info()
        assert after.misses == before.misses, (
            "the IANA timezone set was rebuilt during validation — that is "
            "~10ms per call on the login read path, and it is measurable "
            "from outside as account enumeration."
        )

    def test_validation_is_cheap_enough_to_be_invisible(self) -> None:
        """A bound three orders of magnitude below the 10.4ms that was
        measured, and still far above anything a set lookup costs."""
        validate_timezone("UTC")

        started = time.perf_counter()
        for _ in range(100):
            validate_timezone("Europe/London")
        per_call_ms = (time.perf_counter() - started) / 100 * 1000

        assert per_call_ms < 0.05, f"validate_timezone costs {per_call_ms:.3f}ms per call"
