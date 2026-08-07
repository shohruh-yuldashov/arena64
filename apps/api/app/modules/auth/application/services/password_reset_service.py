"""`PasswordResetService` — request a reset, redeem one, and sign
everything out.

Orchestrates; does not compute (services.md §3.2). Token mechanics live in
`OpaqueTokenService`, the rules about what a token *is* live in the
entity, the password policy lives in `domain/validators.py`, hashing lives
behind `PasswordHasher`, session revocation lives in `SessionService`,
storage lives behind a port, the password column is `users`' to write
through `PasswordResetter`, and delivery is `EmailProvider`'s.

Almost nothing here is new. What *is* new — and what this module exists to
hold — is four decisions that are nowhere else:

    what the forgot endpoint is allowed to reveal      (nothing)
    what order the checks run in                       (policy, then token)
    what order the writes run in                       (sessions before
                                                        the password)
    what a delivery failure may do to the response     (nothing)

Nine collaborators, all injected:

    PasswordResetTokenRepository  storage (AD-06)
    OpaqueTokenService            generate / hash / verify (DB-24)
    UserProfileReader             reads the address to send to
    PasswordResetter              the one write `users` owns
    PasswordHasher                Argon2id, on a worker thread
    SessionService                revocation — reused, not reimplemented
    EmailProvider                 delivery
    UnitOfWork + Clock            transaction boundary; AD-07

## The forgot endpoint must not say whether an address exists

`forgot_password` takes an email address and is **unauthenticated** — it
has to be, because the person asking is by definition the one who cannot
sign in.

That makes it an account-enumeration surface of exactly the kind
`AuthenticationService` spends an Argon2 verification to close, and a more
attractive one than the verification resend: an attacker probing here is
mapping which addresses are worth a phishing campaign that ends in a
password. The defence is the same and it is total: **the method returns
`None` in every case** — unknown address, deactivated account, or a link
freshly issued and delivered. The caller cannot branch, so the endpoint
cannot leak.

Returning `None` on the happy paths is only half of it: **every failure
below this method has to be caught too.** An exception is a branch, and a
branch a caller can observe is the leak. Two can reach here and both are
swallowed deliberately — a mail provider being unreachable (see below) and
the unique index rejecting a concurrent insert (see `forgot_password`).
Both are impossible for an unknown address, which returns before touching
storage, so either one escaping would make the endpoint answer differently
for accounts that exist.

What that costs is honesty about what it does *not* close. The three paths
do different amounts of work — an unknown address does one indexed read,
a real one does a write and an SMTP conversation — so the endpoint remains
distinguishable by *timing*. Closing that needs the response decoupled
from delivery (a queue) plus a floor on elapsed time, and it is the wrong
task to do it in: rate limiting is A64-011.8's, and a throttle that stops
the thousandth probe is worth more than a timing equaliser that slows the
first. It is in that task's recommendations, stated rather than assumed.

## Why the password policy is checked before the token

`reset_password` validates the *new password* first and only then looks at
the token, which is the opposite of the obvious order and is deliberate.

Checking the token first creates an oracle. An attacker holding a
candidate token submits it with a deliberately awful password: a
`weak_password` reply means the token was real and is *still unconsumed*,
while `invalid_reset_token` means it was not — a free, repeatable,
side-effect-free test of any token they care to guess. Validating the
policy first collapses that: an unusable password is rejected identically
whatever the token was.

It is also the kinder order. Somebody who fumbles their new password gets
to try again with the same link rather than discovering their one-time
token was burned by a typo.

The endpoint's schema validates the password too, before this service is
reached, so over HTTP the check is doubled. That is not redundancy to
remove — a Pydantic validator can only be reused by Pydantic, and this
method is reachable from a future CLI, an admin tool, or a test.

## Why the writes run in this order

`reset_password` consumes the token, revokes every session, and only then
writes the new password hash. All three are on one session, but they are
not one commit — the `users` write goes through a published port that owns
its own transaction (BE-05), so there is a window between the second and
the third.

The order chooses which side of that window to fail on. As written, a
crash leaves the token consumed and the sessions revoked with the old
password still in place: the person is signed out everywhere, their old
password still works, and they can request a new link. Annoying, and safe.

Reversed — password first, sessions second — a crash leaves the new
password in place and **the attacker's session still live**, which is the
precise outcome the whole flow exists to prevent. One ordering fails
closed and the other fails open, and they cost the same.

Consuming *first* is what makes one-time use hold under a double-click:
the second request finds a used token and stops before reaching anything
else.

## Why hashing happens before the transaction opens

Argon2id is deliberately ~20ms of memory-hard work (`AuthSettings`). Doing
it inside the transaction would hold a PostgreSQL connection open for the
duration for no reason — the hash depends on nothing the transaction
reads. On a public endpoint that is also an availability question: the
connection pool is the scarce resource, not CPU.

## Why a send failure does not fail the request

`_deliver` swallows provider errors and logs at WARNING, and here that is
not merely a robustness nicety as it is on the verification path — it is
load-bearing for the enumeration guard above.

An unknown address never reaches the provider, so it can never fail. If a
real address *could* return a 500 when the mail vendor was unreachable,
then a vendor outage would turn this endpoint into exactly the oracle the
rest of this module is built to deny: 500 for accounts that exist, 204 for
accounts that do not. Swallowing the error is what keeps the two replies
identical on the worst day rather than only on a good one.
"""

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import UUID

