"""`S3StorageProvider` — A64-027.1.

## Why these tests use a transport and not a bucket

Every assertion here is about the **request this provider composes**: its
method, its path, its headers and its signature. None of that needs an
object store, and testing it against one would make the suite depend on an
account, a network and somebody's credentials to learn things that are
decidable from the bytes.

What a real bucket would add is confidence that S3 *accepts* the signature,
and that is worth having — as a contract test against MinIO, when this
platform runs one. It is recorded as absent in `specs/` rather than
implied by these passing.

## Why the signature is asserted at all

A wrong signature is the failure mode that looks like nothing: the request
is well-formed, the store answers `403`, and the message says only that the
signature does not match. The four inputs that produce it — the canonical
request, the scope, the derived key, the signed header list — are pinned
here against vectors computed from the specification, so a change to any of
them fails in this file rather than in a deployed tier.
"""

import datetime as dt
import hashlib
import hmac
from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr

from app.config.settings import StorageSettings
from app.core.exceptions import TransientInfrastructureError
from app.core.storage import StorageError
from app.storage import S3StorageProvider

SETTINGS = StorageSettings(
    provider="s3",
    s3_endpoint_url="https://store.example.com",
    s3_bucket="arena64-staging",
    s3_region="auto",
    s3_access_key_id=SecretStr("AKIAEXAMPLE"),
    s3_secret_access_key=SecretStr("secret-example"),
    public_base_url="https://cdn.example.com/media",
)


def _provider(handler: Callable[[httpx.Request], httpx.Response]) -> S3StorageProvider:
    return S3StorageProvider(
        SETTINGS, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


class TestTheRequestItComposes:
    async def test_save_puts_the_bytes_at_the_bucket_key_with_its_content_type(self) -> None:
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["method"] = request.method
            seen["url"] = str(request.url)
            seen["type"] = request.headers.get("content-type")
            seen["body"] = request.content
            return httpx.Response(200)

        await _provider(handler).save("avatars/a/b.webp", b"bytes", content_type="image/webp")

        assert seen["method"] == "PUT"
        assert seen["url"] == "https://store.example.com/arena64-staging/avatars/a/b.webp"
        assert seen["type"] == "image/webp"
        assert seen["body"] == b"bytes"

    async def test_a_key_segment_is_encoded_and_the_separator_survives(self) -> None:
        """`/` is structure, not content. Encoding it would address one
        object named `a/b` instead of object `b` under prefix `a`."""
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200)

        await _provider(handler).save("avatars/a b/c+d.webp", b"x", content_type="image/webp")

        assert seen["url"] == ("https://store.example.com/arena64-staging/avatars/a%20b/c%2Bd.webp")

    async def test_the_public_url_is_the_configured_prefix_and_never_the_endpoint(self) -> None:
        """A bucket host and a CDN host are different things, and this
        platform serves the second."""
        url = _provider(lambda _: httpx.Response(200)).get_public_url("avatars/a/b.webp")
        assert url == "https://cdn.example.com/media/avatars/a/b.webp"


