"""`GoogleOidcVerifier` — Google's side of "Continue with Google",
A64-031.E1.

OpenID Connect's authorization-code flow for a confidential web client,
with PKCE. No SDK: three fixed endpoints, one form POST and one JWT
verification, on two libraries this service already audited — `httpx`
(A64-021.5) and PyJWT with `cryptography` (A64-011.3, A64-021.6).

## What `verify` checks, and why each

    exchange      the code, the PKCE verifier and the client secret go
                  to Google's token endpoint over TLS. A stolen code is
                  useless without the verifier, which never left the
                  server
    signature     RS256 against Google's published keys, by `kid`. The
                  algorithm is pinned, so a token claiming `none` or
                  `HS256` is refused rather than "verified"
    iss           exactly one of Google's two issuer spellings
    aud / azp     this client, and only this client — a token minted
                  for another application is not ours to accept
    exp / iat     required, with a minute's leeway for clock skew
    nonce         the value this sign-in minted, compared in constant
                  time. Binds the token to *this* attempt, so one
                  replayed from another flow is refused
    sub           present; it is the identity

The token arrived directly from Google in the back channel, which OIDC
Core §3.1.3.7 says may be trusted without the signature check. It is
checked anyway: the check is cheap, and "trusted because of where it came
from" is a property the next refactor can silently remove.

## Never logged

The code, the verifier, the secret, the ID token and the access token.
A failure logs the stage and, for a refused exchange, Google's `error`
string — which names a reason, never a credential.
"""

import asyncio
import hmac
import logging
import re
import time
from collections.abc import Callable, Mapping
from typing import Any, Final, NoReturn
from urllib.parse import urlencode

import httpx
import jwt

from app.config.settings import GoogleSignInSettings
from app.modules.auth.domain.exceptions import GoogleSignInRejected
from app.modules.auth.domain.google_sign_in import GoogleIdentity, SignInFailure

logger = logging.getLogger(__name__)

AUTHORIZATION_ENDPOINT: Final = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT: Final = "https://oauth2.googleapis.com/token"
JWKS_URI: Final = "https://www.googleapis.com/oauth2/v3/certs"
#: Google issues under both spellings (its discovery document names the
#: first). A `frozenset`, not a string: PyJWT treats a container as a set
#: of exact matches, where a string would be a single exact match too —
#: but a tuple of one string mistyped as a bare string would become a
#: substring test in a lesser library, and this keeps the intent explicit.
ISSUERS: Final = frozenset({"https://accounts.google.com", "accounts.google.com"})
SCOPES: Final = "openid email profile"
#: Clock skew allowed on `exp`, `iat` and `nbf`.
LEEWAY_SECONDS: Final = 60


class GoogleSigningKeys:
    """Google's public signing keys, cached per process.

    Per process is right here, unlike the sign-in state: these are public
    keys every replica fetches identically, so a cache miss costs one GET,
    never a wrong answer. Kept for the `max-age` Google sends (clamped to
    between five minutes and a day), and re-fetched early — at most once a
    minute — when a token names a `kid` this process has not seen, which is
    what a key rotation looks like from here.
    """

    _MIN_TTL: Final = 5 * 60
    _MAX_TTL: Final = 24 * 60 * 60
    _REFRESH_COOLDOWN: Final = 60

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._keys: dict[str, jwt.PyJWK] = {}
        self._expires_at = 0.0
        self._last_fetch = float("-inf")
        self._lock = asyncio.Lock()
        # Injected so a test can expire the cache without sleeping.
        self._now = clock

    async def key_for(self, kid: str, client: httpx.AsyncClient) -> jwt.PyJWK | None:
        async with self._lock:
            now = self._now()
            stale = now >= self._expires_at
            unknown = kid not in self._keys
            if stale or (unknown and now - self._last_fetch >= self._REFRESH_COOLDOWN):
                await self._fetch(client, now)
            return self._keys.get(kid)

    async def _fetch(self, client: httpx.AsyncClient, now: float) -> None:
        self._last_fetch = now
        response = await client.get(JWKS_URI)
        response.raise_for_status()
        keys: dict[str, jwt.PyJWK] = {}
        for entry in response.json().get("keys", []):
            kid = entry.get("kid")
            if isinstance(kid, str) and entry.get("kty") == "RSA":
                keys[kid] = jwt.PyJWK(entry, algorithm="RS256")
        self._keys = keys
        self._expires_at = now + self._ttl_of(response.headers.get("cache-control", ""))

    @classmethod
    def _ttl_of(cls, cache_control: str) -> float:
        match = re.search(r"max-age=(\d+)", cache_control)
        seconds = int(match.group(1)) if match else 60 * 60
        return float(min(max(seconds, cls._MIN_TTL), cls._MAX_TTL))


