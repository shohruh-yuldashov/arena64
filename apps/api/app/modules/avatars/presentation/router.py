"""HTTP routes for `avatars` — the self-service avatar API.

Three endpoints, and **no business logic in any of them**. Each handler
translates a request into a service call and the result into a wire schema.
Validation is the service's, processing is the processor's, storage is the
provider's, and URL composition is `AvatarLinkBuilder`'s.

## Why the path is `/profile/avatar` and not `/profiles/{username}/avatar`

Singular, and with no identifier in it. That is the security design rather
than a naming preference.

A64-012.2 requires that "users may modify ONLY their own avatar". The
strongest way to enforce that is to build an endpoint on which the target
*cannot be expressed*: the account is taken from the access token's `sub`
and there is no path segment, query parameter or body field that could
name a different one. There is no authorization check here because there is
nothing to check — an attacker cannot address somebody else's avatar to
begin with.

`/profiles/{username}` stays what it is: the public, unauthenticated read
of anybody's profile (A64-012.1). One is "me", one is "them", and keeping
them on different paths is what keeps the second from ever growing a write
verb.

## Errors need no handling here

Every failure is a typed exception on the platform hierarchy, and
`app/api/exception_handlers.py` maps them by MRO walk. There is not one
`try`/`except` in this file:

    EmptyAvatarUpload       -> 422  validation_error
    UnsupportedImageFormat  -> 422  validation_error
    InvalidAvatarImage      -> 422  validation_error
    AvatarTooLarge          -> 422  avatar_too_large
    AvatarNotFound          -> 404  not_found
    StorageError            -> 500  permanent_infrastructure_error
    MissingToken            -> 401  authentication_required

## Not rate limited

A64-012.2's scope does not include it, and this endpoint is authenticated —
abusing it costs an account. It is nonetheless the most expensive
unauthenticated-adjacent operation on the platform (a decode and two
encodes per call), and `app.api.rate_limiting.RateLimit` is one dependency
away. The recommendations say so.
"""

import logging
from typing import Annotated, Any

from fastapi import APIRouter, File, UploadFile, status

from app.api.exception_handlers import ErrorResponse
from app.api.responses import build_response
from app.core.responses import ApiResponse
from app.modules.auth.presentation.dependencies import CurrentUser
from app.modules.avatars.presentation.dependencies import (
    AvatarLinkBuilderDep,
    AvatarServiceDep,
)
from app.modules.avatars.presentation.schemas import AvatarResponse
from app.modules.avatars.presentation.schemas.avatar import (
    ACCEPTED_FORMATS_TEXT,
    MAX_UPLOAD_MB,
)

logger = logging.getLogger(__name__)

avatar_router = APIRouter(prefix="/profile/avatar", tags=["avatars"])

#: FastAPI's own annotation for `responses=`. Spelled once rather than
#: inferred, for the reason `auth`'s router gives.
type _Responses = dict[int | str, dict[str, Any]]

_UNAUTHORIZED: _Responses = {
    401: {
        "description": "No access token was presented, or it was invalid or expired.",
        "model": ErrorResponse,
    }
}
_NOT_FOUND: _Responses = {
    404: {
        "description": "This account has no avatar.",
        "model": ErrorResponse,
    }
}
_UNPROCESSABLE: _Responses = {
    422: {
        "description": (
            "The upload was rejected. `code` is `avatar_too_large` when the file "
            f"exceeds {MAX_UPLOAD_MB} MB — a client can re-encode and retry — and "
            "`validation_error` when the file is empty, is not one of the accepted "
            "formats, or is not a readable image."
        ),
        "model": ErrorResponse,
    }
}


