"""What this platform will accept as an avatar, and how it decides.

Framework-free (architecture.md §8): no Pillow, no FastAPI, no filesystem.
Deciding *whether bytes are an image of an accepted kind* needs nothing but
the bytes, and keeping that here means the rule is testable without a
decoder and cannot be quietly relaxed inside an adapter.

## Why the Content-Type header is never consulted

A64-012.2 states it as a requirement and it is worth recording why rather
than only that. `Content-Type` on a multipart part is a **client-supplied
string**. It is trivially set to `image/png` on a PHP script, an ELF
binary, or a ZIP archive, and a server that believes it is one `.php` away
from serving executable content out of its own asset host.

So the declared type is ignored entirely — not compared, not warned about.
What decides is the first few bytes of the file, which the encoder wrote
and the uploader would have to construct a genuine image to forge.

`detect_format` is the whole of that check at this layer. It answers "do
these bytes *claim* to be one of the four accepted formats", and it is
deliberately not the last word: `PillowImageProcessor` then decodes the
image, which is what turns "starts with the right eight bytes" into "is
actually a decodable PNG". A signature check alone would pass a file whose
header is a PNG and whose body is anything at all.

## Why the accepted set is these four

JPEG, PNG and WebP are what browsers produce from a file picker and what
phones produce from a camera. Everything is re-encoded to WebP on the way
in, so the accepted *input* set exists only to bound what the decoder is
asked to parse — and every format on it is one Pillow parses well.

Notably absent: GIF (animation is a moderation surface and a
frame-multiplied decode cost), SVG (an XML document that can carry script
and remote references — not an image in any sense that matters here), TIFF
and BMP (rare from a browser, and TIFF in particular is a historically
rich source of decoder CVEs).
"""

from enum import StrEnum
from typing import Final

#: 5 MB, A64-012.2's figure, in bytes rather than a computed expression so
#: that the number in the code is the number in the API documentation.
#:
#: This bound is enforced **before** any decoding, which is the point: an
#: image decoder is the expensive, attackable part, and the cheapest
#: possible rejection of an oversized upload is one that never reaches it.
MAX_UPLOAD_BYTES: Final[int] = 5 * 1024 * 1024

#: The longest edge of the stored original. A64-012.2's figure.
#:
#: Applied as a *bounding box* rather than a target: a 2000x1000 image
#: becomes 512x256, not 512x512. Cropping to a square would silently
#: discard part of what somebody uploaded, and deciding *which* part is a
#: product question nobody has answered — see `ImageProcessor`.
MAX_DIMENSION: Final[int] = 512

#: The longest edge of the generated thumbnail. A64-012.2's figure.
THUMBNAIL_DIMENSION: Final[int] = 128

#: WebP quality for both renditions.
#:
#: 82 is the knee of the curve for photographic content: visually
#: indistinguishable from 95 at roughly half the bytes, and well above the
#: ~70 where ringing becomes visible on the flat colour and hard edges that
#: illustrated avatars are full of. Chosen once, here, rather than passed
#: per call — a per-caller quality is a per-caller decision about how the
#: platform looks.
WEBP_QUALITY: Final[int] = 82

#: The stored format for every avatar, whatever was uploaded.
#:
#: One output format means one decoder path for browsers, one content type,
#: and a predictable size — and WebP is 25-35% smaller than equivalent-quality
#: JPEG with alpha support PNG-sized files would otherwise be needed for.
#: Universally supported by every browser this platform targets.
STORED_CONTENT_TYPE: Final[str] = "image/webp"
STORED_EXTENSION: Final[str] = "webp"

