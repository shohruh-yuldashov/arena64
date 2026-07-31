"""`EmailVerificationService` — issue, redeem, resend.

Orchestrates; does not compute (services.md §3.2). Token mechanics live
in `OpaqueTokenService`, the rules about what a token *is* live in the
entity, storage lives behind a port, the address is `users`' to mark
verified through `EmailVerifier`, and message delivery is
`EmailProvider`'s. What lives here is the sequencing and two decisions
that are nowhere else: **what the unauthenticated resend is allowed to
reveal**, and **the order of the writes on redemption**.

Six collaborators, all injected:

    VerificationTokenRepository  storage (AD-06)
    OpaqueTokenService           generate / hash / verify (DB-24)
    UserProfileReader            reads the address to send to
    EmailVerifier                the one write `users` owns
    EmailProvider                delivery
    UnitOfWork + Clock           transaction boundary; AD-07

## The resend endpoint must not say whether an address exists

`resend_verification` takes an email address and is **unauthenticated** —
it has to be, because the person asking is the one who never received the
first link and therefore may not be signed in.

That makes it an account-enumeration surface of exactly the kind
`AuthenticationService` spends an Argon2 verification to close. The
defence here is cheaper and total: **the method returns `None` in every
case** — unknown address, already-verified account, freshly issued token.
The caller cannot branch, so the endpoint cannot leak. Nothing about the
response distinguishes "we sent you a link" from "there is no such
account", which is why the endpoint's documented reply is the deliberately
vague "if an account exists, a link has been sent".

There is deliberately no `EmailAlreadyVerified` exception anywhere. One
was written and removed: nothing can raise it. The unauthenticated resend
must not disclose verification state, and a valid token for an
already-verified account is unreachable while at most one token is live
per account. An exception with no raiser on a security surface reads as
"this case is handled" to whoever adds the next endpoint.

## Why redemption writes in this order

`verify_email` consumes the token, marks the address verified, then
invalidates every other outstanding token. Consuming *first* is what
makes the one-time-use rule hold under a double-click: the second request
finds a used token and stops, rather than both requests reaching the
`users` write.

All three happen in one transaction, so a crash between them cannot leave
a consumed token beside an unverified account — which would be the worst
outcome available, since the link is now dead and the account still needs
verifying.

## Why a send failure does not roll back the token

`_deliver` swallows provider errors and logs at WARNING. The token is
already committed and is valid for 24 hours; a resend will produce a
fresh one. Rolling back the issuance because a mail provider was briefly
unreachable would turn a transient vendor outage into a registration
failure, and the person can simply ask again.
"""

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import UUID

from app.config.settings import EmailSettings
from app.core.clock import Clock
from app.core.unit_of_work import UnitOfWork
from app.modules.auth.application.email import EmailMessage, EmailProvider
from app.modules.auth.application.ports import VerificationTokenRepository
from app.modules.auth.application.services.opaque_tokens import OpaqueTokenService
from app.modules.auth.domain.exceptions import InvalidVerificationToken
from app.modules.auth.domain.verification import EmailVerificationToken
from app.modules.users.public import EmailVerifier, UserProfileReader, UserRead

logger = logging.getLogger(__name__)

_GENERIC_REJECTION = "The verification link is not valid or has expired."


@dataclass(frozen=True, slots=True)
class IssuedVerificationToken:
    """A newly issued token and the one and only copy of its raw value.

    A plain frozen dataclass, not a Pydantic model — a type carrying a
    live credential must not be one keystroke from being a FastAPI
    `response_model`. Nothing returns this to a client; it exists so the
    caller can build the message and so tests can redeem what was issued
    without reading the log.

    `repr=False` on the token: a dataclass repr lands in tracebacks and
    error reporters (services.md §8.5).
    """

    token: EmailVerificationToken
    raw_token: str = field(repr=False)