@avatar_router.post(
    "",
    status_code=status.HTTP_200_OK,
    summary="Upload or replace your avatar",
    response_description="The avatar as it now stands, with its stored dimensions.",
    responses={**_UNAUTHORIZED, **_UNPROCESSABLE},
)
async def upload_avatar(
    user: CurrentUser,
    service: AvatarServiceDep,
    links: AvatarLinkBuilderDep,
    file: Annotated[
        UploadFile,
        File(
            description=(
                f"The image. Accepted: {ACCEPTED_FORMATS_TEXT}. At most {MAX_UPLOAD_MB} MB."
            )
        ),
    ],
) -> ApiResponse[AvatarResponse]:
    """Stores a new avatar for the authenticated account, replacing any
    existing one.

    **Upload and replace are the same call.** There is no separate replace
    endpoint: sending a new image when one already exists replaces it and
    removes the previous objects. A client does not need to know which case
    it is in, and could not usefully act on the difference.

    `200`, not `201`. The avatar is a property of an account that already
    exists rather than a new resource with its own URL, and there is no
    `Location` a client could follow to it — the URLs are in the body.

    ## What happens to the file

    Nothing is stored until the image has been validated and re-encoded.
    In order: the size is checked, then the **file signature** — the
    declared `Content-Type` is ignored entirely, because it is a
    client-supplied string and a renamed executable sets it to `image/png`
    as easily as a real image does. Only then is the file decoded.

    The stored image is **not the file that was uploaded**. It is
    re-encoded to WebP from decoded pixels, which strips every piece of
    metadata the original carried — EXIF on a phone photo routinely
    includes GPS coordinates and a device serial, and publishing an avatar
    must not publish where it was taken. Orientation is applied to the
    pixels first, so an image that relied on an EXIF rotation tag still
    appears the right way up in renderers that ignore the tag.

    Two renditions are stored: the original fitted inside 512x512 and a
    thumbnail fitted inside 128x128. **Neither is cropped** — a non-square
    image is fitted inside the box and keeps its aspect ratio, so a 3:2
    photo becomes 512x341. An image already smaller than the box is not
    enlarged.

    The original filename is discarded and never used: the stored object
    gets a cryptographically random name.

    ## Caching

    Both URLs carry a `?v=` matching `avatar_version`, which increments on
    every upload and every delete. The object key itself is random per
    upload, so the URLs of a replaced avatar are new either way — treat any
    avatar URL as immutable and cache it freely.
    """
    data = await file.read()
    reference, processed = await service.upload(user.id, data)

    return build_response(
        AvatarResponse.of(reference, links.links_for(reference), processed=processed)
    )


@avatar_router.get(
    "",
    status_code=status.HTTP_200_OK,
    summary="Read your avatar's metadata",
    response_description="The current avatar's URLs, version and upload time.",
    responses={**_UNAUTHORIZED, **_NOT_FOUND},
)
async def get_avatar(
    user: CurrentUser,
    service: AvatarServiceDep,
    links: AvatarLinkBuilderDep,
) -> ApiResponse[AvatarResponse]:
    """Returns the authenticated account's avatar metadata.

    `404` when the account has no avatar. That is deliberately different
    from `DELETE`, which succeeds in the same state: this endpoint asks for
    a resource, and answering `200` with a body of nulls would make a
    client inspect three fields to learn what a status code says in one.

    `dimensions` is **omitted** here. Reporting it would mean fetching and
    decoding the stored file on every read, which is a decode per profile
    page to report two numbers the client can read off the image it is
    about to load anyway. `POST` returns them because the encode that
    produced them just happened.

    Returns only *this* account's avatar. Anyone else's is on
    `GET /api/v1/profiles/{username}`, which is public and needs no token.
    """
    reference = await service.get_avatar(user.id)

    return build_response(AvatarResponse.of(reference, links.links_for(reference)))


@avatar_router.delete(
    "",
    status_code=status.HTTP_200_OK,
    summary="Delete your avatar",
    response_description="The cleared avatar state, with its new version.",
    responses=_UNAUTHORIZED,
)
async def delete_avatar(
    user: CurrentUser,
    service: AvatarServiceDep,
    links: AvatarLinkBuilderDep,
) -> ApiResponse[AvatarResponse]:
    """Removes the authenticated account's avatar — both stored renditions
    and the reference to them.

    **Idempotent.** Deleting when there is no avatar succeeds, and
    `avatar_version` still increments. A caller retrying after a dropped
    response must not receive an error for the retry, and "there is no
    avatar" is the outcome it wanted.

    `200` with a body rather than `204`, because there *is* something to
    say: the new `avatar_version`. A client that cached the previous URL
    needs it precisely now — the version is what tells any browser or CDN
    still holding the old image to stop serving it. A `204` would leave the
    client to guess, or to issue a second request for a number this one
    already computed.

    `avatar_url` and `thumbnail_url` come back `null`. Rendering a
    placeholder — an identicon, a coloured initial — is the client's
    decision, because only the client knows the size and the surrounding
    design.
    """
    reference = await service.delete(user.id)

    return build_response(AvatarResponse.of(reference, links.links_for(reference)))
