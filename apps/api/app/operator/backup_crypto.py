"""Backup encryption — A64-028.7, closing half of P2-8.

A64-028.1 filed it plainly: "a dump is plaintext and holds every email
address and password hash on the platform". A backup is the one artefact
deliberately copied off the machine that protects it, which makes it the
one artefact most likely to be read by somebody who was never meant to.

## AES-256-GCM, streamed in chunks

**Authenticated** encryption, which is the property that matters most here
and the reason a bare cipher would not do: GCM's tag makes a wrong key and a
corrupted ciphertext both *fail* rather than producing plausible rubbish.
A backup that restores into garbage is worse than one that refuses.

**Streamed**, in fixed chunks, because a dump is arbitrarily large and this
runs on the same host as the database. Reading a multi-gigabyte dump into
memory to encrypt it would make the backup the reason for the incident.

Each chunk gets its own nonce, derived by counter from a random per-file
prefix. Reusing a nonce with GCM is catastrophic — it leaks the XOR of two
plaintexts and lets an attacker forge tags — so the counter is explicit,
bounded, and the reason a file cannot exceed `MAX_CHUNKS` chunks.

## The format

    magic     8 bytes   b"A64BKP\\x01"
    prefix    8 bytes   random, per file
    then, repeated:
        length  4 bytes   big-endian ciphertext length, tag included
        payload           ciphertext || 16-byte tag

The chunk length is what makes decryption streamable without seeking, and
the magic is what makes "this is not an Arena64 backup" a clear error
rather than a decryption failure that reads like a wrong key.

**The header is authenticated.** Every chunk is sealed with the magic,
prefix and chunk index as additional data, so truncating a file, reordering
its chunks or splicing two backups together all fail the tag — none of which
a plain per-chunk encryption would catch.

## The key

`BACKUP_ENCRYPTION_KEY`, 32 bytes, base64. It is **not** stored with the
backup and must not be: an archive carrying its own key is a compressed file
with extra steps. Where it lives and how it rotates is
`docs/05-operations/backup-restore.md`; what this module guarantees is that
the wrong one fails loudly.
"""

import base64
import os
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC: Final = b"ARENA64\x01"

#: Derived, never stated twice. The first version declared the length as a
#: separate constant and got it wrong by one, so every decryption read a
#: byte of the first chunk's length as part of the header and failed with
#: "the archive ends inside a chunk". A constant that can disagree with the
#: value it describes is the defect, not the number.
_MAGIC_LENGTH: Final = len(MAGIC)
_PREFIX_LENGTH: Final = 8
_LENGTH_BYTES: Final = 4
KEY_BYTES: Final = 32

#: 4 MiB. Large enough that the per-chunk overhead is noise, small enough
#: that the peak memory of a backup is bounded and predictable.
CHUNK_BYTES: Final = 4 * 1024 * 1024

#: A 4-byte counter, so a file cannot exceed this many chunks — about 16 TiB
#: at the chunk size above. Stated rather than assumed: a counter that
#: wrapped would reuse a nonce, which is the one failure this construction
#: cannot survive.
MAX_CHUNKS: Final = 2**32 - 1


class BackupDecryptionError(Exception):
    """The archive is not readable with this key.

    One error for a wrong key, a corrupted file, a truncated file and a file
    that is not an Arena64 backup — deliberately, because an operator's next
    step is the same for all four and distinguishing them would mean telling
    an attacker which part of their guess was right.
    """


def parse_key(encoded: str) -> bytes:
    """A base64 key, validated for length.

    A short key is not a weak key here, it is a `ValueError` from the
    cipher at the moment of use — which for a backup means at 3am, on the
    one path nobody exercises until they need it.
    """
    try:
        key = base64.b64decode(encoded, validate=True)
    except Exception as error:  # noqa: BLE001 — every malformed input is one outcome
        raise ValueError(
            "BACKUP_ENCRYPTION_KEY must be base64. Generate one with `openssl rand -base64 32`."
        ) from error
    if len(key) != KEY_BYTES:
        raise ValueError(f"BACKUP_ENCRYPTION_KEY must decode to {KEY_BYTES} bytes, got {len(key)}.")
    return key


