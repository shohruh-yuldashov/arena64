"""`GoogleSignInService` — what a verified Google identity may reach,
A64-031.E1, ADR-008.

The production stack below the verifier: the real `UserService`,
`UserCredentialService`, `ExternalIdentityAccountService` and
`AuthenticationService`, over in-memory repositories. Only Google itself
(`_FakeVerifier`) and Redis (`_MemoryStore`) are replaced — the verifier's
own checks are `test_google_oidc.py`'s, the store's `test_external_sign_in_store.py`'s.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import TracebackType
from typing import Self
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import SecretStr

from app.config.settings import GoogleSignInSettings, get_settings
from app.core.enums import Locale
from app.modules.auth.application.services import (
    AuthenticationService,
    AwaitingStep,
    GoogleSignInService,
    SignedIn,
)
from app.modules.auth.application.services.opaque_tokens import OpaqueTokenService
from app.modules.auth.domain.exceptions import (
    ExternalSignInExpired,
    GoogleSignInRejected,
    InactiveAccount,
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
from app.modules.auth.presentation.dependencies import get_google_sign_in_service
from app.modules.users.application.services import UserService
from app.modules.users.application.services.external_identity_account_service import (
    ExternalIdentityAccountService,
)
from app.modules.users.application.services.user_credential_service import UserCredentialService
from app.modules.users.domain.entities import User
from app.modules.users.domain.exceptions import UsernameAlreadyExists
from app.modules.users.domain.external_identity import ExternalIdentity, IdentityProvider
from app.modules.users.domain.value_objects import Email, Timezone, Username
from tests.fakes.external_identity_repository import FakeExternalIdentityRepository
from tests.fakes.moderation import UnrestrictedAccounts
from tests.fakes.user_repository import FakeUserRepository

_NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
PASSWORD = "CorrectHorse1!"
SUBJECT = "110169484474386276334"
EXISTING_EMAIL = "taken@gmail.com"


class _Clock:
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


class _StubHasher:
    async def hash(self, plaintext: str) -> str:
        return f"v2:{plaintext}"

    async def verify(self, encoded_hash: str, plaintext: str) -> bool:
        return encoded_hash.split(":", 1)[-1] == plaintext

    async def needs_rehash(self, encoded_hash: str) -> bool:
        return False

    async def dummy_hash(self) -> str:
        return "v2:\x00unguessable"


@dataclass
class _FakeVerifier:
    """Google, reduced to "this code proves this identity"."""

    identity: GoogleIdentity
    seen: list[tuple[str, str, str]] = field(default_factory=list)

    def authorization_url(self, *, state: str, nonce: str, code_challenge: str) -> str:
        return f"https://google.test/auth?state={state}&nonce={nonce}&challenge={code_challenge}"

    async def verify(self, *, code: str, code_verifier: str, nonce: str) -> GoogleIdentity:
        self.seen.append((code, code_verifier, nonce))
        return self.identity


class _MemoryStore:
    def __init__(self) -> None:
        self.started: dict[bytes, StartedSignIn] = {}
        self.pending: dict[bytes, PendingSignIn] = {}

    async def save_started(
        self, digest: bytes, started: StartedSignIn, *, ttl_seconds: int
    ) -> None:
        self.started[digest] = started

    async def take_started(self, digest: bytes) -> StartedSignIn | None:
        return self.started.pop(digest, None)

    async def save_pending(
        self, digest: bytes, pending: PendingSignIn, *, ttl_seconds: int
    ) -> None:
        self.pending[digest] = pending

    async def read_pending(self, digest: bytes) -> PendingSignIn | None:
        return self.pending.get(digest)

    async def discard_pending(self, digest: bytes) -> None:
        self.pending.pop(digest, None)


def _identity(**overrides: object) -> GoogleIdentity:
    values: dict[str, object] = {
        "subject": SUBJECT,
        "email": "newcomer@gmail.com",
        "email_verified": True,
        "name": "New Comer",
    }
    values.update(overrides)
    return GoogleIdentity(**values)  # type: ignore[arg-type]


def _account(email: str = EXISTING_EMAIL, *, username: str = "taken_name") -> User:
    return User.create(
        username=Username(username),
        email=Email(email),
        password_hash=f"v2:{PASSWORD}",
        preferred_language=Locale.EN,
        timezone=Timezone("UTC"),
        created_at=_NOW,
    )


class _World:
    def __init__(self, identity: GoogleIdentity, *users: User) -> None:
        self.repository = FakeUserRepository(list(users))
        self.identities = FakeExternalIdentityRepository()
        self.store = _MemoryStore()
        self.verifier = _FakeVerifier(identity)
        self.tokens = OpaqueTokenService()
        users_service = UserService(
            users=self.repository,
            unit_of_work=_NullUnitOfWork(),
            clock=_Clock(),
            external_identities=self.identities,
        )
        credentials = UserCredentialService(users_service)
        self.service = GoogleSignInService(
            verifier=self.verifier,
            store=self.store,
            accounts=ExternalIdentityAccountService(users_service),
            credentials=credentials,
            authentication=AuthenticationService(
                credentials=credentials,
                password_hasher=_StubHasher(),
                restrictions=UnrestrictedAccounts(),
                clock=_Clock(),
            ),
            password_hasher=_StubHasher(),
            tokens=self.tokens,
            state_ttl_seconds=600,
            pending_ttl_seconds=900,
        )

    async def round_trip(self, next_path: str | None = "/play") -> SignedIn | AwaitingStep:
        begun = await self.service.begin(next_path)
        state = parse_qs(urlsplit(begun.authorization_url).query)["state"][0]
        return await self.service.complete(state=state, code="4/code", binding=begun.binding)

    async def link_directly(self, user: User) -> None:
        self.identities.links[ExternalIdentity(IdentityProvider.GOOGLE, SUBJECT)] = user.id


def _ticket(outcome: SignedIn | AwaitingStep) -> str:
    assert isinstance(outcome, AwaitingStep)
    return outcome.ticket


class TestStart:
    async def test_sends_only_state_nonce_and_the_challenge(self) -> None:
        world = _World(_identity())

        begun = await world.service.begin("/play")

        query = {k: v[0] for k, v in parse_qs(urlsplit(begun.authorization_url).query).items()}
        (started,) = world.store.started.values()
        assert world.store.started == {world.tokens.hash(query["state"]): started}
        assert query["nonce"] == started.nonce
        assert query["challenge"] == pkce_challenge(started.code_verifier)
        assert started.code_verifier not in begun.authorization_url

    async def test_binds_the_state_to_the_browser_that_started_it(self) -> None:
        world = _World(_identity())

        begun = await world.service.begin(None)

        (started,) = world.store.started.values()
        assert world.tokens.verify(begun.binding, started.binding_digest)

    async def test_a_foreign_next_path_lands_at_home(self) -> None:
        world = _World(_identity())

        await world.service.begin("https://evil.example/")

        (started,) = world.store.started.values()
        assert started.next_path == "/"


class TestTheCallbackItself:
    async def test_an_unknown_state_is_expired(self) -> None:
        world = _World(_identity())

        with pytest.raises(GoogleSignInRejected) as raised:
            await world.service.complete(state="never-issued", code="c", binding="b")

        assert raised.value.failure is SignInFailure.EXPIRED

    async def test_a_state_works_once(self) -> None:
        """Replay: the second callback with one `state` finds nothing."""
        world = _World(_identity())
        begun = await world.service.begin(None)
        state = parse_qs(urlsplit(begun.authorization_url).query)["state"][0]
        await world.service.complete(state=state, code="c", binding=begun.binding)

        with pytest.raises(GoogleSignInRejected) as raised:
            await world.service.complete(state=state, code="c", binding=begun.binding)

        assert raised.value.failure is SignInFailure.EXPIRED

    @pytest.mark.parametrize("binding", [None, "another-browsers-binding"])
    async def test_a_state_from_another_browser_is_refused_and_spent(
        self, binding: str | None
    ) -> None:
        """Login CSRF: a callback URL started in the attacker's browser and
        opened in the victim's carries no matching binding cookie."""
        world = _World(_identity())
        begun = await world.service.begin(None)
        state = parse_qs(urlsplit(begun.authorization_url).query)["state"][0]

        with pytest.raises(GoogleSignInRejected) as raised:
            await world.service.complete(state=state, code="c", binding=binding)

        assert raised.value.failure is SignInFailure.EXPIRED
        assert world.store.started == {}
        assert world.verifier.seen == []

    async def test_the_exchange_uses_the_verifier_and_nonce_from_start(self) -> None:
        world = _World(_identity())

        await world.round_trip()

        (started_nonce,) = [nonce for _, _, nonce in world.verifier.seen]
        assert len(world.verifier.seen[0][1]) >= 43  # RFC 7636 §4.1
        assert started_nonce