from app.config.settings import EmailSettings
from app.core.clock import Clock
from app.core.exceptions import ConflictError
from app.core.unit_of_work import UnitOfWork
from app.modules.auth.application.ports import PasswordHasher, PasswordResetTokenRepository
from app.modules.auth.application.services.opaque_tokens import OpaqueTokenService
from app.modules.auth.application.services.session_service import SessionService
from app.modules.auth.domain.exceptions import InvalidResetToken
from app.modules.auth.domain.password_reset import PasswordResetToken
from app.modules.auth.domain.sessions import RevocationReason
from app.modules.auth.domain.validators import validate_password
from app.modules.users.public import PasswordResetter, UserProfileReader, UserRead
from app.platform.email import EmailMessage, EmailProvider

logger = logging.getLogger(__name__)

_GENERIC_REJECTION = "The password reset link is not valid or has expired."


@dataclass(frozen=True, slots=True)
class IssuedResetToken:
    """A newly issued token and the one and only copy of its raw value.

    A plain frozen dataclass, not a Pydantic model — a type carrying a live
    credential must not be one keystroke from being a FastAPI
    `response_model`, and of every credential on this platform this is the
    one that must never reach a response body: it replaces a password
    without knowing the old one. Nothing returns this to a client; it
    exists so the caller can build the message and so tests can redeem what
    was issued without reading the log.

    `repr=False` on the token: a dataclass repr lands in tracebacks and
    error reporters (services.md §8.5).
    """

    token: PasswordResetToken
    raw_token: str = field(repr=False)


