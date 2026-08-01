"""`ProcessedAvatar` — what comes out of the processing pipeline.

Framework-free (architecture.md §8): two byte strings and their dimensions,
with no knowledge of how they were produced or where they will go.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Rendition:
    """One encoded image and the size it came out at.

    The dimensions are carried rather than re-derived, because the only way
    to recover them from the bytes is to decode the image again — and this
    platform decodes an uploaded image exactly once, on purpose.

    They are reported in the upload response so a client can lay out an
    `<img>` without waiting for the file, and they are what a test asserts
    the resize against.
    """

    data: bytes = field(repr=False)
    """`repr=False` — a dataclass repr lands in tracebacks and error
    reporters, and this field is up to 5 MB of somebody's photograph
    (services.md §8.5). A64-012.2 states it directly: never log image
    contents."""

    width: int
    height: int

    @property
    def byte_size(self) -> int:
        return len(self.data)


@dataclass(frozen=True, slots=True)
class ProcessedAvatar:
    """The two renditions this platform stores for every avatar.

    Both are WebP, both are stripped of metadata, and both came from one
    decode of the uploaded file. Kept together in one object so a caller
    cannot store the original and forget the thumbnail — which is the
    failure that leaves a profile rendering a 512px image into a 32px slot
    on every listing.
    """

    original: Rendition
    thumbnail: Rendition
