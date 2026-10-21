"""The FastAPI `Depends` bridge for `auth` — dependency-injection.md
DI-01: `Depends` is used only at the routing layer, to hand a route an
already-resolved service. It is not the container.

The graph assembled per request. `users`' side is built five times over,
once per published port, because the ports exist to be separately
grantable capabilities and a single factory returning something that
satisfied all five would quietly undo that:

    AsyncSession                  one per request (`app.api.deps`)
      -> SqlAlchemyUserRepository
      -> SessionUnitOfWork        the transaction `users` will commit
      -> UserService              validation + uniqueness + transaction
           -> UserAccountService      creates accounts
           -> UserCredentialService   reads hashes, compare-and-swaps them
           -> UserProfileService      reads one profile
           -> EmailVerificationWriter marks an address verified
           -> PasswordResetWriter     replaces a hash, cannot read one
      -> SqlAlchemySessionRepository            auth's own tables
      -> SqlAlchemyVerificationTokenRepository
      -> SqlAlchemyPasswordResetTokenRepository
    Argon2idPasswordHasher        process-lifetime singleton
    RedisRateLimiter              process-lifetime singleton (`app.state`)
    Clock                         injected, never read directly (AD-07)
      -> RegistrationService      AuthenticationService
      -> AccessTokenService       SessionService
      -> EmailVerificationService PasswordResetService

**Everything here is per-request except the hasher.** A64-011.1 built
that per-request too, having measured construction at **1 µs** against
the **~19,000 µs** of the hash it performs, and concluded — correctly, on
cost — that the cache was optimising nothing.

A64-011.2 shares it again for a reason cost does not reach: the hasher
memoises the dummy hash that makes an unknown-address sign-in take the
same time as a known one, and a per-request instance memoises nothing.
See `build_password_hasher`. The key is the three integer cost
parameters, not the `AuthSettings` model that broke the earlier attempt.

The repository and unit of work must be per-request for a reason that is
not about cost at all: they hold the request's session
(dependency-injection.md §1.3 — "Never singleton: anything holding a
session... A singleton service holding a session is the classic
production-only bug").

`auth` builds `users`' internals here rather than importing a factory
from `users`, which looks like a boundary violation and is not: this is
the *composition root's* job, and a composition root is the one place
permitted to know how to construct things (BR-6 forbids a *module*
reaching for the container, not the root wiring modules together). The
`RegistrationService` itself sees only `users.public.UserAccountCreator`.
"""

from functools import lru_cache
from typing import Annotated

from fastapi import Depends

from app.api.deps import ClockDep, DbSessionDep, RedisPoolsDep, SettingsDep
from app.api.outbox_deps import EventPublisherDep
from app.api.restriction_deps import AccountRestrictionGateDep
from app.config.settings import Settings
from app.core.clock import Clock
from app.core.constants import API_PREFIX, API_V1_PREFIX
from app.database.unit_of_work import SessionUnitOfWork
from app.modules.auth.application.ports import PasswordHasher
from app.modules.auth.application.services import (
    AccessTokenService,
    AuthenticationService,
    EmailVerificationService,
    GoogleSignInService,
    PasswordResetService,
    RefreshTokenService,
    RegistrationService,
    SessionService,
)
from app.modules.auth.application.services.opaque_tokens import OpaqueTokenService
from app.modules.auth.application.services.websocket_tickets import WebSocketTicketService
from app.modules.auth.infrastructure import (
    GoogleOidcVerifier,
    GoogleSigningKeys,
    JwtTokenProvider,
    RedisExternalSignInStore,
    RedisWebSocketTicketStore,
    SqlAlchemyPasswordResetTokenRepository,
    SqlAlchemySessionRepository,
    SqlAlchemyVerificationTokenRepository,
    build_password_hasher,
)
from app.modules.auth.presentation.dependencies.current_user import (
    CurrentUser,
    OptionalCurrentUser,
    RequireAuthentication,
    TokenValidatorDep,
    get_current_user,
    get_current_user_optional,
    get_token_validator,
)
from app.modules.auth.presentation.dependencies.verified_user import (
    VerifiedUser,
    require_verified_email,
)
from app.modules.users.application.services import UserService
from app.modules.users.application.services.email_verification_writer import (
    EmailVerificationWriter,
)
from app.modules.users.application.services.external_identity_account_service import (
    ExternalIdentityAccountService,
)
from app.modules.users.application.services.password_reset_writer import PasswordResetWriter
from app.modules.users.application.services.user_account_service import UserAccountService
from app.modules.users.application.services.user_credential_service import UserCredentialService
from app.modules.users.application.services.user_profile_service import UserProfileService
from app.modules.users.infrastructure.repositories import (
    SqlAlchemyExternalIdentityRepository,
    SqlAlchemyUserRepository,
)
from app.modules.users.public import (
    EmailVerifier,
    ExternalIdentityAccounts,
    PasswordResetter,
    UserAccountCreator,
    UserCredentialStore,
    UserProfileReader,
)
from app.platform.email import EmailProvider, build_email_provider


