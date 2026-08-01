"""`LocalStorageProvider` — objects on a local disk.

The only `StorageProvider` A64-012.2 ships, and a **development
transport**. It is the storage equivalent of `ConsoleEmailProvider`: it
makes the whole avatar flow exercisable on a laptop with no cloud account,
and it refuses to construct in a production-like environment for the same
reason that one does.

Adding S3, R2, MinIO or GCS is a new class in this package plus a branch in
one dependency factory. No service, no domain type and no schema changes —
that is the requirement A64-012.2 states as "architecture must support
future providers without changing business logic", and this file is the
proof it holds, because nothing above `StorageProvider` imports it.

## Why every write goes through a temporary file and a rename

`save` writes to `<key>.<random>.tmp` in the destination directory and then
`os.replace`s it onto the final name.

A plain `open(key, "wb").write(data)` is not atomic: a crash, a full disk
or a killed process mid-write leaves a truncated file that `exists`
reports as present and a browser renders as a broken image. `os.replace`
is atomic *within a filesystem*, which is why the temporary lives in the
destination directory rather than in `/tmp` — a rename across filesystems
silently degrades to copy-then-delete and loses the property.

This is the local stand-in for a behaviour object stores give for free: an
S3 `PutObject` is atomic, and a reader sees either the old object or the
new one, never half of either.

## Why the disk write happens on a worker thread

`anyio.to_thread.run_sync`, exactly as `Argon2idPasswordHasher` does.
Writing a few hundred kilobytes is milliseconds of blocking syscall, and
blocking the event loop for milliseconds per upload stalls every other
request on the process. The same reasoning, at a smaller scale, and the
same solution.

## Path traversal

`_resolve` rejects any key that escapes the configured root, and it does so
by comparison *after* resolution rather than by inspecting the string —
`..`, absolute keys, symlinks and encoded separators all reduce to the same
check. Keys on this platform are generated from UUIDs and never from user
input, so nothing should ever reach it; it is here because "should never"
is what every traversal advisory says about the code it was found in.
"""

import logging
import os
import secrets
from pathlib import Path

from anyio import to_thread

from app.config.environment import Environment
from app.config.settings import StorageSettings
from app.core.storage import KEY_SEPARATOR, StorageError

logger = logging.getLogger(__name__)

#: Permissions for directories this provider creates: owner-only.
#:
#: Avatars are public *content*, but the directory holding them is not a
#: place a shared development machine should let other accounts write —
#: an attacker who can write here can replace any player's avatar with any
#: bytes, and the store serves them without revalidation.
_DIRECTORY_MODE = 0o700

#: Bytes of randomness in a temporary file's suffix. Enough that two
#: concurrent uploads to the same key cannot collide on the temporary and
#: clobber each other before either rename lands.
_TEMPORARY_SUFFIX_BYTES = 8


