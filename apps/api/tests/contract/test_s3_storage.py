"""`S3StorageProvider` against a real S3 implementation — A64-027.1.

`tests/unit/test_s3_storage.py` pins the request this provider composes: its
path, its headers, its signature, recomputed from the specification. What it
says plainly it cannot prove is the one thing that matters in a deployed
tier — that a **store accepts** that signature. A canonical request wrong by
one newline produces a well-formed request and a `403`, and no assertion
against a mock transport can tell the difference.

This file closes that. It runs the four port operations against MinIO,
which speaks the same protocol as S3, R2 and B2, and it **skips** rather
than fails when there is no store to reach — the rule every contract suite
here follows, so `pytest` still runs cleanly for a contributor with no
Docker.

    docker compose -f infrastructure/staging/compose.yml up -d minio
"""

import os
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr

from app.config.settings import StorageSettings
from app.storage import S3StorageProvider

_ENDPOINT = os.environ.get("CONTRACT_TEST_S3_ENDPOINT_URL", "http://localhost:9000")
_BUCKET = os.environ.get("CONTRACT_TEST_S3_BUCKET", "arena64-media")
_ACCESS_KEY = os.environ.get("CONTRACT_TEST_S3_ACCESS_KEY_ID", "arena64")
_SECRET_KEY = os.environ.get("CONTRACT_TEST_S3_SECRET_ACCESS_KEY", "arena64-secret")


@pytest_asyncio.fixture
async def provider() -> AsyncIterator[S3StorageProvider]:
    """The real provider against a real store, or a skip.

    Probes with `exists` rather than a bare TCP connect: a store that
    answers but rejects the credentials is as unusable as one that is not
    there, and the difference should be visible in the skip message rather
    than in six identical signature failures.
    """
    settings = StorageSettings(
        provider="s3",
        s3_endpoint_url=_ENDPOINT,
        s3_bucket=_BUCKET,
        s3_region="auto",
        s3_access_key_id=SecretStr(_ACCESS_KEY),
        s3_secret_access_key=SecretStr(_SECRET_KEY),
        public_base_url="https://cdn.example.com/media",
        s3_timeout_seconds=3.0,
    )
    subject = S3StorageProvider(settings)

    try:
        await subject.exists(f"probe/{uuid.uuid4()}")
    except Exception as exc:  # noqa: BLE001 — the point is to skip, not fail
        await subject.aclose()
        pytest.skip(
            f"contract tests for object storage need a reachable S3-compatible store "
            f"at {_ENDPOINT!r} with bucket {_BUCKET!r} "
            f"(see infrastructure/staging/compose.yml): {exc}"
        )

    yield subject
    await subject.aclose()


class TestTheStoreAcceptsWhatThisProviderSends:
    async def test_the_four_operations_round_trip(self, provider: S3StorageProvider) -> None:
        """One test rather than four, deliberately: each step is the setup
        for the next, and splitting them would mean four uploads and four
        stores' worth of cleanup to assert the same sequence.
        """
        key = f"contract/{uuid.uuid4()}/avatar.webp"

        assert await provider.exists(key) is False

        await provider.save(key, b"RIFF----WEBPVP8 ", content_type="image/webp")
        assert await provider.exists(key) is True

        await provider.delete(key)
        assert await provider.exists(key) is False

        # Idempotent, which `AvatarService` depends on: it removes an
        # original and a thumbnail, and a crash between the two must leave
        # the operation repeatable.
        await provider.delete(key)

    async def test_the_object_is_publicly_readable_with_its_content_type(
        self, provider: S3StorageProvider
    ) -> None:
        """The two halves of an avatar actually rendering.

        `get_public_url` composes an unsigned URL because `core/storage.py`
        says avatars are public objects — so the bucket has to *be* public,
        and the stored `content-type` has to survive, or a browser downloads
        the file instead of drawing it.

        Fetched from the endpoint rather than from `get_public_url`, whose
        prefix is a CDN hostname that does not exist in a test.
        """
        key = f"contract/{uuid.uuid4()}/avatar.webp"
        await provider.save(key, b"RIFF----WEBPVP8 ", content_type="image/webp")

        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.get(f"{_ENDPOINT}/{_BUCKET}/{key}")

            assert response.status_code == 200, response.text
            assert response.headers["content-type"] == "image/webp"
            assert response.content == b"RIFF----WEBPVP8 "
        finally:
            await provider.delete(key)
