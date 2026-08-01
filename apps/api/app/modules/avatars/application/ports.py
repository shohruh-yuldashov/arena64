"""The ports `avatars` programs against — AD-06: declared in
`application/`, satisfied by `infrastructure/` or by another module's
published surface.

Three collaborators, and only one of them is declared here:

    ImageProcessor   this file. Satisfied by `PillowImageProcessor`.
    AvatarStore      `users.public` — the reference columns.
    StorageProvider  `app.core.storage` — the bytes.

The last two are deliberately **not** redeclared. `AvatarStore` is `users`'
type and BR-2 requires a published port be consumed as published;
`StorageProvider` is a platform port that three modules share. Re-stating
either here would be a second definition of a contract that already has an
owner, and the two would drift.
"""

from typing import Protocol

from app.modules.avatars.domain.images import ImageFormat
from app.modules.avatars.domain.renditions import ProcessedAvatar


class ImageProcessor(Protocol):
    """Turns arbitrary uploaded bytes into the two renditions this platform
    stores.

    A `Protocol`, not an ABC, so `PillowImageProcessor` and a test double
    satisfy it structurally. It exists so that no application service
    imports Pillow — which matters beyond tidiness: a service test that had
    to construct real JPEG bytes to exercise an orchestration path is a
    service test nobody writes.

    ## Why one method rather than resize / strip / encode

    Every step A64-012.2 lists — normalise orientation, strip EXIF, resize,
    thumbnail, convert, optimise — operates on the same decoded image, and
    a port that exposed them separately would decode six times and let a
    caller run them in the wrong order. Orientation in particular *must* be
    applied before resizing, or a portrait photo is fitted to a landscape
    bounding box and then rotated out of it.

    One call, one decode, one correct order, decided by the implementation
    that owns the decoder.

    ## What an implementation must guarantee

    - **No metadata survives.** EXIF, XMP, ICC and any other ancillary
      chunk. EXIF on a phone photo routinely carries GPS coordinates and a
      device serial; publishing an avatar must not publish where it was
      taken.
    - **Orientation is baked into the pixels.** The EXIF orientation tag is
      read, applied, and then discarded with the rest of the metadata — so
      a renderer that ignores the tag (most do) still shows the image the
      right way up.
    - **Neither rendition exceeds its bound**, and aspect ratio is
      preserved. A non-square upload is fitted inside the box, never
      cropped: choosing which part of somebody's picture to discard is a
      product decision nobody has made.
    - **An image smaller than the bound is not enlarged.** Upscaling a
      64x64 avatar to 512x512 produces a blurry file eight times the size
      and no more detail.
    - **Failure is `InvalidAvatarImage`**, never a decoder exception. The
      adapter translates at this boundary (services.md §7.2), so nothing
      above ever catches a Pillow type.
    """

    async def process(self, data: bytes, *, source_format: ImageFormat) -> ProcessedAvatar:
        """Decodes, sanitises and re-encodes into original and thumbnail.

        `source_format` comes from `detect_format` — the signature check —
        and is passed so the decoder can be told what to expect rather than
        sniffing again. It is **not** a promise the bytes are valid: this
        method is the stage that finds out, and raises
        `InvalidAvatarImage` when they are not.

        `async` because a real implementation does CPU-bound work that must
        not run on the event loop; see `PillowImageProcessor` on the worker
        thread.
        """
        ...