class TestTheSignature:
    async def test_it_signs_with_the_scope_and_headers_the_specification_names(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["authorization"] = request.headers["authorization"]
            seen["date"] = request.headers["x-amz-date"]
            seen["payload"] = request.headers["x-amz-content-sha256"]
            return httpx.Response(200)

        await _provider(handler).save("k.webp", b"payload", content_type="image/webp")

        stamp = seen["date"][:8]
        assert seen["authorization"].startswith("AWS4-HMAC-SHA256 ")
        assert f"Credential=AKIAEXAMPLE/{stamp}/auto/s3/aws4_request" in seen["authorization"]
        # The signed header list is part of the signature: a store recomputes
        # it from exactly these names, so an added or dropped header is a 403.
        assert (
            "SignedHeaders=content-type;host;x-amz-content-sha256;x-amz-date"
            in seen["authorization"]
        )
        assert seen["payload"] == hashlib.sha256(b"payload").hexdigest()

    async def test_the_signing_key_is_derived_day_region_service(self) -> None:
        """The chain is what limits a leaked signature to one day, one region
        and S3. Recomputed here from the specification rather than from the
        implementation, so the two must agree."""
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["authorization"] = request.headers["authorization"]
            seen["date"] = request.headers["x-amz-date"]
            seen["payload"] = request.headers["x-amz-content-sha256"]
            return httpx.Response(200)

        await _provider(handler).exists("k.webp")

        amz_date = seen["date"]
        stamp = amz_date[:8]
        canonical_headers = (
            "host:store.example.com\n"
            f"x-amz-content-sha256:{seen['payload']}\n"
            f"x-amz-date:{amz_date}\n"
        )
        canonical_request = "\n".join(
            [
                "HEAD",
                "/arena64-staging/k.webp",
                "",
                canonical_headers,
                "host;x-amz-content-sha256;x-amz-date",
                seen["payload"],
            ]
        )
        scope = f"{stamp}/auto/s3/aws4_request"
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amz_date,
                scope,
                hashlib.sha256(canonical_request.encode()).hexdigest(),
            ]
        )

        def sign(key: bytes, message: str) -> bytes:
            return hmac.new(key, message.encode(), hashlib.sha256).digest()

        key = sign(sign(sign(sign(b"AWS4secret-example", stamp), "auto"), "s3"), "aws4_request")
        expected = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()

        assert f"Signature={expected}" in seen["authorization"]

    async def test_the_date_is_this_instant_in_utc(self) -> None:
        """Signatures expire. A store rejects one skewed by more than fifteen
        minutes, so a provider signing in local time fails everywhere except
        UTC."""
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["date"] = request.headers["x-amz-date"]
            return httpx.Response(204)

        await _provider(handler).delete("k.webp")

        signed_at = dt.datetime.strptime(seen["date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.UTC)
        assert abs((dt.datetime.now(dt.UTC) - signed_at).total_seconds()) < 60


class TestWhatTheStoreAnswers:
    async def test_a_missing_object_does_not_exist_rather_than_raising(self) -> None:
        assert await _provider(lambda _: httpx.Response(404)).exists("gone.webp") is False

    async def test_deleting_a_missing_object_succeeds(self) -> None:
        """The port requires idempotence: `AvatarService` removes an original
        and a thumbnail, and a crash between the two must leave the operation
        repeatable."""
        await _provider(lambda _: httpx.Response(404)).delete("gone.webp")

    async def test_a_refusal_is_permanent_and_carries_no_store_prose(self) -> None:
        """A wrong bucket or a revoked key does not resolve on its own, so
        retrying it is how one misconfiguration becomes a load test."""
        provider = _provider(lambda _: httpx.Response(403, text="<Error>AccessDenied</Error>"))

        with pytest.raises(StorageError) as raised:
            await provider.save("k.webp", b"x", content_type="image/webp")

        assert "403" in str(raised.value)
        assert "AccessDenied" not in str(raised.value)

    async def test_an_unreachable_store_is_transient(self) -> None:
        """The distinction the caller acts on: a network blip may be retried,
        a refusal may not."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host")

        with pytest.raises(TransientInfrastructureError):
            await _provider(handler).exists("k.webp")

    async def test_a_timeout_is_transient(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow")

        with pytest.raises(TransientInfrastructureError):
            await _provider(handler).save("k.webp", b"x", content_type="image/webp")


class TestConfiguration:
    def test_choosing_s3_without_credentials_fails_at_startup(self) -> None:
        """DI-06: fail before serving traffic. The alternative is a tier that
        serves every page and rejects the first avatar hours later."""
        with pytest.raises(ValueError, match="STORAGE_S3_"):
            StorageSettings(provider="s3", s3_endpoint_url="https://store.example.com")
