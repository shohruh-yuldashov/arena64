"""Wire schemas for the avatar endpoints — A64-012.2.

One response type, returned by all three endpoints, and no request type:
the upload body is `multipart/form-data`, which FastAPI models with
`UploadFile` rather than a Pydantic schema.

## Why upload, read and delete share one response

All three answer the same question — *what is this account's avatar now* —
and a client's handling is identical: replace whatever it was rendering.
Three shapes would mean three parsers for one concept, and the delete
response in particular would otherwise be a bespoke "ok: true" that tells a
client nothing it can use.

The one place they differ is `dimensions`, which only an upload can report
without decoding the stored file. It is optional for exactly that reason,
and documented as such.
"""

from datetime import datetime

from pydantic import Field

from app.core.dto import BaseResponseDTO
from app.modules.avatars.domain.images import (
    MAX_DIMENSION,
    MAX_UPLOAD_BYTES,
    THUMBNAIL_DIMENSION,
    accepted_content_types,
)
from app.modules.avatars.domain.renditions import ProcessedAvatar
from app.modules.avatars.public import AvatarLinks
from app.modules.users.public import AvatarReference

#: Rendered into the OpenAPI description of the upload endpoint, so the
#: documented limits are the constants the validator enforces rather than
#: prose that drifts from them.
ACCEPTED_FORMATS_TEXT = ", ".join(accepted_content_types())
MAX_UPLOAD_MB = MAX_UPLOAD_BYTES // (1024 * 1024)


class ImageDimensions(BaseResponseDTO):
    """The pixel size of a stored rendition."""

    width: int = Field(description="Width in pixels.", examples=[512])
    height: int = Field(description="Height in pixels.", examples=[341])


class AvatarDimensions(BaseResponseDTO):
    """Both renditions' dimensions, as stored.

    Neither is necessarily square: a non-square upload is fitted *inside*
    the bounding box rather than cropped, because choosing which part of
    somebody's picture to discard is a product decision nobody has made. A
    3:2 photo becomes 512x341, not 512x512.
    """

    original: ImageDimensions
    thumbnail: ImageDimensions


class AvatarResponse(BaseResponseDTO):
    """An account's avatar as it now stands.

    Returned by all three endpoints — see this module's docstring.
    """

    avatar_url: str | None = Field(
        default=None,
        description=(
            f"URL of the full-size rendition, at most {MAX_DIMENSION}px on its longest "
            "edge. `null` when the account has no avatar. Carries a `?v=` cache-buster "
            "that changes on every upload and every delete."
        ),
        examples=["http://localhost:8000/media/avatars/019fb9ea-.../DkP2n1Qe.webp?v=2"],
    )
    thumbnail_url: str | None = Field(
        default=None,
        description=(
            f"URL of the {THUMBNAIL_DIMENSION}px rendition, for listings and match "
            "cards. `null` when the account has no avatar."
        ),
        examples=["http://localhost:8000/media/avatars/019fb9ea-.../DkP2n1Qe_thumb.webp?v=2"],
    )
    avatar_version: int = Field(
        description=(
            "Increments on every upload **and** every delete. Present even when there "
            "is no avatar, because a client that cached the previous one needs the "
            "change signal precisely then. Use it to bust caches, not to count "
            "uploads — it starts at 1 before the first upload."
        ),
        examples=[2],
    )
    uploaded_at: datetime | None = Field(
        default=None,
        description="When the current avatar was stored, UTC. `null` when there is none.",
        examples=["2026-08-01T12:00:00Z"],
    )
    dimensions: AvatarDimensions | None = Field(
        default=None,
        description=(
            "The stored pixel sizes. Returned **only by `POST`**, where they are known "
            "from the encode that just happened; `GET` and `DELETE` omit them rather "
            "than decoding the stored file to report them."
        ),
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "avatar_url": (
                        "http://localhost:8000/media/avatars/"
                        "019fb9ea-0a0c-7cec-9c5f-402727c31a96/DkP2n1Qe.webp?v=2"
                    ),
                    "thumbnail_url": (
                        "http://localhost:8000/media/avatars/"
                        "019fb9ea-0a0c-7cec-9c5f-402727c31a96/DkP2n1Qe_thumb.webp?v=2"
                    ),
                    "avatar_version": 2,
                    "uploaded_at": "2026-08-01T12:00:00Z",
                    "dimensions": {
                        "original": {"width": 512, "height": 341},
                        "thumbnail": {"width": 128, "height": 85},
                    },
                }
            ]
        }
    }

    @classmethod
    def of(
        cls,
        reference: AvatarReference,
        links: AvatarLinks | None,
        *,
        processed: ProcessedAvatar | None = None,
    ) -> "AvatarResponse":
        """Assembles the response from the reference and its rendered URLs.

        **Takes `links` rather than building them.** This schema has no
        `StorageProvider` and no key layout — composing a URL is
        `AvatarLinkBuilder`'s, and the router hands the result in. That is
        the requirement A64-012.2 states as "`ProfileResponse` should not
        know storage implementation details", applied to this module's own
        response for the same reason.

        `processed` is present only on the upload path, which is the only
        caller that knows the dimensions without re-decoding.
        """
        dimensions = (
            AvatarDimensions(
                original=ImageDimensions(
                    width=processed.original.width, height=processed.original.height
                ),
                thumbnail=ImageDimensions(
                    width=processed.thumbnail.width, height=processed.thumbnail.height
                ),
            )
            if processed is not None
            else None
        )

        return cls(
            avatar_url=links.avatar_url if links else None,
            thumbnail_url=links.thumbnail_url if links else None,
            avatar_version=reference.version,
            uploaded_at=reference.uploaded_at,
            dimensions=dimensions,
        )