def get_password_hasher(settings: SettingsDep) -> PasswordHasher:
    return build_password_hasher(settings.auth)


PasswordHasherDep = Annotated[PasswordHasher, Depends(get_password_hasher)]


def get_user_account_creator(
    session: DbSessionDep, clock: ClockDep, events: EventPublisherDep
) -> UserAccountCreator:
    """Assembles `users`' side of the graph behind its published port.

    The unit of work wraps the *same* session the repository holds —
    otherwise the service would commit a transaction the repository never
    wrote to, and the registration would be silently lost on teardown.

    The publisher wraps that same session too, which is what makes
    `users.registered` part of the registration rather than a second write
    that can fail on its own (AD-16, A64-027.2 §12).
    """
    users = UserService(
        users=SqlAlchemyUserRepository(session),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
        events=events,
    )
    return UserAccountService(users)


UserAccountCreatorDep = Annotated[UserAccountCreator, Depends(get_user_account_creator)]


def get_registration_service(
    accounts: UserAccountCreatorDep,
    password_hasher: PasswordHasherDep,
) -> RegistrationService:
    return RegistrationService(accounts=accounts, password_hasher=password_hasher)


RegistrationServiceDep = Annotated[RegistrationService, Depends(get_registration_service)]


def get_user_credential_store(session: DbSessionDep, clock: ClockDep) -> UserCredentialStore:
    """`users`' side of the login graph, behind its second published port.

    Assembled separately from `get_user_account_creator` rather than
    returning one object satisfying both: the two ports exist apart so
    that registering and reading password hashes are separately grantable
    capabilities, and a single factory handing back something with both
    would quietly undo that.

    The unit of work is here because the rehash-on-login write needs one.
    Nothing on the read path opens a transaction.
    """
    users = UserService(
        users=SqlAlchemyUserRepository(session),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )
    return UserCredentialService(users)


UserCredentialStoreDep = Annotated[UserCredentialStore, Depends(get_user_credential_store)]


def get_authentication_service(
    credentials: UserCredentialStoreDep,
    password_hasher: PasswordHasherDep,
    restrictions: AccountRestrictionGateDep,
    clock: ClockDep,
) -> AuthenticationService:
    return AuthenticationService(
        credentials=credentials,
        password_hasher=password_hasher,
        # A64-024.6 — `domain-model.md` §6's sanctions gate. Resolved in
        # `app/api/` because `auth` may name the published port and may not
        # build `admin`'s adapter; see that module's docstring.
        restrictions=restrictions,
        clock=clock,
    )


AuthenticationServiceDep = Annotated[AuthenticationService, Depends(get_authentication_service)]


def get_access_token_service(
    settings: SettingsDep,
    clock: ClockDep,
) -> AccessTokenService:
    """Token issuance (A64-011.3).

    Wired into `POST /auth/login` and `POST /auth/refresh`, both of which
    return the access token alongside a refresh token (A64-011.5).
    A64-011.3 assembled it here before either endpoint existed, precisely
    so that adding them was one line in a handler rather than a dependency
    graph.
    """
    return AccessTokenService(
        tokens=JwtTokenProvider(settings.jwt, clock),
        settings=settings.jwt,
    )


