"""Reading a certificate's expiry — A64-028.6A §26.

The metric this backs exists because a certificate that quietly stops
renewing works perfectly for eighty-nine days and then takes the whole site
down at once. Every test here is about the reading being trustworthy in the
states an operator will actually meet: a good certificate, one that is
missing because issuance has not finished, and one that is corrupt because
something wrote half a file.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app.operator import certificate_status


def _write_certificate(
    path: Path, *, valid_for: timedelta, common_name: str = "arena64.gg"
) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        # Both dated relative to the expiry, so an already-expired
        # certificate is constructible — `not_valid_before` must precede
        # `not_valid_after` and a fixed "one minute ago" cannot.
        .not_valid_before(now + valid_for - timedelta(days=90))
        .not_valid_after(now + valid_for)
        .sign(key, hashes.SHA256())
    )
    path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))


class TestReadingAValidCertificate:
    def test_the_expiry_is_the_certificates_own(self, tmp_path: Path) -> None:
        path = tmp_path / "fullchain.pem"
        _write_certificate(path, valid_for=timedelta(days=90))

        status = certificate_status.read(path)

        assert status is not None
        assert 89 < status.seconds_remaining(datetime.now(UTC)) / 86400 <= 90

    def test_the_leaf_is_read_from_a_chain(self, tmp_path: Path) -> None:
        """`fullchain.pem` is leaf then intermediates. The leaf's expiry is
        the one a browser rejects; an intermediate's is the CA's problem."""
        leaf, intermediate = tmp_path / "leaf.pem", tmp_path / "int.pem"
        _write_certificate(leaf, valid_for=timedelta(days=30), common_name="arena64.gg")
        _write_certificate(intermediate, valid_for=timedelta(days=3650), common_name="Some CA")
        chain = tmp_path / "fullchain.pem"
        chain.write_bytes(leaf.read_bytes() + intermediate.read_bytes())

        status = certificate_status.read(chain)

        assert status is not None
        assert "arena64.gg" in status.subject
        assert status.seconds_remaining(datetime.now(UTC)) / 86400 < 31

    def test_an_expired_certificate_reports_a_negative_remainder(self, tmp_path: Path) -> None:
        """Negative rather than clamped: `CertificateExpired` fires on the
        sign, and clamping at zero would make an expired certificate
        indistinguishable from one expiring this second."""
        path = tmp_path / "fullchain.pem"
        _write_certificate(path, valid_for=timedelta(days=-1))

        status = certificate_status.read(path)

        assert status is not None
        assert status.seconds_remaining(datetime.now(UTC)) < 0


class TestWhatCannotBeRead:
    """All three answer `None`, and the reason is the alert design.

    An **absent** metric is what `CertificateMissing` fires on. A zero would
    read as "expired just now", which is a different incident with a
    different response — and would silence the rule that was written for
    exactly this state.
    """

    def test_a_missing_file(self, tmp_path: Path) -> None:
        assert certificate_status.read(tmp_path / "nothing.pem") is None

    def test_a_file_that_is_not_a_certificate(self, tmp_path: Path) -> None:
        path = tmp_path / "fullchain.pem"
        path.write_text("this is not a certificate")

        assert certificate_status.read(path) is None

    def test_a_truncated_certificate(self, tmp_path: Path) -> None:
        """Half a file is what a crash mid-write leaves behind."""
        path = tmp_path / "fullchain.pem"
        _write_certificate(path, valid_for=timedelta(days=90))
        path.write_bytes(path.read_bytes()[:200])

        assert certificate_status.read(path) is None

    def test_an_empty_file(self, tmp_path: Path) -> None:
        path = tmp_path / "fullchain.pem"
        path.write_bytes(b"")

        assert certificate_status.read(path) is None

    @pytest.mark.parametrize("name", ["missing.pem", "empty.pem"])
    def test_days_remaining_is_none_rather_than_a_number(self, tmp_path: Path, name: str) -> None:
        path = tmp_path / name
        if name == "empty.pem":
            path.write_bytes(b"")

        assert certificate_status.days_remaining(path) is None


class TestThePrivateKeyIsNeverTouched:
    def test_only_the_public_certificate_is_read(self, tmp_path: Path) -> None:
        """The worker mounts the certificate directory read-only to publish
        one number. It must not need, and must not read, the key."""
        path = tmp_path / "fullchain.pem"
        _write_certificate(path, valid_for=timedelta(days=90))
        key = tmp_path / "privkey.pem"
        key.write_text("-----BEGIN PRIVATE KEY-----\nnot-a-key\n-----END PRIVATE KEY-----\n")

        status = certificate_status.read(path)

        assert status is not None
        # A read of `privkey.pem` would have raised on that body.
        assert key.exists()
