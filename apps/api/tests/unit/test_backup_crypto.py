"""Backup encryption — A64-028.7, closing half of P2-8.

A backup is the one artefact deliberately copied off the machine that
protects it, so the properties that matter are the ones an attacker or a
corrupted disk would test: a wrong key must fail, a flipped bit must fail,
a truncated file must fail, and two backups must not be spliceable.

Every one of those is `InvalidTag` under the hood. The tests are here
because "we used AES-GCM" is not the same claim as "these four things fail".
"""

import base64
import io
import os

import pytest

from app.operator import backup_crypto as crypto


def _roundtrip(plaintext: bytes, key: bytes) -> bytes:
    sealed = io.BytesIO()
    crypto.encrypt_stream(io.BytesIO(plaintext), sealed, key=key)
    opened = io.BytesIO()
    crypto.decrypt_stream(io.BytesIO(sealed.getvalue()), opened, key=key)
    return opened.getvalue()


@pytest.fixture
def key() -> bytes:
    return crypto.parse_key(crypto.generate_key())


class TestRoundTrip:
    @pytest.mark.parametrize(
        "size",
        [0, 1, 1024, crypto.CHUNK_BYTES - 1, crypto.CHUNK_BYTES, crypto.CHUNK_BYTES + 1],
        ids=["empty", "one-byte", "small", "chunk-minus-one", "exact-chunk", "chunk-plus-one"],
    )
    def test_the_plaintext_survives(self, key: bytes, size: int) -> None:
        """The boundaries are the interesting sizes: a chunked format's bugs
        live at exactly one chunk and one byte either side of it."""
        plaintext = os.urandom(size)

        assert _roundtrip(plaintext, key) == plaintext

    def test_the_ciphertext_is_not_the_plaintext(self, key: bytes) -> None:
        """Obvious, and worth asserting: a `copy` that forgot to encrypt
        would pass every other test in this file."""
        plaintext = b"password_hash=$argon2id$" + os.urandom(4096)
        sealed = io.BytesIO()
        crypto.encrypt_stream(io.BytesIO(plaintext), sealed, key=key)

        assert b"password_hash" not in sealed.getvalue()

    def test_two_identical_chunks_encrypt_differently(self, key: bytes) -> None:
        """**The nonce test**, and the most important one in this file.

        Two chunks of identical plaintext, in one file, under one key. If
        their ciphertexts match, the counter is not reaching the nonce — and
        a reused nonce under GCM leaks the XOR of the two plaintexts and
        lets an attacker forge tags for that key. It is the one failure this
        construction cannot survive, and it is invisible: everything still
        round-trips.

        A mutation replacing the counter with a constant survived every
        other test here, which is why this one exists.
        """
        chunk = b"Z" * crypto.CHUNK_BYTES
        sealed = io.BytesIO()
        crypto.encrypt_stream(io.BytesIO(chunk * 2), sealed, key=key)

        raw = sealed.getvalue()[len(crypto.MAGIC) + 8 :]
        length = int.from_bytes(raw[:4], "big")
        first = raw[4 : 4 + length]
        second = raw[4 + length + 4 : 4 + length + 4 + length]

        assert len(first) == len(second) == length
        # The **ciphertext**, not the sealed chunk. The last sixteen bytes
        # are the GCM tag, and the tag differs whatever the nonce does
        # because the chunk index is authenticated as additional data — so
        # comparing the sealed chunks would pass with a constant nonce and
        # prove nothing. That is exactly what the first version of this test
        # did.
        assert first[:-16] != second[:-16], (
            "two identical chunks produced identical ciphertext: the nonce is being reused"
        )

    def test_two_encryptions_of_the_same_input_differ(self, key: bytes) -> None:
        """A fresh nonce prefix per file. Identical ciphertexts would mean a
        reused nonce, which under GCM leaks the XOR of the two plaintexts."""
        plaintext = b"a" * 8192
        first, second = io.BytesIO(), io.BytesIO()
        crypto.encrypt_stream(io.BytesIO(plaintext), first, key=key)
        crypto.encrypt_stream(io.BytesIO(plaintext), second, key=key)

        assert first.getvalue() != second.getvalue()


class TestWhatMustFail:
    """All four raise the same error, deliberately: an operator's next step
    is identical, and distinguishing them tells an attacker which half of
    their guess was right."""

    def test_a_wrong_key(self, key: bytes) -> None:
        sealed = io.BytesIO()
        crypto.encrypt_stream(io.BytesIO(b"secret" * 1000), sealed, key=key)
        other = crypto.parse_key(crypto.generate_key())

        with pytest.raises(crypto.BackupDecryptionError):
            crypto.decrypt_stream(io.BytesIO(sealed.getvalue()), io.BytesIO(), key=other)

    def test_a_single_flipped_bit(self, key: bytes) -> None:
        sealed = io.BytesIO()
        crypto.encrypt_stream(io.BytesIO(os.urandom(4096)), sealed, key=key)
        corrupted = bytearray(sealed.getvalue())
        corrupted[-1] ^= 0x01

        with pytest.raises(crypto.BackupDecryptionError):
            crypto.decrypt_stream(io.BytesIO(bytes(corrupted)), io.BytesIO(), key=key)

    def test_a_truncated_archive(self, key: bytes) -> None:
        """The failure a full disk produces. Silence here would mean a
        restore that succeeds with half the database."""
        sealed = io.BytesIO()
        crypto.encrypt_stream(io.BytesIO(os.urandom(8192)), sealed, key=key)
        truncated = sealed.getvalue()[:-40]

        with pytest.raises(crypto.BackupDecryptionError):
            crypto.decrypt_stream(io.BytesIO(truncated), io.BytesIO(), key=key)

    def test_a_file_that_is_not_a_backup(self, key: bytes) -> None:
        with pytest.raises(crypto.BackupDecryptionError, match="not an Arena64 backup"):
            crypto.decrypt_stream(io.BytesIO(b"PGDMP\x00\x00\x00"), io.BytesIO(), key=key)

    def test_chunks_from_two_archives_cannot_be_spliced(self, key: bytes) -> None:
        """The header is authenticated per chunk, so a chunk lifted from
        another backup fails its tag. Without that, an attacker with write
        access could graft an old table into a current archive."""
        first, second = io.BytesIO(), io.BytesIO()
        crypto.encrypt_stream(io.BytesIO(os.urandom(4096)), first, key=key)
        crypto.encrypt_stream(io.BytesIO(os.urandom(4096)), second, key=key)

        header_length = len(crypto.MAGIC) + 8
        header = first.getvalue()[:header_length]
        spliced = header + second.getvalue()[header_length:]

        with pytest.raises(crypto.BackupDecryptionError):
            crypto.decrypt_stream(io.BytesIO(spliced), io.BytesIO(), key=key)


class TestTheKey:
    def test_a_generated_key_round_trips(self) -> None:
        assert len(crypto.parse_key(crypto.generate_key())) == crypto.KEY_BYTES

    @pytest.mark.parametrize(
        "bad",
        ["", "not base64!!", base64.b64encode(b"short").decode()],
        ids=["empty", "not-base64", "wrong-length"],
    )
    def test_a_malformed_key_is_refused_before_it_is_used(self, bad: str) -> None:
        """At configuration time rather than at 3am on the one path nobody
        exercises until they need it."""
        with pytest.raises(ValueError, match="BACKUP_ENCRYPTION_KEY"):
            crypto.parse_key(bad)