AccessTokenServiceDep = Annotated[AccessTokenService, Depends(get_access_token_service)]


def get_refresh_token_service(settings: SettingsDep) -> RefreshTokenService:
    """Stateless and cheap — a per-request instance costs one attribute
    assignment against the SHA-256 it exists to perform."""
    return RefreshTokenService(settings.session)


RefreshTokenServiceDep = Annotated[RefreshTokenService, Depends(get_refresh_token_service)]


def get_websocket_ticket_service(
    pools: RedisPoolsDep, clock: ClockDep, settings: SettingsDep
) -> WebSocketTicketService:
    """AD-09's ticket issuer and redeemer — A64-016.1.

    Resolved by two very different callers, and that is the point of it
    being one factory: `POST /auth/ws-ticket` mints, and the gateway's
    `/ws` handshake redeems. A second construction site would be two
    places that decide how a ticket is hashed and how long it lives.

    Redis rather than a session-backed repository, and the **`cache`**
    role — see `RedisWebSocketTicketStore` for the AD-03 argument and for
    why redemption is a single `GETDEL`.

    No kill switch. Presence and the friends cache have one because they
    degrade to a working platform with a feature missing; a gateway that
    could not redeem a ticket cannot accept a connection at all, and a
    switch whose off position is "no realtime" is a deploy decision rather
    than a runtime one.
    """
    return WebSocketTicketService(
        store=RedisWebSocketTicketStore(pools.cache),
        tokens=OpaqueTokenService(),
        clock=clock,
        ttl_seconds=settings.gateway.ticket_ttl_seconds,
    )


WebSocketTicketServiceDep = Annotated[WebSocketTicketService, Depends(get_websocket_ticket_service)]


def get_session_service(
    session: DbSessionDep,
    tokens: RefreshTokenServiceDep,
    restrictions: AccountRestrictionGateDep,
    clock: ClockDep,
    settings: SettingsDep,
) -> SessionService:
    """Refresh sessions (A64-011.4).

    Reached by four routes: `login` starts a session, `refresh` rotates
    one, `logout` revokes one, `logout-all` revokes every session for an
    account. A64-011.8's password reset reaches it too, through
    `PasswordResetService`, which is why that factory takes the *resolved*
    `SessionServiceDep` rather than building a second instance — see it
    below on why a second one would revoke into a transaction nobody
    commits.

    The unit of work wraps the *same* session the repository holds;
    otherwise the service would commit a transaction the repository never
    wrote to, and a sign-in would be silently lost on teardown.
    """
    return SessionService(
        sessions=SqlAlchemySessionRepository(session),
        tokens=tokens,
        restrictions=restrictions,
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
        settings=settings.session,
    )


SessionServiceDep = Annotated[SessionService, Depends(get_session_service)]


def get_user_profile_reader(session: DbSessionDep, clock: ClockDep) -> UserProfileReader:
    """`users`' side of `GET /auth/me` and `POST /auth/refresh`, behind its
    third published port.

    Assembled separately from the other two `users` factories for the
    reason they are separate from each other: reading a profile, creating
    an account and reading a password hash are three capabilities, and a
    single factory returning something with all three would undo the
    split the ports exist to make.
    """
    users = UserService(
        users=SqlAlchemyUserRepository(session),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )
    return UserProfileService(users)


UserProfileReaderDep = Annotated[UserProfileReader, Depends(get_user_profile_reader)]


def get_email_verifier(session: DbSessionDep, clock: ClockDep) -> EmailVerifier:
    """`users`' side of email verification, behind its fourth published
    port — the one write `auth` may make to an account."""
    users = UserService(
        users=SqlAlchemyUserRepository(session),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )
    return EmailVerificationWriter(users)


EmailVerifierDep = Annotated[EmailVerifier, Depends(get_email_verifier)]