class LocalStorageProvider:
    """Stores objects as files under a root directory.

    Constructed once per process — it holds a resolved root and nothing
    else, and every method is safe to call concurrently.
    """

    def __init__(self, settings: StorageSettings, environment: Environment) -> None:
        if environment.is_production_like:
            # DI-06's enforcement point, and the same guard
            # `ConsoleEmailProvider` carries. A deployed tier on local
            # storage would (a) lose every avatar the first time a
            # container is rescheduled, and (b) serve nothing at all from
            # a second replica, because the file only exists on the node
            # that received the upload. Both failures are silent — uploads
            # succeed — which is what makes refusing to start the right
            # response rather than a warning.
            raise ValueError(
                f"LocalStorageProvider must not be used in {environment} — objects "
                "live on one node's disk and are lost on reschedule. Configure an "
                "object-storage provider for deployed tiers."
            )

        self._root = Path(settings.local_root).resolve()
        self._public_base_url = settings.public_base_url.rstrip("/")
        self._environment = environment

    # --- key handling --------------------------------------------------------

    def _resolve(self, key: str) -> Path:
        """Maps an object key onto a path inside the root, or refuses.

        The refusal is by *resolved location*, not by pattern: anything
        that lands outside the root is rejected however it got there. A
        blocklist of `..` would miss absolute keys, encoded separators and
        symlinks, each of which reduces to the same question this asks.

        The error deliberately does not include the resolved path — see
        this class's `save` on why a filesystem path never reaches a log or
        a response (A64-012.2: "never expose filesystem paths").
        """
        if not key or key.startswith(KEY_SEPARATOR):
            raise StorageError("Invalid object key.")

        candidate = (self._root / key).resolve()

        if candidate != self._root and self._root not in candidate.parents:
            logger.error("storage_key_escaped_root")
            raise StorageError("Invalid object key.")

        return candidate

    # --- operations ----------------------------------------------------------

    async def save(self, key: str, data: bytes, *, content_type: str) -> None:
        """Writes atomically — temporary file, then rename. See the module
        docstring.

        `content_type` is accepted and **not stored**: a filesystem has no
        object metadata, so the local provider cannot carry it. That is a
        real difference from every cloud provider and it is why development
        serves avatars through `StaticFiles`, which infers the type from
        the `.webp` extension the key already carries. Recorded rather than
        silently dropped, because a provider that ignored it *and* did not
        say so would leave the first S3 deployment wondering why headers
        changed.
        """
        destination = self._resolve(key)

        def _write() -> None:
            destination.parent.mkdir(parents=True, exist_ok=True, mode=_DIRECTORY_MODE)
            temporary = destination.with_name(
                f"{destination.name}.{secrets.token_hex(_TEMPORARY_SUFFIX_BYTES)}.tmp"
            )
            try:
                temporary.write_bytes(data)
                # Atomic within a filesystem, and the temporary is in the
                # destination directory precisely so it is one.
                os.replace(temporary, destination)
            except OSError:
                # Best-effort cleanup so a failed write does not leave a
                # `.tmp` behind. `missing_ok` because the failure may have
                # been the write itself.
                temporary.unlink(missing_ok=True)
                raise

        try:
            await to_thread.run_sync(_write)
        except OSError as error:
            # The key is logged, the path is not. A key is an opaque
            # address the platform generated; a path discloses the
            # deployment's filesystem layout to anyone who can read a log.
            logger.error("storage_save_failed", extra={"object_key": key})
            raise StorageError("Could not store the object.") from error

    async def delete(self, key: str) -> None:
        """Idempotent — a missing object is a success.

        See the port: `AvatarService.delete` removes two objects, and a
        crash between them must leave the operation safely repeatable.
        """
        target = self._resolve(key)

        def _unlink() -> None:
            target.unlink(missing_ok=True)

        try:
            await to_thread.run_sync(_unlink)
        except OSError as error:
            logger.error("storage_delete_failed", extra={"object_key": key})
            raise StorageError("Could not delete the object.") from error

    async def exists(self, key: str) -> bool:
        target = self._resolve(key)
        return await to_thread.run_sync(target.is_file)

    def get_public_url(self, key: str) -> str:
        """Composes the URL `StaticFiles` serves this object at.

        Synchronous and I/O-free, per the port. The result is a **URL**,
        and the fact that it happens to resemble the on-disk layout is a
        property of this provider only — an S3 provider composes the same
        key onto a bucket host, and neither caller nor client can tell the
        difference.

        No existence check: a caller holding a key already stored one, and
        a stat per rendered avatar would put a syscall on every profile
        page.
        """
        return f"{self._public_base_url}/{key.lstrip(KEY_SEPARATOR)}"

    # --- development wiring --------------------------------------------------

    @property
    def root(self) -> Path:
        """The directory objects live under.

        Exists for exactly one caller — `app_factory`, which mounts
        `StaticFiles` here so a browser can fetch what was uploaded. It is
        **not** part of `StorageProvider` and no service can reach it: a
        port with a `root` would be a port every provider has to pretend to
        have, and S3 has no such thing.
        """
        return self._root
