"""`GoogleSignInService` — "Continue with Google", A64-031.E1.

Orchestrates; proves nothing itself. Google's signature and claims are the
verifier's (`infrastructure/google_oidc.py`), the account and the link are
`users`' (`ExternalIdentityAccounts`), sessions are `SessionService`'s, and
every account-state check is `AuthenticationService`'s. What lives here is
the order those happen in, and the one decision that is genuinely this
feature's: what a verified Google identity is allowed to reach.

## What a verified identity may reach — ADR-008

    already linked        that account, after the same inactive / locked /
                          restricted checks a password sign-in passes
    address unused        a **new** account — after the person chooses a
                          username, created verified and linked in one
                          transaction
    address in use        **nothing yet.** The person must enter that
                          account's password; only then is Google linked
    address unverified    nothing at all

The third row is the one that matters. An address match is never proof:
Google vouches that somebody controls an address *now*, not that they are
the person who registered an Arena64 account with it — and an account
someone else registered with your address (unverified, abandoned, or
planted) must not be handed to you, nor yours to them, on that basis.
Linking requires both proofs together: Google for the identity, the
password for the account.

## Where the in-between state lives

In Redis, under the SHA-256 of a value only the initiating browser holds
in an `HttpOnly` cookie. Never process memory — `start` and `callback`
reach different replicas — and never a URL.
"""

import logging
from dataclasses import dataclass, field

from pydantic import SecretStr

from app.modules.auth.application.commands import AuthenticateUser, NewAccount
from app.modules.auth.application.ports import (
    ExternalSignInStore,
    GoogleIdentityVerifier,
    PasswordHasher,
)
from app.modules.auth.application.services.authentication_service import (
    INVALID_CREDENTIALS_MESSAGE,
    AuthenticationService,
)
from app.modules.auth.application.services.opaque_tokens import OpaqueTokenService
from app.modules.auth.domain.exceptions import (
    ExternalSignInExpired,
    GoogleSignInRejected,
    InvalidCredentials,
)
from app.modules.auth.domain.google_sign_in import (
    GoogleIdentity,
    PendingPurpose,
    PendingSignIn,
    SignInFailure,
    StartedSignIn,
    pkce_challenge,
    safe_next_path,
)
from app.modules.users.public import (
    ExternalIdentityAccounts,
    IdentityProvider,
    InvalidEmail,
    UserCredentialStore,
    UserRead,
)

logger = logging.getLogger(__name__)

_NOTHING_PENDING = "There is no Google sign-in in progress."


@dataclass(frozen=True, slots=True)
class BegunSignIn:
    """Where to send the browser, and the binding value to give it."""

    authorization_url: str
    binding: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class SignedIn:
    """The identity was linked; this account may be given a session."""

    account: UserRead
    next_path: str


@dataclass(frozen=True, slots=True)
class AwaitingStep:
    """The identity is verified and waits for the person — see
    `PendingPurpose`. `ticket` goes to the browser in a cookie."""

    purpose: PendingPurpose
    ticket: str = field(repr=False)