def get_email_provider(settings: SettingsDep) -> EmailProvider:
    """This request's transport — A64-021.5 moved the choice.

    Delegates to `platform.email.build_email_provider`, which is the one
    place the provider is selected. `notifications`' email worker holds the
    same transport, and two selection points would be two sender identities
    and two places a credential is configured.

    **Verification and reset mail therefore go through Resend** in any
    process configured with `RESEND_API_KEY`, and this file did not have to
    know that. Nothing about the two services changed: not the token
    semantics, not the expiry, not the responses — only which class the port
    resolves to.

    The startup guard is unchanged and still lands here for HTTP callers:
    without a credential this returns `ConsoleEmailProvider`, which refuses
    to construct in a production-like environment, so a deployed tier without
    a transport fails visibly at boot rather than by silently sending nobody
    anything (DI-06).
    """
    return build_email_provider(settings.environment, settings.email)


EmailProviderDep = Annotated[EmailProvider, Depends(get_email_provider)]


def get_email_verification_service(
    session: DbSessionDep,
    profiles: UserProfileReaderDep,
    verifier: EmailVerifierDep,
    email: EmailProviderDep,
    clock: ClockDep,
    settings: SettingsDep,
    events: EventPublisherDep,
) -> EmailVerificationService:
    """Email verification (A64-011.6).

    The unit of work wraps the *same* session the repository holds —
    otherwise the service would commit a transaction the repository never
    wrote to, and an issued token would be silently lost on teardown.

    The `users`-side collaborators arrive as already-resolved ports rather
    than being assembled inline, so this factory cannot accidentally build
    a second `UserService` on a different session.
    """
    return EmailVerificationService(
        tokens=SqlAlchemyVerificationTokenRepository(session),
        token_factory=OpaqueTokenService(settings.email.token_entropy_bytes),
        profiles=profiles,
        verifier=verifier,
        email=email,
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
        settings=settings.email,
        events=events,
    )


EmailVerificationServiceDep = Annotated[
    EmailVerificationService, Depends(get_email_verification_service)
]


def get_password_resetter(session: DbSessionDep, clock: ClockDep) -> PasswordResetter:
    """`users`' side of password reset, behind its fifth published port.

    Assembled separately from `get_user_credential_store`, which also
    writes passwords, for the reason the ports are separate: that one can
    *read* a hash and this one cannot, and a single factory returning
    something with both capabilities would hand the reset flow the ability
    to read the credential it is replacing.
    """
    users = UserService(
        users=SqlAlchemyUserRepository(session),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )
    return PasswordResetWriter(users)


PasswordResetterDep = Annotated[PasswordResetter, Depends(get_password_resetter)]


def get_password_reset_service(
    session: DbSessionDep,
    profiles: UserProfileReaderDep,
    resetter: PasswordResetterDep,
    password_hasher: PasswordHasherDep,
    sessions: SessionServiceDep,
    email: EmailProviderDep,
    clock: ClockDep,
    settings: SettingsDep,
) -> PasswordResetService:
    """Password reset (A64-011.7).

    The unit of work wraps the *same* session the repository holds —
    otherwise the service would commit a transaction the repository never
    wrote to, and an issued token would be silently lost on teardown.

    Every collaborator that already exists arrives already resolved rather
    than being assembled inline: `SessionServiceDep` in particular, so that
    the sessions this flow revokes are read and written through the same
    session as the token it consumes. Building a second `SessionService`
    here on a different unit of work is exactly the mistake that would make
    "the password changed but the sessions survived" a production-only bug.

    `OpaqueTokenService` is constructed with the reset-specific entropy
    setting rather than the verification one, so the two can be tuned
    independently — see `EmailSettings`.
    """
    return PasswordResetService(
        tokens=SqlAlchemyPasswordResetTokenRepository(session),
        token_factory=OpaqueTokenService(settings.email.password_reset_token_entropy_bytes),
        profiles=profiles,
        resetter=resetter,
        password_hasher=password_hasher,
        sessions=sessions,
        email=email,
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
        settings=settings.email,
    )


PasswordResetServiceDep = Annotated[PasswordResetService, Depends(get_password_reset_service)]


# --- Google sign-in — A64-031.E1 ---------------------------------------------

#: The route Google sends the browser back to, under this API's prefixes.
#: `google_router.py` declares the same path; the redirect URI is built from
#: this so the two cannot disagree, and Google compares it byte for byte.
GOOGLE_CALLBACK_PATH = f"{API_PREFIX}{API_V1_PREFIX}/auth/browser/google/callback"


