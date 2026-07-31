"""`AuthenticationService` — proves who someone is. Nothing more.

It answers exactly one question: *is this the person who owns this
account?* It issues no token, opens no session, and sets no cookie. That
is A64-011.3's work, and this service is deliberately shaped so that
adding it is a wrapper around `authenticate`, not a rewrite of it — the
method returns the authenticated account, which is precisely the input a
token issuer needs.

## The flow

    1. normalise the address           the same folding registration used,
                                       or "A@B.com" never matches "a@b.com"
    2. look up the credentials         `None` for an unknown address —
                                       a return value, never an exception
    3. verify with Argon2id            against the real hash, or against a
                                       throwaway one if there is no account
    4. reject on mismatch              `InvalidCredentials`, identically
                                       for both causes
    5. rehash if parameters moved      best-effort, never blocking the
                                       login; here rather than last so the
                                       plaintext's lifetime ends at its
                                       last use
    6. check the account is usable     active, and not temporarily locked
    7. return the account

## Why an unknown address still costs a full Argon2 verification

Steps 2–4 are where account enumeration lives, and the leak is not in the
response — it is in the clock. Argon2id at this platform's parameters
costs roughly 20 ms by design. A sign-in for an address that does not
exist, short-circuited at step 2, answers in about 1 ms. Anyone with a
list of addresses and a stopwatch can then sort them into "has an account
here" and "does not", from an endpoint that returns a byte-identical
response either way.

So step 3 always runs. With no account, it verifies the submitted
password against `PasswordHasher.dummy_hash()` — a hash of an unguessable
per-process random string, at the current parameters — which cannot
succeed and costs exactly what a real verification costs. `tests/unit/
test_authentication_timing.py` measures the two paths and asserts they
stay within a bound, because a comment claiming they match is worth
nothing the day someone adds an early `return`.

Equalising the hash is necessary and was not sufficient. Smoke-testing
this task found the same leak one layer down and an order of magnitude
larger: a lookup that *hit* cost 11.5ms more than one that missed, not in
the query (0.40ms against 0.35ms) but in the repository's row mapping,
where constructing a `Timezone` rebuilt the entire IANA name set —
`users.domain.validators._known_timezones` has the fix and the numbers.
No unit test could have caught it, because the fake repository costs the
same either way by construction; `tests/contract/test_login_endpoint.py`
now asserts the property against the adapter that can.

One residual signal is known and accepted: an account whose hash predates
a parameter raise verifies *faster* than the dummy, so it is
distinguishable from a nonexistent one until its next successful sign-in
rehashes it. Closing it would mean padding every login to the slowest
verification the platform has ever configured — a permanent cost for a
window that closes on its own.

## Why `InactiveAccount` and `AccountLocked` are not an oracle

The task specifies both "use a single exception: InvalidCredentials" and
these two named states. Those pull in opposite directions only if the
named states are reachable *before* verification. They are not: both are
checked at step 5, after Argon2 has already confirmed the caller knows
the password.

The distinction that matters is therefore not "which error do we return"
but "what does the caller already know". An anonymous prober learns
nothing — every wrong password and every unknown address is one
`InvalidCredentials`. Someone who has proved they hold the credential
learns their own account is disabled, which they are entitled to know and
which withholding would only convert into support tickets from people
retyping a correct password.

## Why nothing here writes a failure counter

No attempt is recorded and no lock is ever set. Rate limiting is
explicitly out of scope for this task, and automatic lockout after N
failures is not a substitute for it: NIST SP 800-63B §5.2.2 prefers
throttling precisely because a counter keyed on the account is a
denial-of-service primitive — anyone who knows an address can lock its
owner out at will. `locked_until` is read here and written by nothing,
which is the correct state until there is a throttle to write it.
"""

import logging

from app.core.clock import Clock
from app.modules.auth.application.commands import AuthenticateUser
from app.modules.auth.application.ports import PasswordHasher
from app.modules.auth.domain.exceptions import (
    AccountLocked,
    InactiveAccount,
    InvalidCredentials,
)
from app.modules.users.public import InvalidEmail, UserCredentials, UserCredentialStore, UserRead

logger = logging.getLogger(__name__)

_GENERIC_FAILURE_MESSAGE = "Invalid email or password."
"""One string for both causes. A message that varied — even in
punctuation — would reintroduce, in the response body, exactly the
distinction the timing work above removes from the clock."""