class PasswordResetService:
    def __init__(
        self,
        *,
        tokens: PasswordResetTokenRepository,
        token_factory: OpaqueTokenService,
        profiles: UserProfileReader,
        resetter: PasswordResetter,
        password_hasher: PasswordHasher,
        sessions: SessionService,
        email: EmailProvider,
        unit_of_work: UnitOfWork,
        clock: Clock,
        settings: EmailSettings,
    ) -> None:
        self._tokens = tokens
        self._factory = token_factory
        self._profiles = profiles
        self._resetter = resetter
        self._hasher = password_hasher
        self._sessions = sessions
        self._email = email
        self._uow = unit_of_work
        self._clock = clock
        self._settings = settings

    # --- issuing -------------------------------------------------------------

    async def create_reset_token(self, user_id: UUID) -> IssuedResetToken:
        """Issues a token, invalidating any that came before it.

        The invalidation is not a convenience — database.md §4.5 requires
        "at most one live token per account", enforced by a partial unique
        index, so an insert without it would be *rejected* rather than
        merely untidy. Both statements are in one transaction, so the
        window between them cannot be observed.

        Does not send anything and does not check whether the account may
        have a reset. Both are `forgot_password`'s concerns, which is what
        keeps this method usable by an operator-initiated flow later
        without that flow having to opt out of an enumeration guard it does
        not need.
        """
        now = self._clock.now()
        raw_token = self._factory.generate()

        token = PasswordResetToken.issue(
            user_id=user_id,
            token_hash=self._factory.hash(raw_token),
            issued_at=now,
            lifetime=timedelta(hours=self._settings.password_reset_token_ttl_hours),
        )

        async with self._uow:
            await self._tokens.invalidate_active_for_user(user_id, at=now)
            created = await self._tokens.create(token)
            await self._uow.commit()

        # Identifiers only — never the raw token, never the address.
        logger.info(
            "password_reset_token_issued",
            extra={"user_id": str(user_id), "token_id": str(created.id)},
        )
        return IssuedResetToken(token=created, raw_token=raw_token)

    async def invalidate_previous_tokens(self, user_id: UUID) -> int:
        """Marks every outstanding reset token for a user unusable; returns
        how many.

        Public because it is a use case in its own right: an account being
        investigated for abuse, or one whose owner says "I did not request
        that email", should have its outstanding links killed without a new
        one being issued. It is also the method a future support tool
        calls, and having it here means that tool does not reach for the
        repository.
        """
        async with self._uow:
            invalidated = await self._tokens.invalidate_active_for_user(
                user_id, at=self._clock.now()
            )
            await self._uow.commit()

        if invalidated:
            logger.info(
                "password_reset_tokens_invalidated",
                extra={"user_id": str(user_id), "tokens_invalidated": invalidated},
            )
        return invalidated

    async def invalidate_all_sessions(self, user_id: UUID) -> int:
        """Revokes every live session for an account; returns how many.

        Delegates to `SessionService` rather than reaching for the session
        repository. That is the whole reason the method reads as a
        one-liner: revocation already exists, is already atomic in one
        statement, already logs, and already handles the idempotent case.
        A second implementation here would be a second place for "log out
        everywhere" to be subtly wrong.

        `RevocationReason.PASSWORD_CHANGE` rather than a new
        `PASSWORD_RESET` member, and the choice is deliberate on both
        counts. It is accurate — the password did change, and that is why
        the sessions died — and adding a member means an `ALTER TYPE` on a
        PostgreSQL enum for a distinction that nothing branches on. What
        separates a reset from an authenticated change is recorded where it
        belongs: in this module's `password_reset_succeeded` log line,
        which an incident reads anyway.

        **No `except_session_id`.** SE-1's exception — "revoke every
        session except the one performing it" — exists for an
        *authenticated* password change, where signing the user out of the
        device they are typing on reads as a bug. A reset is the opposite
        case. The person performing it has no session, by definition, and
        the plausible reason they are here is that somebody else does.
        Keeping any session alive through a recovery flow would leave
        exactly the one the flow is meant to kill.
        """
        return await self._sessions.revoke_all_sessions(
            user_id, reason=RevocationReason.PASSWORD_CHANGE
        )

    # --- redeeming -----------------------------------------------------------

    async def validate_reset_token(self, raw_token: str) -> PasswordResetToken:
        """Resolves a raw token to a usable one, or raises.

        Raises `InvalidResetToken` for every unusable case — unknown,
        already used, expired — with one message. The client offers a new
        link in all three, and distinguishing them would say whether a
        token the caller holds was ever real.

        **Read-only. Consumes nothing.** That is what makes it safe to call
        twice, which `reset_password` does not need but a "is this link
        still good?" check on page load would — the frontend can decide
        whether to render the password form before asking somebody to type
        a password twice into a dead link. No endpoint exposes it yet
        because the task's endpoint list does not include one; the method
        is public because the task names it and because that endpoint is
        one handler away.
        """
        presented_hash = self._factory.hash(raw_token)
        token = await self._tokens.get_by_hash(presented_hash)

        if token is None:
            logger.info("password_reset_failed", extra={"reason": "unknown_token"})
            raise InvalidResetToken(_GENERIC_REJECTION)

        # Belt and braces. The lookup already matched on the digest, so
        # this can only fail if storage returned the wrong row — but a
        # repository that returned "close enough" would otherwise let
        # somebody replace another account's password, which is worth one
        # constant-time comparison to make impossible.
        if not self._factory.verify(raw_token, token.token_hash):
            logger.error("password_reset_token_hash_mismatch", extra={"token_id": str(token.id)})
            raise InvalidResetToken(_GENERIC_REJECTION)

        now = self._clock.now()
        if not token.is_usable_at(now):
            # One branch for used and expired. The *reason* goes to the
            # log, where a caller cannot read it — a replayed link and a
            # stale one are worth telling apart operationally, and on this
            # table a replay is the more interesting of the two.
            logger.info(
                "password_reset_failed",
                extra={
                    "reason": "used" if token.is_used else "expired",
                    "token_id": str(token.id),
                    "user_id": str(token.user_id),
                },
            )
            raise InvalidResetToken(_GENERIC_REJECTION)

        return token

    async def reset_password(self, raw_token: str, new_password: str) -> None:
        """Redeems a reset link and replaces the account's password.

        Raises `WeakPassword` if the new password fails the policy, and
        `InvalidResetToken` for every unusable token. The policy is checked
        **first** — see this module's docstring on why that order is a
        security property rather than a preference.

        Returns `None`. Deliberately: there is no account to hand back and
        nothing a caller could do with one. Every session has just been
        revoked, so whoever called this holds no credential and must sign
        in with the new password — returning a profile would invite an
        endpoint to imply otherwise, and returning a token pair would hand
        a fresh session to somebody who has proven control of an inbox
        rather than knowledge of a password.
        """
        # 1. Policy first, unconditionally. Raises `WeakPassword`.
        validate_password(new_password)

        # 2. Then the token. Read-only; nothing is consumed yet, so a
        #    failure here leaves the link usable.
        token = await self.validate_reset_token(raw_token)
        now = self._clock.now()

        # 3. Hash outside the transaction — ~20ms of memory-hard work that
        #    depends on nothing the transaction reads, and holding a
        #    connection through it would spend the scarce resource.
        new_hash = await self._hasher.hash(new_password)

        async with self._uow:
            # Consume first: a double-submitted form finds a used token on
            # the second request and stops. One statement marks the
            # presented token *and* every other outstanding one used, which
            # is both halves of the requirement — replay prevention and
            # "invalidate all remaining reset tokens" — in one round trip.
            invalidated = await self._tokens.invalidate_active_for_user(token.user_id, at=now)

            # Sessions before the password. See this module's docstring:
            # a crash here leaves somebody signed out with a working old
            # password, and the reverse order would leave an attacker
            # holding a live session on an account whose password just
            # changed.
            revoked = await self.invalidate_all_sessions(token.user_id)

            # `users`' write, through the one port that can make it.
            # Raises `UserNotFound` if the account was deleted between the
            # link being issued and being clicked.
            await self._resetter.reset_password(token.user_id, new_hash=new_hash)
            await self._uow.commit()

        # Never the token, never the password, never the address — only
        # what happened, to whom, and how much it invalidated.
        logger.info(
            "password_reset_succeeded",
            extra={
                "user_id": str(token.user_id),
                "token_id": str(token.id),
                "sessions_revoked": revoked,
                "tokens_invalidated": invalidated,
            },
        )

    # --- requesting ----------------------------------------------------------

    async def forgot_password(self, email: str) -> None:
        """Sends a reset link, if there is anything to send.

        **Returns `None` in every case, and that is the security
        property.** Unknown address, deactivated account, or a link
        genuinely issued and delivered — the caller cannot tell which
        happened, so the endpoint cannot be used to discover which
        addresses have accounts. See this module's docstring.

        The address is looked up through `UserProfileReader.find_by_email`,
        which returns `None` rather than raising — the port carries that
        convention precisely so this method has no exception to swallow and
        therefore no branch that could behave differently.

        **A deactivated account gets nothing**, and this is the one policy
        decision in the method. Somebody who cannot sign in must not be
        able to have their credential rotated by a stranger who knows their
        address, and a reset that "succeeds" into an account that still
        cannot sign in helps nobody. Reactivation is an administrative
        action, not a self-service one.

        An **unverified** account does get a link, which is the opposite
        call and the right one: the address on file is the address the
        account was registered with, sending there proves nothing new is
        being trusted, and refusing would strand anybody who registered,
        never clicked the verification link, and then forgot their
        password — a very ordinary sequence.
        """
        account = await self._profiles.find_by_email(email)

        if account is None:
            # Logged without the address — an unknown-address probe is
            # exactly the traffic worth counting, and exactly the traffic
            # whose contents must not be retained (services.md §8.5).
            logger.info("password_reset_requested", extra={"outcome": "no_account"})
            return

        if not account.is_active:
            logger.info(
                "password_reset_requested",
                extra={"outcome": "inactive_account", "user_id": str(account.id)},
            )
            return

        try:
            issued = await self.create_reset_token(account.id)
        except ConflictError:
            # The partial unique index rejected the insert, which happens
            # when a genuinely concurrent request for the *same account*
            # issued its token between this one's invalidate and its
            # create.
            #
            # Swallowed rather than propagated, and this is not tidiness.
            # `ConflictError` is a 409, and an unknown address can never
            # produce one — it returns two lines above without touching
            # storage. Letting it escape would mean the endpoint answers
            # 409 for accounts that exist and 204 for accounts that do
            # not, handing back exactly the enumeration oracle the
            # identical replies exist to deny, to anyone willing to send
            # two requests at once. Which is everyone.
            #
            # Nothing is lost by returning quietly: the request that won
            # the race has issued a token and sent it, so a link is on its
            # way to the same inbox. Sending a second would only invalidate
            # the first.
            logger.warning(
                "password_reset_requested",
                extra={"outcome": "concurrent_request", "user_id": str(account.id)},
            )
            return

        await self._deliver(account, issued.raw_token)

        logger.info(
            "password_reset_requested",
            extra={"outcome": "link_sent", "user_id": str(account.id)},
        )

    async def _deliver(self, account: UserRead, raw_token: str) -> None:
        """Composes and sends the message. Never raises.

        Swallowing provider failures is what keeps the enumeration guard
        working on a bad day as well as a good one — see this module's
        docstring. WARNING rather than ERROR: a single failed send is
        recoverable by the user asking again, and it is the *rate* of these
        that indicates a broken provider.
        """
        hours = self._settings.password_reset_token_ttl_hours
        message = EmailMessage(
            to=account.email,
            subject="Reset your Arena64 password",
            text_body=(
                # `effective_display_name` is on the `User` *entity*, which
                # is private to `users`; the published DTO carries the two
                # fields it is derived from. Falling back here rather than
                # publishing the property keeps the boundary narrow.
                f"Hello {account.display_name or account.username},\n\n"
                "Somebody asked to reset the password on your Arena64 account. "
                "If it was you, choose a new password here:\n\n"
                f"    {self._settings.password_reset_url(raw_token)}\n\n"
                f"This link expires in {hours} hour{'s' if hours != 1 else ''} and can "
                "be used once. Resetting your password signs you out on every "
                "device.\n\n"
                # The one sentence in this message that does real work. A
                # reset email arriving unrequested is the first thing
                # somebody sees when an attacker is probing their account,
                # and the correct advice is genuinely "do nothing" —
                # saying so stops a worried person from clicking the link
                # to "check", which is the one action that would consume
                # their token for the attacker's benefit.
                "If you did not ask for this, ignore this message. Your password "
                "has not changed and no action is needed.\n"
            ),
        )

        try:
            await self._email.send(message)
        except Exception:
            logger.warning(
                "password_reset_email_send_failed",
                extra={"user_id": str(account.id)},
                exc_info=True,
            )