def google_redirect_uri(settings: Settings) -> str:
    """`PUBLIC_APP_URL` + the callback route — what the Google console must
    list. Derived rather than configured; see `GoogleSignInSettings`."""
    return f"{settings.app.public_url}{GOOGLE_CALLBACK_PATH}"


@lru_cache(maxsize=1)
def _google_signing_keys() -> GoogleSigningKeys:
    """One key cache per process — public keys, identical on every replica,
    so sharing them across requests is what the cache is for."""
    return GoogleSigningKeys()


def get_external_identity_accounts(
    session: DbSessionDep, clock: ClockDep, events: EventPublisherDep
) -> ExternalIdentityAccounts:
    """`users`' side of the Google graph — the same assembly as
    `get_user_account_creator`, with the identity repository on the same
    session, so an account and its link commit together."""
    users = UserService(
        users=SqlAlchemyUserRepository(session),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
        events=events,
        external_identities=SqlAlchemyExternalIdentityRepository(session),
    )
    return ExternalIdentityAccountService(users)


ExternalIdentityAccountsDep = Annotated[
    ExternalIdentityAccounts, Depends(get_external_identity_accounts)
]


def get_google_sign_in_service(
    settings: SettingsDep,
    pools: RedisPoolsDep,
    accounts: ExternalIdentityAccountsDep,
    credentials: UserCredentialStoreDep,
    authentication: AuthenticationServiceDep,
    password_hasher: PasswordHasherDep,
) -> GoogleSignInService | None:
    """The Google flow, or `None` on a tier with no Google client.

    `None` rather than an exception, because "not configured" is a valid
    state with a defined answer on every route — the start route sends the
    browser back with `unavailable` — and an exception raised while
    resolving a dependency would turn that into a 500.
    """
    google = settings.google_sign_in
    if not google.is_configured:
        return None
    return GoogleSignInService(
        verifier=GoogleOidcVerifier(
            google, redirect_uri=google_redirect_uri(settings), keys=_google_signing_keys()
        ),
        # `cache`, beside WebSocket tickets — see `RedisExternalSignInStore`.
        store=RedisExternalSignInStore(pools.cache),
        accounts=accounts,
        credentials=credentials,
        authentication=authentication,
        password_hasher=password_hasher,
        tokens=OpaqueTokenService(),
        state_ttl_seconds=google.state_ttl_seconds,
        pending_ttl_seconds=google.pending_ttl_seconds,
    )


GoogleSignInServiceDep = Annotated[GoogleSignInService | None, Depends(get_google_sign_in_service)]

__all__ = [
    "AccessTokenServiceDep",
    "AuthenticationServiceDep",
    "EmailProviderDep",
    "EmailVerificationServiceDep",
    "EmailVerifierDep",
    "ExternalIdentityAccountsDep",
    "GOOGLE_CALLBACK_PATH",
    "GoogleSignInServiceDep",
    "Clock",
    "CurrentUser",
    "VerifiedUser",
    "require_verified_email",
    "OptionalCurrentUser",
    "RequireAuthentication",
    "PasswordHasherDep",
    "PasswordResetServiceDep",
    "PasswordResetterDep",
    "RefreshTokenServiceDep",
    "RegistrationServiceDep",
    "TokenValidatorDep",
    "SessionServiceDep",
    "WebSocketTicketServiceDep",
    "UserAccountCreatorDep",
    "UserCredentialStoreDep",
    "UserProfileReaderDep",
    "get_access_token_service",
    "get_authentication_service",
    "get_email_provider",
    "get_email_verification_service",
    "get_email_verifier",
    "get_external_identity_accounts",
    "get_google_sign_in_service",
    "google_redirect_uri",
    "get_current_user",
    "get_current_user_optional",
    "get_password_hasher",
    "get_password_reset_service",
    "get_password_resetter",
    "get_refresh_token_service",
    "get_registration_service",
    "get_token_validator",
    "get_session_service",
    "get_user_account_creator",
    "get_user_credential_store",
    "get_user_profile_reader",
]