#: The largest decoded pixel count this platform will accept, as a
#: **decompression-bomb** guard.
#:
#: The 5 MB byte limit does not bound this: a maliciously crafted PNG of a
#: single flat colour compresses ~10000:1, so a 5 MB upload can decode to
#: tens of gigabytes of RGBA and take the process down before any resize
#: runs. Pillow has its own `MAX_IMAGE_PIXELS` warning threshold; this is
#: the platform's own limit, applied as a hard refusal.
#:
#: 50 megapixels is roughly an 8000x6000 image — far beyond anything a
#: 512px avatar needs, and comfortably above what a modern phone camera
#: produces, so no legitimate upload meets it.
MAX_DECODED_PIXELS: Final[int] = 50_000_000


class ImageFormat(StrEnum):
    """An accepted *input* format. Never the stored format — everything is
    re-encoded to WebP."""

    JPEG = "jpeg"
    PNG = "png"
    WEBP = "webp"

    @property
    def content_type(self) -> str:
        """The type a browser would have sent. Reported in errors and used
        nowhere else — it is never trusted as input."""
        return f"image/{self.value}"


#: File signatures ("magic bytes"), longest first so a prefix cannot mask a
#: longer match.
#:
#: WebP is the awkward one and is handled separately: its signature is
#: `RIFF` at offset 0 *and* `WEBP` at offset 8, with a four-byte length in
#: between. Matching only `RIFF` would accept a WAV file, which is the same
#: container family.
_SIGNATURES: Final[tuple[tuple[bytes, ImageFormat], ...]] = (
    (b"\x89PNG\r\n\x1a\n", ImageFormat.PNG),
    # JPEG: SOI marker. The third byte varies by encoder (`\xe0` JFIF,
    # `\xe1` Exif, `\xdb` raw quantisation table), so only the two-byte
    # start-of-image marker is stable enough to match on.
    (b"\xff\xd8\xff", ImageFormat.JPEG),
)

_RIFF_MAGIC: Final[bytes] = b"RIFF"
_WEBP_MAGIC: Final[bytes] = b"WEBP"
_WEBP_MAGIC_OFFSET: Final[int] = 8

#: Enough bytes to decide. The longest signature check reads to offset 12.
SIGNATURE_PROBE_BYTES: Final[int] = 16


def detect_format(data: bytes) -> ImageFormat | None:
    """The format these bytes *claim* to be, or `None`.

    Reads only the leading bytes — this is a signature check, not a parse.
    A file that passes is not yet known to be a valid image; that is
    `ImageProcessor`'s job, and the two-step order matters because decoding
    is the expensive and attackable half. Rejecting a `.exe` costs a
    comparison here rather than a decoder invocation there.

    Returns `None` for anything unrecognised, including an empty buffer.
    Deliberately not raising: "what is this" is a question with a legitimate
    negative answer, and the caller turns it into the typed rejection with
    the right message.

    **Executables are rejected by this returning `None`**, not by a
    blocklist. A64-012.2 lists them explicitly, and an allowlist of four
    signatures excludes ELF, Mach-O, PE, shell scripts and everything else
    without having to enumerate them — which is the only way that
    enumeration ever stays complete.
    """
    if len(data) < len(_RIFF_MAGIC):
        return None

    for signature, image_format in _SIGNATURES:
        if data.startswith(signature):
            return image_format

    # WebP: `RIFF` then a 4-byte size then `WEBP`. Both halves are checked,
    # because `RIFF` alone is also WAV and AVI.
    if data.startswith(_RIFF_MAGIC) and (
        data[_WEBP_MAGIC_OFFSET : _WEBP_MAGIC_OFFSET + len(_WEBP_MAGIC)] == _WEBP_MAGIC
    ):
        return ImageFormat.WEBP

    return None


def accepted_content_types() -> tuple[str, ...]:
    """The content types a client may usefully send, for documentation and
    for the `accept` attribute of a file input.

    Advisory only. Nothing on the upload path compares an incoming header
    against this — see this module's docstring on why the header is never
    trusted. It exists so the OpenAPI description and the error message
    naming the accepted formats are generated from the same enum the
    validator uses, rather than retyped and left to drift.
    """
    return tuple(image_format.content_type for image_format in ImageFormat)
