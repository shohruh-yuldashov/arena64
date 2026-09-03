"""`S3StorageProvider` — objects in an S3-compatible bucket.

The deployed-tier counterpart to `LocalStorageProvider`, and the class
`app/storage/__init__.py` and `core/storage.py` have both been promising
since A64-012.2: *"adding S3, R2, MinIO or GCS is a new class in this
package plus a branch in one dependency factory."* This is that class, and
`_configure_storage` is that branch. Nothing above `StorageProvider`
changed.

## Why `httpx` and a hand-written signature, and not `boto3`

This repository has answered the same question twice, and both answers are
in `pyproject.toml`.

`resend` was rejected as an email transport because it is **synchronous**
in an async worker whose entire job is I/O, with no per-request timeout to
set; the platform posts one JSON body with `httpx` instead. `pywebpush` was
rejected for the same reason plus its three transitive dependencies, and
RFC 8291's encryption was written by hand over `cryptography` — about
eighty lines pinned to a standard that does not move.

`boto3` is synchronous and very large. `aioboto3` is asynchronous and
brings `aiobotocore` and `botocore` with it. What this provider needs is
four HTTP requests — PUT, DELETE, HEAD, and a URL built by string
composition — against a protocol whose signing scheme has not changed since
2012. `httpx` is already a runtime dependency, `hmac` and `hashlib` are
standard library, and the whole of SigV4 is `_sign` below.

So this file adds **no dependency at all**, which is the same trade the two
decisions above already made.

## Why one client, held for the process

`httpx.AsyncClient` pools connections. Constructing one per request would
open a new TLS session per avatar, which is most of the cost of storing a
30 KB file. It is built with the provider and closed by `aclose`, wired
into the application's lifespan beside the database and Redis pools.

## What is deliberately not here

**No presigned URLs.** `get_public_url` composes a public URL by string
concatenation, exactly as the port requires it to — `core/storage.py` says
plainly that a provider needing signed reads cannot implement that method
as written, and that avatars on this platform are public objects. A private
bucket is a different port, added when something needs one.

**No multipart upload.** Avatars are bounded at 5 MB by
`AvatarSettings`; multipart exists for objects two orders of magnitude
larger and would be code with no caller.

**No retries.** `TransientInfrastructureError` says the caller may retry,
and where that is safe the platform's own outbox and task machinery already
decide the policy. A retry loop hidden inside an adapter is one nobody can
see, bound or turn off.
"""

import datetime as dt
import hashlib
import hmac
import logging
from urllib.parse import quote

import httpx

from app.config.settings import StorageSettings
from app.core.exceptions import TransientInfrastructureError
from app.core.storage import KEY_SEPARATOR, StorageError

logger = logging.getLogger(__name__)

#: The signature version every S3-compatible store speaks.
_ALGORITHM = "AWS4-HMAC-SHA256"

#: What SigV4 calls the service name for S3. Part of the credential scope,
#: so it is not cosmetic: a wrong value signs correctly and is rejected.
_SERVICE = "s3"

#: The hash of an empty body, precomputed. Sent as `x-amz-content-sha256`
#: on requests that carry no payload — DELETE and HEAD — where computing it
#: every time would hash the same zero bytes.
_EMPTY_PAYLOAD_SHA256 = hashlib.sha256(b"").hexdigest()


