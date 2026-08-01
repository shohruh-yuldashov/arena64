"""`PillowImageProcessor` — the one place this platform decodes an image.

The `ImageProcessor` port's only implementation, and deliberately the only
module in the codebase that imports PIL. An image decoder is the most
attackable component in an upload path — decades of CVEs across every
library — so confining it to one file means the guards below are in one
place rather than distributed across whoever needed to open an image.

## The pipeline, in the order it must run

    1. bound the decoded size    before decoding anything — the
                                 decompression-bomb guard
    2. decode                    inside a worker thread
    3. apply EXIF orientation    *before* resizing, or a portrait photo is
                                 fitted to a landscape box and then rotated
                                 out of it
    4. flatten to a safe mode    palette and CMYK images cannot be encoded
                                 to WebP directly
    5. resize to 512 box         aspect preserved, never enlarged
    6. resize to 128 box         from the *original*, not from the 512 —
                                 see below
    7. encode both to WebP       which drops every metadata chunk

## Why the thumbnail is made from the source, not from the 512px rendition

Chaining resizes compounds the loss: a 128px image derived from an
already-downscaled 512px one is visibly softer than one derived from the
original, because the first resample's low-pass filtering is applied twice.
Both renditions come from the same decoded image, which costs one extra
resample and no extra decode.

## Why the whole thing runs on a worker thread

`anyio.to_thread.run_sync`, exactly as `Argon2idPasswordHasher` and
`LocalStorageProvider` do. Decoding and resampling a multi-megapixel image
is tens to hundreds of milliseconds of pure CPU with the GIL held; on the
event loop that stalls every other request in the process. The same
reasoning, the same solution.

## Why metadata removal is by omission rather than by deletion

Nothing here calls `del image.info["exif"]`. The image is decoded to raw
pixels, transformed, and encoded into a **new** WebP buffer, and the
encoder is simply never handed the metadata. That is stronger than
stripping: a strip has to enumerate every chunk that might carry something
(EXIF, XMP, IPTC, ICC, comment blocks, and whatever the next format adds),
and the one it forgets is the one that leaks. Re-encoding from pixels
cannot forget, because there is nothing to forget from.

The one exception is deliberate: `ImageOps.exif_transpose` reads the
orientation tag *before* the pixels are taken, so the rotation survives
into the pixel data while the tag itself does not.
"""

import io
import logging

from anyio import to_thread
from PIL import Image, ImageOps, UnidentifiedImageError

from app.modules.avatars.domain.exceptions import InvalidAvatarImage
from app.modules.avatars.domain.images import (
    MAX_DECODED_PIXELS,
    MAX_DIMENSION,
    THUMBNAIL_DIMENSION,
    WEBP_QUALITY,
    ImageFormat,
)
from app.modules.avatars.domain.renditions import ProcessedAvatar, Rendition

logger = logging.getLogger(__name__)

#: The mode every rendition is flattened to before encoding.
#:
#: `RGBA` rather than `RGB`, because PNG and WebP avatars legitimately carry
#: transparency and flattening it onto a background would put a white (or
#: black) box around every rounded-corner logo somebody uploads. WebP
#: supports alpha, so there is no reason to discard it.
_ENCODE_MODE = "RGBA"

#: Pillow's own bomb warning threshold. Raised to the platform's limit so
#: Pillow does not emit a `DecompressionBombWarning` for images this
#: platform has already decided to refuse outright — the refusal below is
#: the control, and a warning alongside it is noise.
Image.MAX_IMAGE_PIXELS = MAX_DECODED_PIXELS