class TestAnAlreadyLinkedIdentity:
    async def test_signs_in_to_its_account_and_keeps_the_destination(self) -> None:
        user = _account()
        world = _World(_identity(), user)
        await world.link_directly(user)

        outcome = await world.round_trip("/play")

        assert isinstance(outcome, SignedIn)
        assert (outcome.account.id, outcome.next_path) == (user.id, "/play")

    async def test_is_found_by_subject_even_after_the_address_changed(self) -> None:
        user = _account()
        world = _World(_identity(email="renamed@gmail.com"), user)
        await world.link_directly(user)

        outcome = await world.round_trip()

        assert isinstance(outcome, SignedIn)
        assert outcome.account.id == user.id

    async def test_does_not_need_a_verified_address(self) -> None:
        user = _account()
        world = _World(_identity(email=None, email_verified=False), user)
        await world.link_directly(user)

        assert isinstance(await world.round_trip(), SignedIn)

    async def test_a_deactivated_account_is_refused_as_a_password_sign_in_would_be(self) -> None:
        user = _account()
        user.is_active = False
        world = _World(_identity(), user)
        await world.link_directly(user)

        with pytest.raises(InactiveAccount):
            await world.round_trip()


class TestAnUnlinkedIdentity:
    @pytest.mark.parametrize(
        "identity",
        [
            pytest.param(_identity(email_verified=False), id="unverified"),
            pytest.param(_identity(email=None, email_verified=False), id="no address"),
        ],
    )
    async def test_without_a_verified_address_reaches_nothing(
        self, identity: GoogleIdentity
    ) -> None:
        world = _World(identity, _account(email="newcomer@gmail.com"))

        with pytest.raises(GoogleSignInRejected) as raised:
            await world.round_trip()

        assert raised.value.failure is SignInFailure.EMAIL_UNVERIFIED
        assert world.store.pending == {}

    async def test_with_a_new_address_waits_for_a_username(self) -> None:
        world = _World(_identity())

        outcome = await world.round_trip()

        assert isinstance(outcome, AwaitingStep)
        assert outcome.purpose is PendingPurpose.CREATE_ACCOUNT
        assert world.repository._users == {}  # nothing created yet

    async def test_whose_address_an_account_uses_is_never_linked_on_the_address(self) -> None:
        """ADR-008's central rule. The callback must stop and ask for the
        password — not sign in, and not link."""
        user = _account()
        world = _World(_identity(email=EXISTING_EMAIL), user)

        outcome = await world.round_trip()

        assert isinstance(outcome, AwaitingStep)
        assert outcome.purpose is PendingPurpose.LINK_ACCOUNT
        assert world.identities.links == {}

    async def test_matches_the_address_case_insensitively(self) -> None:
        user = _account()
        world = _World(_identity(email=EXISTING_EMAIL.upper()), user)

        outcome = await world.round_trip()

        assert isinstance(outcome, AwaitingStep)
        assert outcome.purpose is PendingPurpose.LINK_ACCOUNT