class S3StorageProvider:
    """A `StorageProvider` over any S3-compatible object store.

    Constructed once per process. Holds a client and configuration and no
    mutable state, so every method is safe to call concurrently.
    """

    def __init__(self, settings: StorageSettings, *, client: httpx.AsyncClient | None = None):
        self._endpoint = settings.s3_endpoint_url.rstrip("/")
        self._bucket = settings.s3_bucket
        self._region = settings.s3_region
        self._access_key = settings.s3_access_key_id.get_secret_value()
        self._secret_key = settings.s3_secret_access_key.get_secret_value()
        self._public_base_url = settings.public_base_url.rstrip("/")
        # Injectable so a test can drive a transport instead of a bucket —
        # the same seam `ResendEmailProvider` offers, and the reason signing
        # is testable without an account anywhere.
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.s3_timeout_seconds)
        )

    async def aclose(self) -> None:
        """Releases the connection pool. Called from the application's
        shutdown, beside the database and Redis teardown."""
        await self._client.aclose()

    async def save(self, key: str, data: bytes, *, content_type: str) -> None:
        """`PutObject`. Atomic at the store: a reader sees the old object or
        the new one, never half of either — which is the property
        `LocalStorageProvider` emulates with a temporary file and a rename.
        """
        response = await self._request(
            "PUT",
            key,
            body=data,
            headers={"content-type": content_type},
        )
        if response.status_code not in (200, 201):
            raise self._failure("save", key, response)

    async def delete(self, key: str) -> None:
        """`DeleteObject`, which S3 defines as **idempotent**: removing a key
        that is not there answers `204`, exactly as the port requires."""
        response = await self._request("DELETE", key)
        if response.status_code not in (200, 204, 404):
            raise self._failure("delete", key, response)

    async def exists(self, key: str) -> bool:
        """`HeadObject`. `404` is the ordinary answer for absence and is
        returned as `False` rather than raised, per the port."""
        response = await self._request("HEAD", key)
        if response.status_code == 200:
            return True
        if response.status_code == 404:
            return False
        raise self._failure("exists", key, response)

    def get_public_url(self, key: str) -> str:
        """The configured public prefix and the key, joined.

        Synchronous and I/O-free, as the port specifies. The prefix is a
        bucket host or a CDN in front of one; this provider never serves
        bytes itself.
        """
        return f"{self._public_base_url}{KEY_SEPARATOR}{key.lstrip(KEY_SEPARATOR)}"

    # -- the store -------------------------------------------------------

    async def _request(
        self,
        method: str,
        key: str,
        *,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """One signed request, with every transport failure translated here.

        A store that refused is a `StorageError` — permanent, because a
        wrong bucket or a revoked key does not resolve on its own. A store
        that could not be *reached* is transient: that is a network blip,
        and the distinction is what stops a retry loop running against a
        misconfiguration.
        """
        url = self._url(key)
        signed = self._sign(method, key, body, headers or {})

        try:
            return await self._client.request(method, url, content=body, headers=signed)
        except httpx.TimeoutException as error:
            raise TransientInfrastructureError(
                f"object store timed out on {method} {key}"
            ) from error
        except httpx.HTTPError as error:
            raise TransientInfrastructureError(
                f"object store unreachable on {method} {key}"
            ) from error

    def _url(self, key: str) -> str:
        return f"{self._endpoint}{self._path(key)}"

    def _path(self, key: str) -> str:
        """The canonical path: `/bucket/key`, each segment percent-encoded.

        `safe=""` on the key's segments rather than the whole string,
        because `/` separates them and must survive — the separator is
        structure, and encoding it would address one object named `a/b`
        instead of object `b` under prefix `a`.
        """
        encoded = KEY_SEPARATOR.join(
            quote(segment, safe="") for segment in key.lstrip(KEY_SEPARATOR).split(KEY_SEPARATOR)
        )
        return f"/{quote(self._bucket, safe='')}/{encoded}"

    def _sign(
        self, method: str, key: str, body: bytes, extra_headers: dict[str, str]
    ) -> dict[str, str]:
        """AWS Signature Version 4, in the four steps the specification
        names: a canonical request, a string to sign, a derived key, and the
        signature.

        Written out rather than pulled in — see this module's docstring on
        why a dependency is not the cheaper option here.
        """
        now = dt.datetime.now(dt.UTC)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(body).hexdigest() if body else _EMPTY_PAYLOAD_SHA256
        host = self._endpoint.split("://", 1)[-1]

        headers = {
            "host": host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
            **{name.lower(): value for name, value in extra_headers.items()},
        }
        signed_headers = ";".join(sorted(headers))
        canonical_headers = "".join(f"{name}:{headers[name].strip()}\n" for name in sorted(headers))

        # No query string on any of the four operations, hence the empty
        # line. Kept explicit rather than omitted: the canonical request is
        # positional, and a missing line signs a different request.
        canonical_request = "\n".join(
            [method, self._path(key), "", canonical_headers, signed_headers, payload_hash]
        )

        scope = f"{date_stamp}/{self._region}/{_SERVICE}/aws4_request"
        string_to_sign = "\n".join(
            [
                _ALGORITHM,
                amz_date,
                scope,
                hashlib.sha256(canonical_request.encode()).hexdigest(),
            ]
        )

        signing_key = _derive_key(self._secret_key, date_stamp, self._region)
        signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()

        headers["authorization"] = (
            f"{_ALGORITHM} Credential={self._access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        return headers

    def _failure(self, operation: str, key: str, response: httpx.Response) -> StorageError:
        """A refusal, logged with what an operator needs and raised with what
        a caller can act on.

        The body is logged and not raised: a store's error document can
        carry a request id worth having in a log and is not something to put
        in front of a player (§9.7).
        """
        logger.error(
            "object_store_refused",
            extra={
                "operation": operation,
                "key": key,
                "status": response.status_code,
                "body": response.text[:500],
            },
        )
        return StorageError(f"object store refused {operation} on {key}: {response.status_code}")


def _derive_key(secret: str, date_stamp: str, region: str) -> bytes:
    """The SigV4 signing key: four chained HMACs, narrowing from the secret
    to one day, one region and one service.

    That narrowing is the point of the scheme — a leaked signature is usable
    only for that day, that region and S3, which is why the derivation is
    not simply `hmac(secret, string_to_sign)`.
    """

    def _hmac(key: bytes, message: str) -> bytes:
        return hmac.new(key, message.encode(), hashlib.sha256).digest()

    date_key = _hmac(f"AWS4{secret}".encode(), date_stamp)
    region_key = _hmac(date_key, region)
    service_key = _hmac(region_key, _SERVICE)
    return _hmac(service_key, "aws4_request")
