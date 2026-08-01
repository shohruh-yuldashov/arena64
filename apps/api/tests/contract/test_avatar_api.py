"""The avatar system end to end — real PostgreSQL, a real object store on
disk, real Pillow, the real composition root.

A64-012.2 asks for essential tests only and names four: successful upload,
invalid MIME type, oversized file, delete. All four are here. What else is
here is the small number of properties that would be silently wrong rather
than loudly broken, and that no unit test can reach:

  - the stored object is **actually fetchable** at the URL returned. A
    storage abstraction is easiest to get subtly wrong here — a key that
    saves fine and composes into a URL nothing serves;
  - **EXIF does not survive**, which is the difference between publishing
    an avatar and publishing where somebody's photo was taken;
  - **no orphaned files** after a replace or a delete, asserted by looking
    at the directory rather than by trusting the ordering argument;
  - a **renamed executable** is refused, which is the whole point of
    ignoring `Content-Type`.

Nothing here mocks storage or Pillow. The point is the composition.

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

import io
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db_session, get_rate_limiter
from app.app_factory import create_app
from app.config.settings import get_settings
from app.modules.avatars.domain.images import (
    MAX_DIMENSION,
    MAX_UPLOAD_BYTES,
    THUMBNAIL_DIMENSION,
)
from app.storage import LocalStorageProvider
from tests.contract.conftest import with_presence_switched_off
from tests.fakes.rate_limiter import AllowAllRateLimiter

AVATAR_URL = "/api/v1/profile/avatar"
REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
PASSWORD = "CorrectHorse1!"


def png(size: tuple[int, int] = (1200, 800), colour: tuple[int, int, int] = (30, 90, 200)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


def jpeg_with_exif() -> bytes:
    """A JPEG carrying an orientation tag and a text field.

    Both matter. The orientation must be *applied* to the pixels (so a
    renderer ignoring the tag still shows it upright) and the text must
    *not* survive — on a real phone photo that field's neighbours are GPS
    coordinates and a device serial.
    """
    image = Image.new("RGB", (800, 400), (10, 200, 10))
    exif = image.getexif()
    exif[0x0112] = 6  # Orientation: rotate 90° clockwise.
    exif[0x9286] = "SECRET-USER-COMMENT"
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


@pytest.fixture
def app(contract_storage_root: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """The production app, with the object store rooted in a temp directory.

    Redirected through the **environment** rather than by replacing
    `app.state.storage` afterwards, and the difference matters: `create_app`
    both builds the provider *and* mounts `StaticFiles` at its root, so
    swapping only the provider would leave the mount serving the configured
    directory. Mounting a second time does not help — Starlette matches
    routes in order, so the first mount wins.

    Setting the variable before construction means the app under test is
    wired exactly as a deployed one, pointed somewhere disposable. That is
    what makes "the returned URL actually serves the image" a real
    assertion rather than one about a directory the app is not using.
    """
    monkeypatch.setenv("STORAGE_LOCAL_ROOT", str(contract_storage_root))
    get_settings.cache_clear()
    return create_app()


@pytest.fixture
def storage(app: FastAPI) -> LocalStorageProvider:
    """The provider the app actually built."""
    provider: LocalStorageProvider = app.state.storage
    return provider


@pytest_asyncio.fixture
async def client(app: FastAPI, contract_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """The production app with only the session and the rate limiter
    redirected.

    The rate limiter is a permissive double for the reason
    `tests/conftest.py` documents: limiting is off suite-wide, and these
    tests would otherwise depend on a counter with an hour-long window.
    """

    async def _session() -> AsyncIterator[AsyncSession]:
        yield contract_session

    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[get_rate_limiter] = lambda: AllowAllRateLimiter()
    with_presence_switched_off(app)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def auth(client: AsyncClient) -> dict[str, str]:
    """A registered, signed-in account's bearer header."""
    suffix = uuid4().hex[:10]
    account = {
        "username": f"player{suffix}",
        "email": f"{suffix}@example.com",
        "password": PASSWORD,
    }
    registered = await client.post(REGISTER_URL, json=account)
    assert registered.status_code == 201, registered.text

    signed_in = await client.post(LOGIN_URL, json={"email": account["email"], "password": PASSWORD})
    assert signed_in.status_code == 200, signed_in.text
    return {"Authorization": f"Bearer {signed_in.json()['data']['access_token']}"}