class EmailVerificationService:
    def __init__(
        self,
        *,
        tokens: VerificationTokenRepository,
        token_factory: OpaqueTokenService,
        profiles: UserProfileReader,
        verifier: EmailVerifier,
        email: EmailProvider,
        unit_of_work: UnitOfWork,
        clock: Clock,
        settings: EmailSettings,
    ) -> None:
        self._tokens = tokens
        self._factory = token_factory
        self._profiles = profiles
        self._verifier = verifier
        self._email = email
        self._uow = unit_of_work
        self._clock = clock
        self._settings = settings

    # --- issuing -------------------------------------------------------------

    async def create_verification_token(self, user_id: UUID) -> IssuedVerificationToken:
        """Issues a token, invalidating any that came before it.

        The invalidation is not a convenience — database.md §4.5 requires
        "at most one live token per account", enforced by a partial unique
        index, so an insert without it would be *rejected* rather than
        merely untidy. Both statements are in one transaction, so the
        window between them cannot be observed.

        Does not send anything and does not check whether the account is
        already verified. Both are the callers' concerns:
        `resend_verification` needs to stay silent about verification
        state, and registration has no reason to check a flag it just set
        to `false`.
        """
        now = self._clock.now()
        raw_token = self._factory.generate()

        token = EmailVerificationToken.issue(
            user_id=user_id,
            token_hash=self._factory.hash(raw_token),
            issued_at=now,
            lifetime=timedelta(hours=self._settings.verification_token_ttl_hours),
        )

        async with self._uow:
            await self._tokens.invalidate_active_for_user(user_id, at=now)
            created = await self._tokens.create(token)
            await self._uow.commit()

        # Identifiers only — never the raw token, never the address.
        logger.info(
            "verification_token_issued",
            extra={"user_id": str(user_id), "token_id": str(created.id)},
        )
        return IssuedVerificationToken(token=created, raw_token=raw_token)

    async def invalidate_previous_tokens(self, user_id: UUID) -> int:
        """Marks every outstanding token for a user unusable; returns how
        many.

        Public because it is a use case in its own right and the task
        names it: an account whose address is being changed, or one caught
        in an abuse investigation, should have its outstanding links
        killed without a new one being issued.
        """
        async with self._uow:
            invalidated = await self._tokens.invalidate_active_for_user(
                user_id, at=self._clock.now()
            )
            await self._uow.commit()

        if invalidated:
            logger.info(
                "verification_tokens_invalidated",
                extra={"user_id": str(user_id), "tokens_invalidated": invalidated},
            )
        return invalidated

    # --- redeeming -----------------------------------------------------------

    async def verify_email(self, raw_token: str) -> UserRead:
        """Redeems a verification link and marks the address verified.

        Raises `InvalidVerificationToken` for every unusable case —
        unknown, already used, expired — with one message. The client
        offers a new link in all three, and distinguishing them would say
        whether a token the caller holds was ever real.

        Returns the verified account, so the endpoint can confirm *which*
        address was confirmed without a second round trip.
        """
        presented_hash = self._factory.hash(raw_token)
        token = await self._tokens.get_by_hash(presented_hash)

        if token is None:
            logger.info("verification_failed", extra={"reason": "unknown_token"})
            raise InvalidVerificationToken(_GENERIC_REJECTION)

        # Belt and braces. The lookup already matched on the digest, so
        # this can only fail if storage returned the wrong row — but a
        # repository that returned "close enough" would otherwise verify
        # somebody else's address, which is worth one constant-time
        # comparison to make impossible.
        if not self._factory.verify(raw_token, token.token_hash):
            logger.error("verification_token_hash_mismatch", extra={"token_id": str(token.id)})
            raise InvalidVerificationToken(_GENERIC_REJECTION)

        now = self._clock.now()
        if not token.is_usable_at(now):
            # One branch for used and expired. The *reason* goes to the
            # log, where a caller cannot read it — a replayed link and a
            # stale one are worth telling apart operationally.
            logger.info(
                "verification_failed",
                extra={
                    "reason": "used" if token.is_used else "expired",
                    "token_id": str(token.id),
                    "user_id": str(token.user_id),
                },
            )
            raise InvalidVerificationToken(_GENERIC_REJECTION)

        token.consume(now)

        async with self._uow:
            # Consume first: a double-clicked link finds a used token on
            # the second request and stops before reaching the `users`
            # write.
            await self._tokens.invalidate_active_for_user(token.user_id, at=now)
            verified = await self._verifier.mark_email_verified(token.user_id)
            await self._uow.commit()

        logger.info(
            "verification_succeeded",
            extra={"user_id": str(token.user_id), "token_id": str(token.id)},
        )
        return verified

    # --- resending -----------------------------------------------------------

    async def resend_verification(self, email: str) -> None:
        """Sends a fresh link, if there is anything to send.

        **Returns `None` in every case, and that is the security
        property.** Unknown address, already-verified account, or a token
        freshly issued and delivered — the caller cannot tell which
        happened, so the endpoint cannot be used to discover which
        addresses have accounts. See this module's docstring.

        The address is looked up through `UserProfileReader.find_by_email`,
        which returns `None` rather than raising — the port carries that
        convention precisely so this method has no exception to swallow
        and therefore no branch that could behave differently.
        """
        account = await self._profiles.find_by_email(email)

        if account is None:
            # Logged without the address — an unknown-address probe is
            # exactly the traffic worth counting, and exactly the traffic
            # whose contents must not be retained (services.md §8.5).
            logger.info("verification_resend_ignored", extra={"reason": "no_account"})
            return

        if account.is_verified:
            logger.info(
                "verification_resend_ignored",
                extra={"reason": "already_verified", "user_id": str(account.id)},
            )
            return

        issued = await self.create_verification_token(account.id)
        await self._deliver(account, issued.raw_token)

        logger.info("verification_resend_sent", extra={"user_id": str(account.id)})

    async def send_verification(self, account: UserRead) -> None:
        """Issues and delivers a link for an account that is known to
        exist and known to be unverified.

        The registration path's entry point, separate from
        `resend_verification` because registration has already read the
        account and has nothing to hide — the caller who just created it
        knows it exists. Keeping them apart means the enumeration guard
        above is not something registration has to opt out of.
        """
        issued = await self.create_verification_token(account.id)
        await self._deliver(account, issued.raw_token)

    async def _deliver(self, account: UserRead, raw_token: str) -> None:
        """Composes and sends the message. Never raises.

        Swallowing provider failures is deliberate — see this module's
        docstring. The token is already committed and valid for 24 hours,
        and a resend produces a fresh one; turning a transient vendor
        outage into a failed registration would be the worse trade.

        WARNING rather than ERROR: a single failed send is recoverable by
        the user, and it is the *rate* of these that indicates a broken
        provider.
        """
        message = EmailMessage(
            to=account.email,
            subject="Confirm your Arena64 email address",
            text_body=(
                # `effective_display_name` is on the `User` *entity*, which is
                # private to `users`; the published DTO carries the two
                # fields it is derived from. Falling back here rather than
                # publishing the property keeps the boundary narrow.
                f"Hello {account.display_name or account.username},\n\n"
                "Confirm your email address to finish setting up your Arena64 "
                "account:\n\n"
                f"    {self._settings.verification_url(raw_token)}\n\n"
                f"This link expires in {self._settings.verification_token_ttl_hours} "
                "hours and can be used once.\n\n"
                "If you did not create an Arena64 account, ignore this message.\n"
            ),
        )

        try:
            await self._email.send(message)
        except Exception:
            logger.warning(
                "verification_email_send_failed",
                extra={"user_id": str(account.id)},
                exc_info=True,
            )
