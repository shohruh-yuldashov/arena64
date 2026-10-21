"""`GoogleOidcVerifier` — every check an ID token must pass, A64-031.E1.

No network. Google's three endpoints are an `httpx.MockTransport`, and the
ID tokens are signed here with an RSA key generated for the test, published
through the mocked key endpoint exactly as Google publishes its own. So the
signature, issuer, audience, expiry and nonce checks all run for real, and
nothing depends on Google being reachable.
"""

import base64
import json
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from pydantic import SecretStr

from app.config.settings import GoogleSignInSettings
from app.modules.auth.domain.exceptions import GoogleSignInRejected
from app.modules.auth.domain.google_sign_in import SignInFailure
from app.modules.auth.infrastructure.google_oidc import (
    AUTHORIZATION_ENDPOINT,
    JWKS_URI,
    TOKEN_ENDPOINT,
    GoogleOidcVerifier,
    GoogleSigningKeys,
)

CLIENT_ID = "arena64-test.apps.googleusercontent.com"
CLIENT_SECRET = "test-client-secret-not-real"
REDIRECT_URI = "https://arena64.example/api/v1/auth/browser/google/callback"
NONCE = "the-nonce-this-sign-in-minted"
KID = "test-key-1"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk(key: rsa.RSAPrivateKey, kid: str) -> dict[str, Any]:
    public = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    return {**public, "kid": kid, "alg": "RS256", "use": "sig"}


def _claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "azp": CLIENT_ID,
        "sub": "110169484474386276334",
        "email": "player@gmail.com",
        "email_verified": True,
        "name": "Player One",
        "nonce": NONCE,
        "iat": now,
        "exp": now + 3600,
    }
    claims.update(overrides)
    return {key: value for key, value in claims.items() if value is not None}


def _segment(value: dict[str, Any]) -> str:
    raw = json.dumps(value).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _token(claims: dict[str, Any], *, key: rsa.RSAPrivateKey = _KEY, kid: str = KID) -> str:
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