class GoogleOidcVerifier:
    """`GoogleIdentityVerifier` against Google's production endpoints."""

    def __init__(
        self,
        settings: GoogleSignInSettings,
        *,
        redirect_uri: str,
        keys: GoogleSigningKeys,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        if settings.client_id is None or settings.client_secret is None:
            # The route checks `is_configured` before building this, so
            # reaching here is a wiring defect, not a player's problem.
            raise RuntimeError("GoogleOidcVerifier needs GOOGLE_OAUTH_CLIENT_ID and _SECRET")
        self._client_id = settings.client_id
        self._client_secret = settings.client_secret
        self._redirect_uri = redirect_uri
        self._keys = keys
        # A client per sign-in, opened and closed around it: one exchange
        # per sign-in is too rare to be worth a pool that would otherwise
        # outlive every request that built one.
        timeout = settings.http_timeout_seconds
        self._client_factory = client_factory or (lambda: httpx.AsyncClient(timeout=timeout))

    def authorization_url(self, *, state: str, nonce: str, code_challenge: str) -> str:
        query = urlencode(
            {
                "client_id": self._client_id,
                "redirect_uri": self._redirect_uri,
                "response_type": "code",
                "scope": SCOPES,
                "state": state,
                "nonce": nonce,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                # Lets somebody signed in to several Google accounts pick
                # one, instead of being signed in as whichever was last.
                "prompt": "select_account",
            }
        )
        return f"{AUTHORIZATION_ENDPOINT}?{query}"

    async def verify(self, *, code: str, code_verifier: str, nonce: str) -> GoogleIdentity:
        async with self._client_factory() as client:
            id_token = await self._exchange(client, code, code_verifier)
            claims = await self._verified_claims(client, id_token)

        presented = claims.get("nonce")
        if not isinstance(presented, str) or not hmac.compare_digest(presented, nonce):
            _reject("nonce")

        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            _reject("subject")

        email = claims.get("email")
        return GoogleIdentity(
            subject=subject,
            email=email if isinstance(email, str) and email else None,
            email_verified=_is_true(claims.get("email_verified")),
            name=claims.get("name") if isinstance(claims.get("name"), str) else None,
        )

    async def _exchange(self, client: httpx.AsyncClient, code: str, code_verifier: str) -> str:
        try:
            response = await client.post(
                TOKEN_ENDPOINT,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "code_verifier": code_verifier,
                    "redirect_uri": self._redirect_uri,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret.get_secret_value(),
                },
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as error:
            logger.warning(
                "google_token_exchange_unreachable", extra={"error": type(error).__name__}
            )
            raise GoogleSignInRejected(
                SignInFailure.FAILED, "token endpoint unreachable"
            ) from error

        if response.status_code != httpx.codes.OK:
            # `error` is an OAuth reason code such as `invalid_grant` — safe
            # to log, and the only part of the body that is.
            logger.warning(
                "google_token_exchange_refused",
                extra={"status": response.status_code, "reason": _oauth_error(response)},
            )
            _reject("exchange")

        token = _json(response).get("id_token")
        if not isinstance(token, str):
            _reject("no id_token")
        return token

    async def _verified_claims(self, client: httpx.AsyncClient, id_token: str) -> Mapping[str, Any]:
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError:
            _reject("header")
        kid = header.get("kid")
        if header.get("alg") != "RS256" or not isinstance(kid, str):
            _reject("algorithm")

        try:
            key = await self._keys.key_for(kid, client)
        except httpx.HTTPError as error:
            logger.warning("google_keys_unreachable", extra={"error": type(error).__name__})
            raise GoogleSignInRejected(SignInFailure.FAILED, "signing keys unreachable") from error
        if key is None:
            _reject("unknown key")

        try:
            claims: dict[str, Any] = jwt.decode(
                id_token,
                key=key,
                algorithms=["RS256"],
                audience=self._client_id,
                issuer=ISSUERS,
                leeway=LEEWAY_SECONDS,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as error:
            logger.warning("google_id_token_rejected", extra={"error": type(error).__name__})
            raise GoogleSignInRejected(SignInFailure.FAILED, "id token rejected") from error

        # OIDC Core §3.1.3.7 rule 4-5: with several audiences, the
        # authorised party must be us.
        audience = claims.get("aud")
        several = isinstance(audience, list) and len(audience) > 1
        if several and claims.get("azp") != self._client_id:
            _reject("authorised party")
        return claims


def _reject(stage: str) -> NoReturn:
    logger.warning("google_sign_in_unverified", extra={"stage": stage})
    raise GoogleSignInRejected(SignInFailure.FAILED, f"google identity failed at {stage}")


def _is_true(value: object) -> bool:
    """`email_verified` is a boolean in Google's tokens, and was a string
    in some older ones; both are read, nothing else counts as true."""
    return value is True or value == "true"


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _oauth_error(response: httpx.Response) -> str | None:
    error = _json(response).get("error")
    return error if isinstance(error, str) else None