class GoogleSignInService:
    def __init__(
        self,
        *,
        verifier: GoogleIdentityVerifier,
        store: ExternalSignInStore,
        accounts: ExternalIdentityAccounts,
        credentials: UserCredentialStore,
        authentication: AuthenticationService,
        password_hasher: PasswordHasher,
        tokens: OpaqueTokenService,
        state_ttl_seconds: int,
        pending_ttl_seconds: int,
    ) -> None:
        self._verifier = verifier
        self._store = store
        self._accounts = accounts
        self._credentials = credentials
        self._authentication = authentication
        self._hasher = password_hasher
        self._tokens = tokens
        self._state_ttl = state_ttl_seconds
        self._pending_ttl = pending_ttl_seconds

    # --- start ---------------------------------------------------------------

    async def begin(self, next_path: str | None) -> BegunSignIn:
        """Mints the flow's four secrets and remembers three of them.

        `state` goes to Google and back in the URL; `binding` goes to this
        browser in a cookie; `nonce` goes to Google and must come back
        inside the signed ID token; the PKCE verifier never leaves the
        server until the exchange. Each is 256 bits from `secrets`.
        """
        state = self._tokens.generate()
        binding = self._tokens.generate()
        nonce = self._tokens.generate()
        code_verifier = self._tokens.generate()

        await self._store.save_started(
            self._tokens.hash(state),
            StartedSignIn(
                binding_digest=self._tokens.hash(binding),
                code_verifier=code_verifier,
                nonce=nonce,
                next_path=safe_next_path(next_path),
            ),
            ttl_seconds=self._state_ttl,
        )
        url = self._verifier.authorization_url(
            state=state, nonce=nonce, code_challenge=pkce_challenge(code_verifier)
        )
        return BegunSignIn(authorization_url=url, binding=binding)

    # --- callback ------------------------------------------------------------

    async def complete(
        self, *, state: str, code: str, binding: str | None
    ) -> SignedIn | AwaitingStep:
        """Finishes the round trip.

        The `state` record is spent before anything else is checked, so a
        replayed callback — or a second one racing the first on another
        replica — finds nothing, whatever else is wrong with it.
        """
        started = await self._store.take_started(self._tokens.hash(state))
        if started is None:
            raise GoogleSignInRejected(SignInFailure.EXPIRED, "unknown, used or expired state")
        if binding is None or not self._tokens.verify(binding, started.binding_digest):
            # Login CSRF: a callback URL started in somebody else's browser.
            raise GoogleSignInRejected(SignInFailure.EXPIRED, "state from another browser")

        identity = await self._verifier.verify(
            code=code, code_verifier=started.code_verifier, nonce=started.nonce
        )
        return await self._resolve(identity, next_path=started.next_path)

    async def _resolve(
        self, identity: GoogleIdentity, *, next_path: str
    ) -> SignedIn | AwaitingStep:
        linked = await self._accounts.find_credentials_by_external_identity(
            IdentityProvider.GOOGLE, identity.subject
        )
        if linked is not None:
            # By `sub` alone — the address may have changed at Google since
            # the link was made, and that is fine: `sub` is who it is.
            return SignedIn(await self._authentication.admit(linked), next_path=next_path)

        if identity.email is None or not identity.email_verified:
            raise GoogleSignInRejected(
                SignInFailure.EMAIL_UNVERIFIED, "unlinked identity without a verified address"
            )

        try:
            existing = await self._credentials.find_credentials_by_email(identity.email)
        except InvalidEmail as error:
            # Google verified it, so this is Google's notion of an address
            # disagreeing with ours — not something the person can fix.
            raise GoogleSignInRejected(SignInFailure.FAILED, "unusable address") from error

        pending = PendingSignIn(
            purpose=PendingPurpose.CREATE_ACCOUNT
            if existing is None
            else PendingPurpose.LINK_ACCOUNT,
            subject=identity.subject,
            email=identity.email,
            user_id=None if existing is None else existing.account.id,
            next_path=next_path,
        )
        ticket = self._tokens.generate()
        await self._store.save_pending(
            self._tokens.hash(ticket), pending, ttl_seconds=self._pending_ttl
        )
        logger.info("google_sign_in_pending", extra={"purpose": pending.purpose.value})
        return AwaitingStep(purpose=pending.purpose, ticket=ticket)

    # --- the step after ------------------------------------------------------

    async def pending(self, ticket: str | None) -> PendingSignIn:
        """What this browser's verified identity is waiting for. Raises
        `ExternalSignInExpired` when there is nothing."""
        _, pending = await self._read_pending(ticket)
        return pending

    async def create_account(
        self,
        ticket: str | None,
        *,
        username: str,
        preferred_language: str,
        timezone: str,
    ) -> UserRead:
        """Creates the account a `CREATE_ACCOUNT` identity was waiting for.

        Through `users`' own creation path, so every rule a password
        registration meets — username shape, reserved names, uniqueness —
        applies unchanged, and a `UsernameAlreadyExists` leaves the ticket
        in place for another try.

        **A password nobody knows.** `users.user.password_hash` is
        `NOT NULL`, and the account must not be reachable by password until
        its owner chooses one — so it gets the hash of 256 random bits that
        are discarded here. "Forgot password" sets a real one, through the
        address Google has just vouched for.
        """
        digest, pending = await self._pending_for(ticket, PendingPurpose.CREATE_ACCOUNT)

        unusable = await self._hasher.hash(self._tokens.generate())
        created = await self._accounts.create_with_external_identity(
            NewAccount(
                username=username,
                email=pending.email,
                password_hash=unusable,
                preferred_language=preferred_language,
                timezone=timezone,
            ),
            provider=IdentityProvider.GOOGLE,
            subject=pending.subject,
        )
        await self._store.discard_pending(digest)
        logger.info("google_account_created", extra={"user_id": str(created.id)})
        return created

    async def link_account(self, ticket: str | None, password: SecretStr) -> UserRead:
        """Links a `LINK_ACCOUNT` identity once the account's password is
        proven, and returns the account to sign in.

        `AuthenticationService.authenticate` does the proving — the same
        Argon2 verification, the same generic failure, the same account
        checks — against the address the callback found. The account must
        still be the one the callback found: if the address moved to
        another account in between, the answer is the generic one.
        """
        digest, pending = await self._pending_for(ticket, PendingPurpose.LINK_ACCOUNT)

        account = await self._authentication.authenticate(
            AuthenticateUser(identifier=pending.email, password=password)
        )
        if account.id != pending.user_id:
            raise InvalidCredentials(INVALID_CREDENTIALS_MESSAGE)

        await self._accounts.link_external_identity(
            account.id, provider=IdentityProvider.GOOGLE, subject=pending.subject
        )
        await self._store.discard_pending(digest)
        return account

    async def _pending_for(
        self, ticket: str | None, purpose: PendingPurpose
    ) -> tuple[bytes, PendingSignIn]:
        digest, pending = await self._read_pending(ticket)
        if pending.purpose is not purpose:
            # A ticket for the other step is, for this one, no ticket.
            raise ExternalSignInExpired(_NOTHING_PENDING)
        return digest, pending

    async def _read_pending(self, ticket: str | None) -> tuple[bytes, PendingSignIn]:
        if ticket is None:
            raise ExternalSignInExpired(_NOTHING_PENDING)
        digest = self._tokens.hash(ticket)
        pending = await self._store.read_pending(digest)
        if pending is None:
            raise ExternalSignInExpired(_NOTHING_PENDING)
        return digest, pending