class AuthenticationService:
    def __init__(
        self,
        *,
        credentials: UserCredentialStore,
        password_hasher: PasswordHasher,
        clock: Clock,
    ) -> None:
        self._credentials = credentials
        self._hasher = password_hasher
        self._clock = clock

    async def authenticate(self, command: AuthenticateUser) -> UserRead:
        """Verifies a sign-in and returns the account it belongs to.

        Raises `InvalidCredentials` (401) when the pair does not identify
        anyone, `InactiveAccount` (403) when it does but the account is
        deactivated, and `AccountLocked` (403) when sign-in is
        temporarily barred. Returns `UserRead`, which has no
        `password_hash` field — the absence is a property of the type, not
        of remembering to strip it here.
        """
        plaintext = command.password.get_secret_value()

        stored = await self._find_credentials(command.email)

        # The real hash when there is an account, an unguessable throwaway
        # when there is not. Either way one Argon2 verification runs — see
        # this module's docstring on why the branch cannot be an early
        # return.
        expected_hash = stored.password_hash if stored else await self._hasher.dummy_hash()
        verified = await self._hasher.verify(expected_hash, plaintext)

        if stored is None or not verified:
            # One branch, one exception, whichever half failed.
            logger.info("login_failed", extra={"reason": "invalid_credentials"})
            raise InvalidCredentials(_GENERIC_FAILURE_MESSAGE)

        # Before the account-status checks, not after — the two are
        # independent, and doing it here means the plaintext's lifetime
        # ends at the last statement that needs it rather than spanning
        # code that can raise. The cost is a rehash for an account that is
        # then refused, which is harmless: the credential is correct and
        # was going to be upgraded on the next successful sign-in anyway.
        await self._rehash_if_stale(stored, expected_hash, plaintext)

        # Every use is now behind us. This does **not** scrub the string
        # from memory — `command.password` still holds it and CPython
        # promises nothing — but it keeps the plaintext out of the frame
        # locals an error reporter would capture from everything below.
        del plaintext

        self._ensure_usable(stored)

        # The user id only — never the address. A sign-in log line is a
        # permanent record in a system with broader read access and
        # different retention than the database (services.md §8.5), and the
        # id joins back to everything else for anyone entitled to see it.
        logger.info("login_succeeded", extra={"user_id": str(stored.account.id)})
        return stored.account

    async def _find_credentials(self, email: str) -> UserCredentials | None:
        try:
            return await self._credentials.find_credentials_by_email(email)
        except InvalidEmail:
            # A malformed address cannot belong to anyone — nothing that
            # fails this check could ever have been registered. Returning
            # `None` sends it down the dummy-verification path instead of
            # letting it escape as a 422, so that no caller can tell "not a
            # valid address" from "wrong password" by the response *or* by
            # how quickly it arrived.
            #
            # The HTTP schema still rejects malformed addresses earlier with
            # a field-level 422, which is the right feedback for a form.
            # This is the guard for every other caller.
            return None

    def _ensure_usable(self, stored: UserCredentials) -> None:
        """Both checks happen only after a successful verification."""
        if not stored.account.is_active:
            logger.info(
                "login_rejected",
                extra={"user_id": str(stored.account.id), "reason": "inactive_account"},
            )
            raise InactiveAccount("This account has been deactivated.")

        # Read against the injected clock (AD-07), never `datetime.now()`.
        # The lock lapses by itself the moment this comparison stops
        # holding — no job has to run for someone to get back in.
        if stored.locked_until is not None and self._clock.now() < stored.locked_until:
            logger.info(
                "login_rejected",
                extra={"user_id": str(stored.account.id), "reason": "account_locked"},
            )
            raise AccountLocked("This account is temporarily locked. Try again later.")

    async def _rehash_if_stale(
        self,
        stored: UserCredentials,
        expected_hash: str,
        plaintext: str,
    ) -> None:
        """Upgrades the stored hash when Argon2's cost has been raised
        since it was written — database.md §14.2's rehash-on-login.

        A sign-in is the only moment the platform legitimately holds the
        plaintext, so it is the only moment a hash can be re-derived at
        stronger parameters without asking anyone to reset a password.
        Skipping it means the parameters in `AuthSettings` apply to new
        accounts only, and every account that existed before a raise stays
        at the old cost forever.

        **Fail-open, deliberately.** Every failure below is swallowed: the
        caller has already proved who they are, and refusing a valid login
        because an opportunistic security upgrade could not be written
        would convert a background improvement into an outage. A declined
        compare-and-swap (`False`) is not even an error — it means the
        credential changed underneath us, and the new one is the one to
        keep.
        """
        try:
            if not await self._hasher.needs_rehash(expected_hash):
                return

            # A sign-in is the only moment the plaintext exists, so it is
            # the only moment this can be re-derived. `expected_hash` is
            # passed as the compare-and-swap's expectation, so a password
            # changed between the read and this write is not clobbered.
            new_hash = await self._hasher.hash(plaintext)
            await self._credentials.replace_password_hash(
                stored.account.id,
                expected_hash=expected_hash,
                new_hash=new_hash,
            )
        except Exception:
            # Broad on purpose, and this is the one place on the platform
            # where that is right: nothing this method can fail at should
            # ever be visible to someone who has just signed in correctly.
            # Logged at WARNING rather than swallowed silently — a rehash
            # that never succeeds is an operational problem, just not this
            # request's problem.
            logger.warning(
                "password_rehash_failed",
                extra={"user_id": str(stored.account.id)},
                exc_info=True,
            )