class PillowImageProcessor:
    """Stateless — holds nothing, safe to share across requests."""

    async def process(self, data: bytes, *, source_format: ImageFormat) -> ProcessedAvatar:
        """See `ImageProcessor.process`. Raises `InvalidAvatarImage` for
        anything the decoder will not read."""
        return await to_thread.run_sync(self._process, data, source_format)

    def _process(self, data: bytes, source_format: ImageFormat) -> ProcessedAvatar:
        try:
            with Image.open(io.BytesIO(data)) as image:
                # Before any pixel is touched. `Image.open` is lazy — it
                # reads the header and defers decoding — so `size` is known
                # while the multi-gigabyte allocation has not happened yet.
                # This is the only moment a decompression bomb can be
                # refused cheaply.
                self._reject_oversized(image, source_format)

                # Orientation first: `exif_transpose` reads the EXIF tag and
                # rotates the pixels to match. Doing it after the resize
                # would fit a portrait photo into a landscape box and then
                # turn it sideways.
                #
                # Returns a new image; the tag does not survive into it,
                # which is exactly what is wanted — the rotation is now in
                # the pixels and needs no tag to be rendered correctly.
                oriented = ImageOps.exif_transpose(image) or image

                # Palette (`P`) and CMYK images cannot be encoded to WebP
                # directly, and `LA`/`L` greyscale would encode but lose the
                # alpha channel's meaning. One conversion covers all of
                # them.
                if oriented.mode != _ENCODE_MODE:
                    oriented = oriented.convert(_ENCODE_MODE)

                # Both from the same source image — see the module
                # docstring on why the thumbnail is not derived from the
                # 512px rendition.
                original = self._encode(oriented, MAX_DIMENSION)
                thumbnail = self._encode(oriented, THUMBNAIL_DIMENSION)

        except InvalidAvatarImage:
            raise
        except (UnidentifiedImageError, OSError, ValueError) as error:
            # Everything Pillow raises for a file it cannot read: a
            # truncated stream, a corrupt chunk, an unsupported subformat.
            # Translated here so nothing above catches a PIL type
            # (services.md §7.2).
            #
            # The exception is logged with its *type* only. The message can
            # contain fragments of the file, and the file is somebody's
            # photograph.
            logger.info(
                "avatar_decode_failed",
                extra={"reason": type(error).__name__, "declared_format": source_format.value},
            )
            raise InvalidAvatarImage(
                "The file could not be read as an image. It may be corrupt or incomplete."
            ) from error

        return ProcessedAvatar(original=original, thumbnail=thumbnail)

    @staticmethod
    def _reject_oversized(image: Image.Image, source_format: ImageFormat) -> None:
        """Refuses a decompression bomb before it is decoded.

        The 5 MB byte limit does not bound this. A PNG of one flat colour
        compresses at roughly 10000:1, so a file well under the limit can
        decode to tens of gigabytes of RGBA — enough to take the process
        down before any resize runs.
        """
        width, height = image.size
        if width * height > MAX_DECODED_PIXELS:
            logger.warning(
                "avatar_rejected_oversized_dimensions",
                extra={
                    "width": width,
                    "height": height,
                    "declared_format": source_format.value,
                },
            )
            raise InvalidAvatarImage(
                f"Image dimensions are too large: {width}x{height}. "
                f"At most {MAX_DECODED_PIXELS // 1_000_000} megapixels."
            )

    @staticmethod
    def _encode(image: Image.Image, bound: int) -> Rendition:
        """Fits a copy inside `bound` x `bound` and encodes it to WebP.

        `thumbnail` is Pillow's bounding-box resize: it preserves aspect
        ratio and — importantly — **never enlarges**. An image already
        smaller than the bound is left alone, so a 64x64 upload stays
        64x64 rather than becoming a blurry 512x512 file eight times the
        size.

        `LANCZOS` rather than the default: it is the highest-quality
        downsampling filter Pillow offers, and downsampling is the only
        operation performed here. The cost is milliseconds on an image this
        small.

        The copy is not optional. `thumbnail` mutates in place, so encoding
        the 512px rendition and then the 128px one from the same object
        would produce a 128px "original".
        """
        rendition = image.copy()
        rendition.thumbnail((bound, bound), Image.Resampling.LANCZOS)

        buffer = io.BytesIO()
        rendition.save(
            buffer,
            format="WEBP",
            quality=WEBP_QUALITY,
            # Spends more encoder time searching for a smaller file. An
            # avatar is written once and served indefinitely, so the trade
            # is heavily in favour of the bytes.
            method=6,
            # No `exif=`, no `icc_profile=`, no `xmp=`. Metadata is dropped
            # by never being passed — see the module docstring on why that
            # is stronger than stripping.
        )

        return Rendition(data=buffer.getvalue(), width=rendition.width, height=rendition.height)