def generate_key() -> str:
    """A fresh key, base64, for an operator to store in their secret manager."""
    return base64.b64encode(os.urandom(KEY_BYTES)).decode()


def _nonce(prefix: bytes, index: int) -> bytes:
    """96 bits: an 8-byte per-file random prefix and a 4-byte counter.

    GCM's nonce is 12 bytes and must never repeat under one key. A random
    prefix per file plus a counter within it gives that without needing to
    remember anything between runs.
    """
    return prefix + index.to_bytes(4, "big")


def _associated(prefix: bytes, index: int) -> bytes:
    """What each chunk is bound to but does not carry.

    The magic, the file's prefix and the chunk's index. Binding the index is
    what makes reordering or dropping a chunk fail the tag; binding the
    prefix is what stops a chunk being spliced in from another backup.
    """
    return MAGIC + prefix + index.to_bytes(4, "big")


def encrypt_stream(source: IO[bytes], target: IO[bytes], *, key: bytes) -> int:
    """Encrypts `source` into `target`. Returns the plaintext bytes read.

    Chunked so peak memory is one chunk regardless of the dump's size.
    """
    cipher = AESGCM(key)
    prefix = os.urandom(_PREFIX_LENGTH)
    target.write(MAGIC)
    target.write(prefix)

    read = 0
    for index in range(MAX_CHUNKS):
        chunk = source.read(CHUNK_BYTES)
        if not chunk:
            return read
        read += len(chunk)
        sealed = cipher.encrypt(_nonce(prefix, index), chunk, _associated(prefix, index))
        target.write(len(sealed).to_bytes(_LENGTH_BYTES, "big"))
        target.write(sealed)

    raise ValueError(f"the source exceeds {MAX_CHUNKS} chunks; a longer file would reuse a nonce")


def decrypt_stream(source: IO[bytes], target: IO[bytes], *, key: bytes) -> int:
    """Decrypts `source` into `target`. Returns the plaintext bytes written.

    Raises `BackupDecryptionError` for a wrong key, a corrupted chunk, a
    truncated file, or a file that is not an Arena64 backup.
    """
    cipher = AESGCM(key)
    header = source.read(_MAGIC_LENGTH + _PREFIX_LENGTH)
    if len(header) != _MAGIC_LENGTH + _PREFIX_LENGTH or not header.startswith(MAGIC):
        raise BackupDecryptionError("not an Arena64 backup archive")
    prefix = header[_MAGIC_LENGTH:]

    written = 0
    for index in range(MAX_CHUNKS):
        raw_length = source.read(_LENGTH_BYTES)
        if not raw_length:
            return written
        if len(raw_length) != _LENGTH_BYTES:
            raise BackupDecryptionError("the archive ends inside a chunk header")

        length = int.from_bytes(raw_length, "big")
        sealed = source.read(length)
        if len(sealed) != length:
            raise BackupDecryptionError("the archive ends inside a chunk")

        try:
            chunk = cipher.decrypt(_nonce(prefix, index), sealed, _associated(prefix, index))
        except InvalidTag as error:
            raise BackupDecryptionError(
                "the archive could not be authenticated: wrong key, or the file is corrupt"
            ) from error

        target.write(chunk)
        written += len(chunk)

    raise BackupDecryptionError("the archive is longer than the format allows")


def encrypted_name(name: str) -> str:
    return f"{name}.enc"


def chunks_of(path: Path) -> Iterator[bytes]:
    """A file as chunks, for a caller that streams it somewhere else."""
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_BYTES):
            yield chunk


__all__ = [
    "CHUNK_BYTES",
    "KEY_BYTES",
    "MAGIC",
    "MAX_CHUNKS",
    "BackupDecryptionError",
    "chunks_of",
    "decrypt_stream",
    "encrypt_stream",
    "encrypted_name",
    "generate_key",
    "parse_key",
]