async def upload(client: AsyncClient, auth: dict[str, str], data: bytes) -> dict[str, Any]:
    response = await client.post(
        AVATAR_URL, headers=auth, files={"file": ("whatever.png", data, "image/png")}
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()["data"]
    return body


def stored_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file())


class TestSuccessfulUpload:
    async def test_returns_urls_a_version_and_dimensions(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        body = await upload(client, auth, png())

        assert body["avatar_url"]
        assert body["thumbnail_url"]
        assert body["uploaded_at"]
        assert body["dimensions"]["original"] == {"width": 512, "height": 341}
        assert body["dimensions"]["thumbnail"] == {"width": 128, "height": 85}

    async def test_the_returned_url_actually_serves_the_image(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        """The assertion a storage abstraction most needs. A key can save
        cleanly and compose into a URL that nothing serves, and every unit
        test would still pass."""
        body = await upload(client, auth, png())

        fetched = await client.get(body["avatar_url"].split("localhost:8000")[-1])

        assert fetched.status_code == 200
        assert fetched.headers["content-type"] == "image/webp"
        assert fetched.content.startswith(b"RIFF")

    async def test_both_renditions_are_stored(
        self, client: AsyncClient, auth: dict[str, str], storage: LocalStorageProvider
    ) -> None:
        await upload(client, auth, png())

        assert len(stored_files(storage.root)) == 2

    async def test_aspect_ratio_is_preserved_rather_than_cropped(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        """A 3:2 image becomes 512x341, not 512x512. Cropping would
        silently discard part of what somebody uploaded, and deciding which
        part is a product question nobody has answered."""
        body = await upload(client, auth, png(size=(1500, 1000)))

        original = body["dimensions"]["original"]
        assert original["width"] == MAX_DIMENSION
        assert original["height"] == round(MAX_DIMENSION * 1000 / 1500)

    async def test_a_small_image_is_not_enlarged(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        body = await upload(client, auth, png(size=(64, 64)))

        assert body["dimensions"]["original"] == {"width": 64, "height": 64}

    async def test_the_original_filename_is_discarded(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        """Never trusted, never used. A filename is attacker-controlled and
        routinely carries the uploader's real name off their desktop."""
        response = await client.post(
            AVATAR_URL,
            headers=auth,
            files={"file": ("../../Personal Photo of Alice.png", png(), "image/png")},
        )

        body = response.json()["data"]
        assert "Alice" not in body["avatar_url"]
        assert "Personal" not in body["avatar_url"]
        assert ".." not in body["avatar_url"]

    async def test_exif_is_stripped_and_orientation_applied(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        """Two properties in one upload. The 800x400 source carries
        `Orientation=6` (rotate 90°), so the stored image must come out
        *portrait* — and the text field must not survive at all."""
        body = await upload(client, auth, jpeg_with_exif())

        assert body["dimensions"]["original"]["height"] > body["dimensions"]["original"]["width"]

        fetched = await client.get(body["avatar_url"].split("localhost:8000")[-1])
        assert b"SECRET-USER-COMMENT" not in fetched.content

        with Image.open(io.BytesIO(fetched.content)) as stored:
            assert not stored.getexif()

    async def test_the_public_profile_renders_the_avatar(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        """The integration A64-012.2 asks for: the URL is composed during
        profile response mapping, from the stored key."""
        body = await upload(client, auth, png())
        me = await client.get("/api/v1/auth/me", headers=auth)
        username = me.json()["data"]["username"]

        profile = await client.get(f"/api/v1/profiles/{username}")

        data = profile.json()["data"]
        assert data["avatar_url"] == body["avatar_url"]
        assert data["thumbnail_url"] == body["thumbnail_url"]


class TestInvalidMimeType:
    @pytest.mark.parametrize(
        ("label", "payload"),
        [
            pytest.param("elf executable", b"\x7fELF\x02\x01\x01" + b"\x00" * 128, id="elf"),
            pytest.param("shell script", b"#!/bin/sh\nrm -rf /\n" * 8, id="script"),
            pytest.param("gif", b"GIF89a" + b"\x00" * 64, id="gif"),
            pytest.param("svg", b"<svg xmlns='http://www.w3.org/2000/svg'></svg>", id="svg"),
            pytest.param("wav", b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 32, id="wav-riff"),
        ],
    )
    async def test_a_file_that_is_not_an_accepted_image_is_refused(
        self, client: AsyncClient, auth: dict[str, str], label: str, payload: bytes
    ) -> None:
        """**Every one of these declares `image/png`.** The header is
        ignored entirely — what decides is the file signature, which an
        uploader would have to construct a genuine image to forge.

        The WAV case is the non-obvious one: it shares WebP's `RIFF`
        container magic, so a signature check that matched only the first
        four bytes would accept it.
        """
        response = await client.post(
            AVATAR_URL, headers=auth, files={"file": ("payload.png", payload, "image/png")}
        )

        assert response.status_code == 422, f"{label} was accepted"
        assert response.json()["code"] == "validation_error"

    async def test_a_declared_image_type_does_not_help(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        """The same bytes, declared three different ways, all refused
        identically — which is what "do not trust Content-Type" means in
        practice."""
        payload = b"\x7fELF\x02\x01\x01" + b"\x00" * 128

        for content_type in ("image/png", "image/jpeg", "image/webp"):
            response = await client.post(
                AVATAR_URL, headers=auth, files={"file": ("x", payload, content_type)}
            )
            assert response.status_code == 422

    async def test_an_empty_file_is_refused(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        response = await client.post(
            AVATAR_URL, headers=auth, files={"file": ("empty.png", b"", "image/png")}
        )

        assert response.status_code == 422

    async def test_a_corrupt_image_with_a_valid_signature_is_refused(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        """The second stage doing its job: the signature says PNG and the
        decoder disagrees. A signature check alone would have accepted
        this."""
        response = await client.post(
            AVATAR_URL,
            headers=auth,
            files={"file": ("x.png", b"\x89PNG\r\n\x1a\n" + b"junk" * 64, "image/png")},
        )

        assert response.status_code == 422

    async def test_a_rejected_upload_stores_nothing(
        self, client: AsyncClient, auth: dict[str, str], storage: LocalStorageProvider
    ) -> None:
        """Validation happens before any write, so a rejection needs no
        compensating delete — which matters because the rejection path is
        the one that runs most often under attack."""
        await client.post(
            AVATAR_URL, headers=auth, files={"file": ("x.png", b"\x7fELF", "image/png")}
        )

        assert stored_files(storage.root) == []


class TestOversizedFile:
    async def test_a_file_over_the_limit_is_refused(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        oversized = png() + b"\x00" * MAX_UPLOAD_BYTES

        response = await client.post(
            AVATAR_URL, headers=auth, files={"file": ("big.png", oversized, "image/png")}
        )

        assert response.status_code == 422
        assert response.json()["code"] == "avatar_too_large"

    async def test_the_error_names_the_limit(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        """`avatar_too_large` is the one avatar rejection a client can act
        on automatically — re-encode and retry — so the message says what
        to aim for."""
        oversized = png() + b"\x00" * MAX_UPLOAD_BYTES

        response = await client.post(
            AVATAR_URL, headers=auth, files={"file": ("big.png", oversized, "image/png")}
        )

        assert "5 MB" in response.json()["message"]

    async def test_an_oversized_upload_stores_nothing(
        self, client: AsyncClient, auth: dict[str, str], storage: LocalStorageProvider
    ) -> None:
        await client.post(
            AVATAR_URL,
            headers=auth,
            files={"file": ("big.png", png() + b"\x00" * MAX_UPLOAD_BYTES, "image/png")},
        )

        assert stored_files(storage.root) == []


class TestReplace:
    async def test_replacing_bumps_the_version(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        first = await upload(client, auth, png())

        second = await upload(client, auth, png(size=(400, 400), colour=(250, 10, 10)))

        assert second["avatar_version"] == first["avatar_version"] + 1
        assert second["avatar_url"] != first["avatar_url"]

    async def test_replacing_removes_the_previous_objects(
        self, client: AsyncClient, auth: dict[str, str], storage: LocalStorageProvider
    ) -> None:
        """No orphans. Asserted by counting files rather than by trusting
        the ordering argument in `AvatarService`."""
        first = await upload(client, auth, png())

        await upload(client, auth, png(size=(400, 400)))

        assert len(stored_files(storage.root)) == 2
        stale = await client.get(first["avatar_url"].split("localhost:8000")[-1])
        assert stale.status_code == 404


class TestDeleteAvatar:
    async def test_returns_a_cleared_state(self, client: AsyncClient, auth: dict[str, str]) -> None:
        uploaded = await upload(client, auth, png())

        response = await client.delete(AVATAR_URL, headers=auth)

        assert response.status_code == 200
        body = response.json()["data"]
        assert body["avatar_url"] is None
        assert body["thumbnail_url"] is None
        assert body["uploaded_at"] is None
        assert body["avatar_version"] == uploaded["avatar_version"] + 1

    async def test_removes_both_objects(
        self, client: AsyncClient, auth: dict[str, str], storage: LocalStorageProvider
    ) -> None:
        """A64-012.2: "deleting an avatar must not leave orphaned files"."""
        await upload(client, auth, png())

        await client.delete(AVATAR_URL, headers=auth)

        assert stored_files(storage.root) == []

    async def test_is_idempotent(self, client: AsyncClient, auth: dict[str, str]) -> None:
        """A caller retrying after a dropped response must not receive an
        error — "there is no avatar" is the outcome it wanted. The version
        still bumps, because a cached copy needs the change signal
        precisely then."""
        await upload(client, auth, png())
        first = await client.delete(AVATAR_URL, headers=auth)

        second = await client.delete(AVATAR_URL, headers=auth)

        assert second.status_code == 200
        assert second.json()["data"]["avatar_version"] > first.json()["data"]["avatar_version"]

    async def test_deleting_without_an_avatar_succeeds(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        assert (await client.delete(AVATAR_URL, headers=auth)).status_code == 200

    async def test_the_public_profile_stops_rendering_it(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        await upload(client, auth, png())
        me = await client.get("/api/v1/auth/me", headers=auth)
        username = me.json()["data"]["username"]

        await client.delete(AVATAR_URL, headers=auth)

        profile = await client.get(f"/api/v1/profiles/{username}")
        assert profile.json()["data"]["avatar_url"] is None


class TestReadMetadata:
    async def test_returns_404_before_any_upload(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        response = await client.get(AVATAR_URL, headers=auth)

        assert response.status_code == 404

    async def test_returns_the_current_avatar(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        uploaded = await upload(client, auth, png())

        response = await client.get(AVATAR_URL, headers=auth)

        body = response.json()["data"]
        assert body["avatar_url"] == uploaded["avatar_url"]
        assert body["avatar_version"] == uploaded["avatar_version"]

    async def test_omits_dimensions(self, client: AsyncClient, auth: dict[str, str]) -> None:
        """Reporting them would mean decoding the stored file on every
        read, to report two numbers the client can read off the image it is
        about to load."""
        await upload(client, auth, png())

        body = (await client.get(AVATAR_URL, headers=auth)).json()["data"]

        assert body["dimensions"] is None


class TestOnlyYourOwnAvatar:
    @pytest.mark.parametrize("method", ["post", "get", "delete"])
    async def test_every_endpoint_requires_authentication(
        self, client: AsyncClient, method: str
    ) -> None:
        """There is no path segment or body field naming an account, so
        "may I modify this one" is not a question these endpoints can be
        asked — the account comes from the token's `sub`. Authentication is
        therefore the whole of the authorization."""
        if method == "post":
            response = await client.post(AVATAR_URL, files={"file": ("x.png", png(), "image/png")})
        else:
            response = await getattr(client, method)(AVATAR_URL)

        assert response.status_code == 401

    async def test_one_account_cannot_reach_anothers(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        """Asserted structurally: the upload is stored under the *token's*
        player id, so an attacker holding their own token writes only into
        their own prefix."""
        body = await upload(client, auth, png())
        me = await client.get("/api/v1/auth/me", headers=auth)

        assert me.json()["data"]["id"] in body["avatar_url"]


class TestNoFilesystemPathsEscape:
    async def test_the_response_carries_no_filesystem_path(
        self, client: AsyncClient, auth: dict[str, str], storage: LocalStorageProvider
    ) -> None:
        """A64-012.2: "never expose filesystem paths". The URL resembles
        the on-disk layout by coincidence of the local provider; the
        absolute root must not appear."""
        response = await client.post(
            AVATAR_URL, headers=auth, files={"file": ("x.png", png(), "image/png")}
        )

        assert str(storage.root) not in response.text

    async def test_an_error_carries_no_filesystem_path(
        self, client: AsyncClient, auth: dict[str, str], storage: LocalStorageProvider
    ) -> None:
        response = await client.post(
            AVATAR_URL, headers=auth, files={"file": ("x.png", b"\x7fELF", "image/png")}
        )

        assert str(storage.root) not in response.text
        assert "/tmp" not in response.text


class TestOpenApi:
    async def test_every_endpoint_is_documented(self, client: AsyncClient) -> None:
        spec = (await client.get("/openapi.json")).json()
        operations = spec["paths"]["/api/v1/profile/avatar"]

        assert set(operations) >= {"post", "get", "delete"}
        for operation in operations.values():
            assert operation["summary"]
            assert operation["description"].strip()
            assert operation["tags"] == ["avatars"]

    async def test_the_upload_documents_its_limits(self, client: AsyncClient) -> None:
        spec = (await client.get("/openapi.json")).json()
        upload_operation = spec["paths"]["/api/v1/profile/avatar"]["post"]

        assert "5 MB" in str(upload_operation)
        assert "422" in upload_operation["responses"]

        # The accepted formats are documented on the file field, which
        # FastAPI emits as a generated component schema rather than inline
        # on the operation — so the assertion looks at the document.
        body_schema = next(
            schema
            for name, schema in spec["components"]["schemas"].items()
            if name.startswith("Body_upload_avatar")
        )
        rendered_body = str(body_schema)
        assert "image/jpeg" in rendered_body
        assert "image/webp" in rendered_body
        assert "5 MB" in rendered_body

    async def test_the_response_schema_carries_an_example(self, client: AsyncClient) -> None:
        spec = (await client.get("/openapi.json")).json()

        assert spec["components"]["schemas"]["AvatarResponse"].get("examples")

    async def test_the_dimension_bounds_are_documented(self, client: AsyncClient) -> None:
        spec = (await client.get("/openapi.json")).json()
        rendered = str(spec["components"]["schemas"]["AvatarResponse"])

        assert str(MAX_DIMENSION) in rendered
        assert str(THUMBNAIL_DIMENSION) in rendered
