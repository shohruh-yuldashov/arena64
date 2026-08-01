"""`AvatarService` — upload, read and delete a player's avatar.

Orchestrates; does not compute (services.md §3.2). What an acceptable image
is lives in `domain/images.py`, where it is stored lives in `domain/keys.py`,
the pixel work is `ImageProcessor`'s, the bytes go to `StorageProvider`, and
the columns are `users`' through `AvatarStore`. What lives here is the
sequencing — and on this flow the sequencing *is* the correctness argument,
because every step can fail independently and two of them are in different
systems.

Four collaborators, all injected:

    AvatarStore      the reference columns (`users.public`)
    StorageProvider  the bytes (`app.core.storage`)
    ImageProcessor   decode, sanitise, resize, encode
    Clock            AD-07 — never read directly

## Validation runs cheapest-first

    1. empty?                  a length check
    2. too large?              a length check
    3. known signature?        a few bytes compared
    4. decodable image?        a decoder invocation

The order is deliberate rather than incidental. Step 4 is the expensive and
attackable step, and everything above it exists to keep bytes away from it.
A 40 MB `.exe` renamed to `.png` is refused by step 2 without a decoder
ever seeing it; a 3 KB one is refused by step 3.

## Why nothing is stored before the image is validated

A64-012.2: "validate image before saving". The whole pipeline runs in
memory and the store is touched only once there are two valid WebP buffers
to write. A service that wrote first and validated after would need a
compensating delete on every rejection — and the rejection path is the one
that runs most often under attack, which is exactly when a compensating
delete is least likely to be reached.

## The write order, and why it cannot orphan the reference

Upload is: **store new objects → point the row at them → delete the old
objects.**

Each boundary fails in the safe direction:

  - storage fails         the row is untouched; the player keeps the avatar
                          they had. Partially-written new objects are
                          cleaned up before the error propagates.
  - the row write fails   the new objects exist but nothing references
                          them; cleaned up in the same way.
  - the old delete fails  the row is already correct and the player sees
                          the new avatar. Two orphaned objects survive,
                          logged at ERROR with their keys.

The reverse order — delete the old objects first — has no such property: a
crash after the delete leaves the row pointing at bytes that are gone, and
every profile renders a broken image until somebody notices.

**Orphaned bytes are the failure this design accepts**, and it is worth
being plain about that rather than claiming the requirement is met
absolutely. Deleting from two systems without a distributed transaction can
always be interrupted between them. What the ordering guarantees is that an
interruption never produces a *dangling reference*; what it cannot
guarantee is that no unreferenced bytes are left. Both delete paths are
idempotent so a retry finishes the job, and the ERROR log carries the keys
so a sweep is a query rather than an audit. A scheduled reconciler is the
first recommendation for A64-012.3.
"""

import logging
from uuid import UUID

from app.core.clock import Clock
from app.core.storage import StorageError, StorageProvider
from app.modules.avatars.application.ports import ImageProcessor
from app.modules.avatars.domain.exceptions import (
    AvatarNotFound,
    AvatarTooLarge,
    EmptyAvatarUpload,
    UnsupportedImageFormat,
)
from app.modules.avatars.domain.images import (
    MAX_UPLOAD_BYTES,
    STORED_CONTENT_TYPE,
    ImageFormat,
    accepted_content_types,
    detect_format,
)
from app.modules.avatars.domain.keys import AvatarKey
from app.modules.avatars.domain.renditions import ProcessedAvatar
from app.modules.users.public import AvatarReference, AvatarStore

logger = logging.getLogger(__name__)