class _Google:
    """The two endpoints `verify` calls, recording what they were sent."""

    def __init__(self, id_token: str | None, *, token_status: int = 200) -> None:
        self.id_token = id_token
        self.token_status = token_status
        self.token_requests: list[dict[str, list[str]]] = []
        self.key_fetches = 0
        self.published = [_jwk(_KEY, KID)]

    def handler(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_ENDPOINT:
            self.token_requests.append(parse_qs(request.content.decode()))
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_grant"})
            body = {"access_token": "ya29.unused"}
            if self.id_token is not None:
                body["id_token"] = self.id_token
            return httpx.Response(200, json=body)
        if str(request.url) == JWKS_URI:
            self.key_fetches += 1
            return httpx.Response(
                200, json={"keys": self.published}, headers={"cache-control": "max-age=3600"}
            )
        return httpx.Response(404)

    def factory(self) -> Callable[[], httpx.AsyncClient]:
        return lambda: httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def _settings() -> GoogleSignInSettings:
    return GoogleSignInSettings(client_id=CLIENT_ID, client_secret=SecretStr(CLIENT_SECRET))


def _verifier(google: _Google, keys: GoogleSigningKeys | None = None) -> GoogleOidcVerifier:
    return GoogleOidcVerifier(
        _settings(),
        redirect_uri=REDIRECT_URI,
        keys=keys or GoogleSigningKeys(),
        client_factory=google.factory(),
    )


async def _verify(google: _Google, *, nonce: str = NONCE) -> Any:
    return await _verifier(google).verify(code="4/0Acode", code_verifier="v" * 43, nonce=nonce)


async def _rejected(google: _Google, *, nonce: str = NONCE) -> SignInFailure:
    with pytest.raises(GoogleSignInRejected) as raised:
        await _verify(google, nonce=nonce)
    return raised.value.failure


class TestAuthorizationUrl:
    def test_asks_google_for_a_code_with_pkce_state_and_nonce(self) -> None:
        url = _verifier(_Google(None)).authorization_url(
            state="the-state", nonce=NONCE, code_challenge="the-challenge"
        )

        parts = urlsplit(url)
        query = {key: values[0] for key, values in parse_qs(parts.query).items()}
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == AUTHORIZATION_ENDPOINT
        assert query == {
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": "openid email profile",
            "state": "the-state",
            "nonce": NONCE,
            "code_challenge": "the-challenge",
            "code_challenge_method": "S256",
            "prompt": "select_account",
        }

    def test_never_carries_the_client_secret(self) -> None:
        url = _verifier(_Google(None)).authorization_url(state="s", nonce="n", code_challenge="c")

        assert CLIENT_SECRET not in url


class TestAValidToken:
    async def test_yields_the_identity_it_asserts(self) -> None:
        identity = await _verify(_Google(_token(_claims())))

        assert (identity.subject, identity.email, identity.email_verified, identity.name) == (
            "110169484474386276334",
            "player@gmail.com",
            True,
            "Player One",
        )

    async def test_the_exchange_presents_the_code_the_verifier_and_the_secret(self) -> None:
        google = _Google(_token(_claims()))

        await _verify(google)

        sent = {key: values[0] for key, values in google.token_requests[0].items()}
        assert sent == {
            "grant_type": "authorization_code",
            "code": "4/0Acode",
            "code_verifier": "v" * 43,
            "redirect_uri": REDIRECT_URI,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        }

    async def test_accepts_googles_other_issuer_spelling(self) -> None:
        identity = await _verify(_Google(_token(_claims(iss="accounts.google.com"))))

        assert identity.subject == "110169484474386276334"

    async def test_reads_a_string_email_verified(self) -> None:
        identity = await _verify(_Google(_token(_claims(email_verified="true"))))

        assert identity.email_verified is True

    @pytest.mark.parametrize("value", [False, "false", None, 1])
    async def test_anything_else_is_not_verified(self, value: object) -> None:
        identity = await _verify(_Google(_token(_claims(email_verified=value))))

        assert identity.email_verified is False


class TestARejectedToken:
    async def test_signed_by_a_key_google_did_not_publish(self) -> None:
        assert await _rejected(_Google(_token(_claims(), key=_OTHER_KEY))) is SignInFailure.FAILED

    async def test_naming_a_key_google_does_not_have(self) -> None:
        assert await _rejected(_Google(_token(_claims(), kid="unknown"))) is SignInFailure.FAILED

    async def test_for_another_application(self) -> None:
        google = _Google(_token(_claims(aud="someone-else.apps.googleusercontent.com")))

        assert await _rejected(google) is SignInFailure.FAILED

    async def test_from_another_issuer(self) -> None:
        google = _Google(_token(_claims(iss="https://accounts.google.com.evil.example")))

        assert await _rejected(google) is SignInFailure.FAILED

    async def test_that_has_expired(self) -> None:
        past = int(time.time()) - 7200
        google = _Google(_token(_claims(iat=past, exp=past + 600)))

        assert await _rejected(google) is SignInFailure.FAILED

    @pytest.mark.parametrize("claim", ["exp", "iat", "sub", "aud", "iss"])
    async def test_missing_a_required_claim(self, claim: str) -> None:
        google = _Google(_token(_claims(**{claim: None})))

        assert await _rejected(google) is SignInFailure.FAILED

    async def test_minted_for_another_sign_in(self) -> None:
        """The nonce binds the token to *this* attempt."""
        assert await _rejected(_Google(_token(_claims())), nonce="another") is SignInFailure.FAILED

    async def test_with_no_nonce(self) -> None:
        assert await _rejected(_Google(_token(_claims(nonce=None)))) is SignInFailure.FAILED

    async def test_with_several_audiences_and_another_authorised_party(self) -> None:
        google = _Google(_token(_claims(aud=[CLIENT_ID, "other"], azp="other")))

        assert await _rejected(google) is SignInFailure.FAILED

    async def test_that_is_not_signed_at_all(self) -> None:
        """`alg: none` — the classic downgrade. Refused by the pinned
        algorithm, before any key is looked up."""
        unsigned = ".".join(
            [_segment({"alg": "none", "typ": "JWT", "kid": KID}), _segment(_claims()), ""]
        )

        assert await _rejected(_Google(unsigned)) is SignInFailure.FAILED

    async def test_signed_with_hmac_instead_of_rsa(self) -> None:
        """Algorithm confusion: an HS256 token naming a published key id is
        refused by the pinned algorithm, whatever secret signed it."""
        forged = jwt.encode(
            _claims(),
            key="a-shared-secret-of-thirty-two-bytes!",
            algorithm="HS256",
            headers={"kid": KID},
        )

        assert await _rejected(_Google(forged)) is SignInFailure.FAILED


class TestTheExchange:
    async def test_a_refused_code_is_a_failure(self) -> None:
        assert await _rejected(_Google(None, token_status=400)) is SignInFailure.FAILED

    async def test_a_response_without_an_id_token_is_a_failure(self) -> None:
        assert await _rejected(_Google(None)) is SignInFailure.FAILED

    async def test_an_unreachable_google_is_a_failure(self) -> None:
        def unreachable(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("timed out", request=request)

        verifier = GoogleOidcVerifier(
            _settings(),
            redirect_uri=REDIRECT_URI,
            keys=GoogleSigningKeys(),
            client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(unreachable)),
        )

        with pytest.raises(GoogleSignInRejected) as raised:
            await verifier.verify(code="c", code_verifier="v" * 43, nonce=NONCE)

        assert raised.value.failure is SignInFailure.FAILED

    async def test_nothing_secret_reaches_the_log(self, caplog: pytest.LogCaptureFixture) -> None:
        await _rejected(_Google(None, token_status=400))

        logged = "\n".join(f"{record.getMessage()} {record.__dict__}" for record in caplog.records)
        for secret in (CLIENT_SECRET, "4/0Acode", "v" * 43, NONCE):
            assert secret not in logged


class TestSigningKeys:
    async def test_are_fetched_once_and_reused(self) -> None:
        google = _Google(_token(_claims()))
        keys = GoogleSigningKeys()

        await _verifier(google, keys).verify(code="c", code_verifier="v" * 43, nonce=NONCE)
        await _verifier(google, keys).verify(code="c", code_verifier="v" * 43, nonce=NONCE)

        assert google.key_fetches == 1

    async def test_a_rotated_key_is_fetched_when_a_token_names_it(self) -> None:
        now = [1000.0]
        keys = GoogleSigningKeys(clock=lambda: now[0])
        google = _Google(_token(_claims()))
        await _verifier(google, keys).verify(code="c", code_verifier="v" * 43, nonce=NONCE)

        google.published = [_jwk(_OTHER_KEY, "rotated")]
        google.id_token = _token(_claims(), key=_OTHER_KEY, kid="rotated")
        now[0] += 120
        identity = await _verifier(google, keys).verify(
            code="c", code_verifier="v" * 43, nonce=NONCE
        )

        assert identity.subject == "110169484474386276334"
        assert google.key_fetches == 2

    async def test_unknown_keys_cannot_force_a_fetch_per_request(self) -> None:
        now = [1000.0]
        keys = GoogleSigningKeys(clock=lambda: now[0])
        google = _Google(_token(_claims()))
        await _verifier(google, keys).verify(code="c", code_verifier="v" * 43, nonce=NONCE)

        google.id_token = _token(_claims(), kid="never-published")
        for _ in range(5):
            with pytest.raises(GoogleSignInRejected):
                await _verifier(google, keys).verify(code="c", code_verifier="v" * 43, nonce=NONCE)

        assert google.key_fetches == 1


class TestConfiguration:
    def test_both_or_neither(self) -> None:
        with pytest.raises(ValueError, match="GOOGLE_OAUTH_CLIENT_SECRET"):
            GoogleSignInSettings(client_id=CLIENT_ID)

    def test_empty_values_mean_not_configured(self) -> None:
        # As the environment delivers them: strings, which Compose renders
        # empty for an unset host variable.
        settings = GoogleSignInSettings.model_validate({"client_id": "", "client_secret": "  "})

        assert settings.is_configured is False

    def test_the_secret_never_prints(self) -> None:
        assert CLIENT_SECRET not in repr(_settings())