class TestCreatingTheAccount:
    async def test_creates_a_verified_account_linked_to_the_subject(self) -> None:
        world = _World(_identity())
        ticket = _ticket(await world.round_trip())

        created = await world.service.create_account(
            ticket, username="New_Comer", preferred_language="uz", timezone="Asia/Tashkent"
        )

        assert (created.username, created.email, created.is_verified) == (
            "New_Comer",
            "newcomer@gmail.com",
            True,
        )
        assert world.identities.links == {
            ExternalIdentity(IdentityProvider.GOOGLE, SUBJECT): created.id
        }

    async def test_the_account_has_no_password_anybody_knows(self) -> None:
        world = _World(_identity())
        ticket = _ticket(await world.round_trip())
        created = await world.service.create_account(
            ticket, username="new_comer", preferred_language="en", timezone="UTC"
        )

        stored = world.repository._users[created.id].password_hash

        assert stored.startswith("v2:")
        assert len(stored) > 40
        assert ticket not in stored

    async def test_the_next_google_sign_in_reaches_it(self) -> None:
        world = _World(_identity())
        ticket = _ticket(await world.round_trip())
        created = await world.service.create_account(
            ticket, username="new_comer", preferred_language="en", timezone="UTC"
        )

        outcome = await world.round_trip()

        assert isinstance(outcome, SignedIn)
        assert outcome.account.id == created.id

    async def test_a_ticket_creates_one_account(self) -> None:
        world = _World(_identity())
        ticket = _ticket(await world.round_trip())
        await world.service.create_account(
            ticket, username="new_comer", preferred_language="en", timezone="UTC"
        )

        with pytest.raises(ExternalSignInExpired):
            await world.service.create_account(
                ticket, username="second_one", preferred_language="en", timezone="UTC"
            )

    async def test_a_taken_username_keeps_the_ticket_for_another_try(self) -> None:
        world = _World(_identity(), _account(username="wanted_name"))
        ticket = _ticket(await world.round_trip())

        with pytest.raises(UsernameAlreadyExists):
            await world.service.create_account(
                ticket, username="WANTED_NAME", preferred_language="en", timezone="UTC"
            )
        created = await world.service.create_account(
            ticket, username="another_name", preferred_language="en", timezone="UTC"
        )

        assert created.username == "another_name"

    async def test_a_link_ticket_cannot_create_an_account(self) -> None:
        world = _World(_identity(email=EXISTING_EMAIL), _account())
        ticket = _ticket(await world.round_trip())

        with pytest.raises(ExternalSignInExpired):
            await world.service.create_account(
                ticket, username="sneaky_one", preferred_language="en", timezone="UTC"
            )

    async def test_no_ticket_is_expired(self) -> None:
        world = _World(_identity())

        with pytest.raises(ExternalSignInExpired):
            await world.service.create_account(
                None, username="new_comer", preferred_language="en", timezone="UTC"
            )