class AvatarService:
    def __init__(
        self,
        *,
        avatars: AvatarStore,
        storage: StorageProvider,
        processor: ImageProcessor,
        clock: Clock,
    ) -> None:
        self._avatars = avatars
        self._storage = storage
        self._processor = processor
        self._clock = clock

    # --- reading -------------------------------------------------------------

    async def get_avatar(self, user_id: UUID) -> AvatarReference:
        """The caller's current avatar reference.

        Raises `AvatarNotFound` (404) when there is none. That is the one
        place this module treats "no avatar" as an error rather than a
        value: `GET /profile/avatar` asks for a resource, and answering
        `200` with a body full of nulls would make a client check three
        fields to learn what a status code says in one.

        `DELETE` deliberately does *not* raise for the same state — see
        `delete`.
        """
        reference = await self._avatars.get_avatar(user_id)

        if not reference.is_set:
            raise AvatarNotFound("This account has no avatar.")

        return reference

    # --- uploading -----------------------------------------------------------

    async def upload(self, user_id: UUID, data: bytes) -> tuple[AvatarReference, ProcessedAvatar]:
        """Validates, processes and stores a new avatar, replacing any
        existing one.

        Upload and replace are **one operation**, not two. A64-012.2 lists
        them separately, and a separate `replace` would be the same code
        with one branch — plus a way for a client to get the wrong one and
        either orphan the old objects or fail on a first upload. The
        distinction that matters is recorded where it belongs: in the log
        line, which reports whether this replaced something.

        Returns the new reference *and* the renditions, because the caller
        renders both the new URLs and the resulting dimensions, and neither
        is recoverable afterwards without a store round trip and a decode.

        Raises `EmptyAvatarUpload`, `AvatarTooLarge`,
        `UnsupportedImageFormat` or `InvalidAvatarImage` for a rejected
        file — all `422`. Raises `StorageError` (500) if the store cannot
        be written.
        """
        source_format = self._validate(user_id, data)
        processed = await self._processor.process(data, source_format=source_format)

        key = AvatarKey.generate(user_id)
        previous = await self._avatars.get_avatar(user_id)

        await self._store(key, processed)

        try:
            reference = await self._avatars.set_avatar(user_id, object_key=key.original)
        except Exception:
            # The row was not updated, so nothing references what was just
            # written. Remove it before the error propagates, or every
            # failed upload leaves two objects behind.
            await self._discard(key, reason="reference_write_failed")
            raise

        # Only now — the row points at the new objects, so a failure here
        # costs storage rather than correctness. See the module docstring.
        if previous.object_key is not None:
            await self._remove_objects(previous.object_key, user_id=user_id)

        logger.info(
            "avatar_uploaded",
            extra={
                "user_id": str(user_id),
                "avatar_version": reference.version,
                "replaced": previous.is_set,
                "source_format": source_format.value,
                "original_bytes": processed.original.byte_size,
                "thumbnail_bytes": processed.thumbnail.byte_size,
            },
        )
        return reference, processed

    # --- deleting ------------------------------------------------------------

    async def delete(self, user_id: UUID) -> AvatarReference:
        """Removes the avatar: both objects, the reference, and the cached
        copies.

        **Idempotent.** A player with no avatar gets a success, not a 404 —
        a caller retrying after a dropped response must not receive an
        error for the retry (CLAUDE.md §3 rule 8), and "there is no avatar"
        is the outcome it wanted. The version is bumped either way, which
        is what tells a CDN holding the previous URL to stop serving it.

        The row is cleared **before** the objects are removed, which is the
        opposite of the intuitive order and is the safe one: a failure
        after the clear leaves unreferenced bytes, while a failure after a
        storage-first delete would leave the row pointing at objects that
        no longer exist — and every profile rendering that player would
        show a broken image.
        """
        previous = await self._avatars.get_avatar(user_id)
        reference = await self._avatars.clear_avatar(user_id)

        if previous.object_key is not None:
            await self._remove_objects(previous.object_key, user_id=user_id)

        logger.info(
            "avatar_deleted",
            extra={
                "user_id": str(user_id),
                "avatar_version": reference.version,
                "had_avatar": previous.is_set,
            },
        )
        return reference

    # --- internals -----------------------------------------------------------

    def _validate(self, user_id: UUID, data: bytes) -> ImageFormat:
        """The three cheap checks, in cost order. Returns the detected
        format; raises for anything refused.

        Every rejection is logged, because A64-012.2 requires validation
        failures to be recorded and because the *rate* of them is the only
        signal that somebody is probing the endpoint with crafted files.
        None of the logs carries the bytes, a filename, or a temporary
        path.
        """
        if not data:
            logger.info("avatar_rejected", extra={"user_id": str(user_id), "reason": "empty"})
            raise EmptyAvatarUpload("No file was uploaded.")

        if len(data) > MAX_UPLOAD_BYTES:
            logger.info(
                "avatar_rejected",
                extra={
                    "user_id": str(user_id),
                    "reason": "too_large",
                    "byte_size": len(data),
                },
            )
            raise AvatarTooLarge(
                f"The image is larger than the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit."
            )

        source_format = detect_format(data)
        if source_format is None:
            # The *declared* content type is not consulted here or
            # anywhere — see `domain/images.py`. What is logged is that the
            # signature was unrecognised, never the bytes that failed.
            logger.info(
                "avatar_rejected",
                extra={"user_id": str(user_id), "reason": "unsupported_signature"},
            )
            raise UnsupportedImageFormat(
                "Unsupported image format. Accepted formats: "
                f"{', '.join(accepted_content_types())}."
            )

        return source_format

    async def _store(self, key: AvatarKey, processed: ProcessedAvatar) -> None:
        """Writes both renditions, cleaning up if the second fails.

        Without the cleanup, a store that accepted the original and refused
        the thumbnail would leave one orphan on every such failure — and
        the caller would see an error suggesting nothing had been written.
        """
        await self._storage.save(
            key.original, processed.original.data, content_type=STORED_CONTENT_TYPE
        )

        try:
            await self._storage.save(
                key.thumbnail, processed.thumbnail.data, content_type=STORED_CONTENT_TYPE
            )
        except Exception:
            await self._discard(key, reason="thumbnail_write_failed")
            raise

    async def _discard(self, key: AvatarKey, *, reason: str) -> None:
        """Best-effort removal of objects nothing references yet.

        Never raises: it runs while another error is already propagating,
        and replacing that error with a cleanup failure would hide the
        cause. A failure here is logged at ERROR with both keys, because
        what it leaves behind is exactly what a sweeper needs to find.
        """
        for object_key in key.keys:
            try:
                await self._storage.delete(object_key)
            except StorageError:
                logger.error(
                    "avatar_orphaned_object",
                    extra={"object_key": object_key, "reason": reason},
                )

    async def _remove_objects(self, original_key: str, *, user_id: UUID) -> None:
        """Removes a stored original and its thumbnail, best-effort.

        Never raises. Both callers reach it *after* the database is already
        correct, so a storage failure here costs unreferenced bytes rather
        than correctness — and turning it into a 500 would tell a caller
        their upload failed when it succeeded.

        A key this platform did not write cannot have its thumbnail
        derived, so the original is removed alone and the anomaly is logged.
        Refusing outright would make such a row's avatar undeletable, which
        is worse than one orphaned derivative.
        """
        key = AvatarKey.from_object_key(original_key)

        if key is None:
            logger.error(
                "avatar_key_unrecognised",
                extra={"user_id": str(user_id), "object_key": original_key},
            )
            targets: tuple[str, ...] = (original_key,)
        else:
            # Thumbnail first: if only one delete lands, the derivative is
            # the one better lost. Nothing references it directly, whereas
            # the original is the key that was stored in the row.
            targets = (key.thumbnail, key.original)

        for object_key in targets:
            try:
                await self._storage.delete(object_key)
            except StorageError:
                # ERROR rather than WARNING: this is the one failure mode
                # that leaves the platform paying for bytes nothing points
                # at, and the key logged here is what makes a sweep a query
                # rather than a full-bucket audit.
                logger.error(
                    "avatar_orphaned_object",
                    extra={"object_key": object_key, "reason": "cleanup_failed"},
                )