class TestLinkingAnExistingAccount:
    async def test_the_right_password_links_and_returns_the_account(self) -> None:
        user = _account()
        world = _World(_identity(email=EXISTING_EMAIL), user)
        ticket = _ticket(await world.round_trip())

        linked = await world.service.link_account(ticket, SecretStr(PASSWORD))

        assert linked.id == user.id
        assert world.identities.links == {
            ExternalIdentity(IdentityProvider.GOOGLE, SUBJECT): user.id
        }
        assert isinstance(await world.round_trip(), SignedIn)

    async def test_a_wrong_password_links_nothing_and_keeps_the_ticket(self) -> None:
        world = _World(_identity(email=EXISTING_EMAIL), _account())
        ticket = _ticket(await world.round_trip())

        with pytest.raises(InvalidCredentials):
            await world.service.link_account(ticket, SecretStr("WrongHorse9?"))

        assert world.identities.links == {}
        assert await world.service.pending(ticket) is not None

    async def test_an_address_that_moved_to_another_account_links_nothing(self) -> None:
        """The account is fixed when Google returns. If its address now
        belongs to somebody else, their password must not link Google to
        *their* account."""
        original = _account()
        world = _World(_identity(email=EXISTING_EMAIL), original)
        ticket = _ticket(await world.round_trip())
        world.repository._users.pop(original.id)
        other = _account(username="someone_else")
        world.repository._users[other.id] = other

        with pytest.raises(InvalidCredentials):
            await world.service.link_account(ticket, SecretStr(PASSWORD))

        assert world.identities.links == {}

    async def test_a_create_ticket_cannot_link(self) -> None:
        world = _World(_identity())
        ticket = _ticket(await world.round_trip())

        with pytest.raises(ExternalSignInExpired):
            await world.service.link_account(ticket, SecretStr(PASSWORD))


class TestThePath:
    @pytest.mark.parametrize(
        ("candidate", "expected"),
        [
            ("/play", "/play"),
            ("/tournaments/42?tab=bracket", "/tournaments/42?tab=bracket"),
            (None, "/"),
            ("", "/"),
            ("play", "/"),
            ("//evil.example", "/"),
            ("/\\evil.example", "/"),
            ("@evil.example", "/"),
            ("https://evil.example", "/"),
            ("/play\nSet-Cookie: x", "/"),
            ("/" + "a" * 600, "/"),
        ],
    )
    def test_only_an_in_app_path_survives(self, candidate: str | None, expected: str) -> None:
        assert safe_next_path(candidate) == expected

    def test_the_challenge_is_rfc_7636_s256(self) -> None:
        """RFC 7636 Appendix B's worked example."""
        assert (
            pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")
            == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
        )


class TestTheDependency:
    def test_resolves_to_nothing_without_a_client_pair(self) -> None:
        """An unconfigured tier answers every Google route with
        "unavailable" rather than failing to build the flow."""
        settings = get_settings().model_copy(update={"google_sign_in": GoogleSignInSettings()})

        resolved = get_google_sign_in_service(
            settings,
            pools=None,  # type: ignore[arg-type]
            accounts=None,  # type: ignore[arg-type]
            credentials=None,  # type: ignore[arg-type]
            authentication=None,  # type: ignore[arg-type]
            password_hasher=None,  # type: ignore[arg-type]
        )

        assert resolved is None
