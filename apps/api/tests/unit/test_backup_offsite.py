"""The off-host uploader's signature — A64-028.7, second half of P2-8.

The upload itself is proven against a real MinIO in the task's deployment
verification: a correct signature stores the object, a wrong secret is
refused with 403, a missing bucket with 404 and a tampered payload hash with
400. What is worth pinning in a unit test is the part that has to be exactly
right and is invisible when it is wrong — SigV4's canonical form.

A signature that is subtly wrong does not fail here. It fails on the first
real upload, at 3am, against a provider whose error message says
`SignatureDoesNotMatch` and nothing else.
"""

import dataclasses
import hashlib
import hmac
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.operator.backup_offsite import (
    OffsiteTarget,
    OffsiteUploadError,
    _authorisation,
    _signing_key,
    upload,
)

TARGET = OffsiteTarget(
    endpoint="https://s3.example.test",
    bucket="arena64-backups",
    region="eu-central-1",
    access_key_id="AKIAEXAMPLE",
    secret_access_key="secret",
    prefix="production",
)
WHEN = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


class TestTheObjectKey:
    def test_the_prefix_is_applied(self) -> None:
        assert TARGET.key_for("arena64.dump.enc") == "production/arena64.dump.enc"

    def test_an_empty_prefix_leaves_the_name_alone(self) -> None:
        """A leading slash would create an object literally named `/…` on
        some providers and a directory on others."""
        assert dataclasses.replace(TARGET, prefix="").key_for("a.enc") == "a.enc"

    @pytest.mark.parametrize("prefix", ["/production/", "production/", "/production"])
    def test_stray_slashes_are_trimmed(self, prefix: str) -> None:
        assert dataclasses.replace(TARGET, prefix=prefix).key_for("a.enc") == "production/a.enc"


class TestTheSigningKey:
    def test_it_is_scoped_to_a_day_and_a_region(self) -> None:
        """Four chained HMACs, which is what makes a leaked signature
        unusable against another day or another region."""
        first = _signing_key("secret", "20260906", "eu-central-1")
        assert first != _signing_key("secret", "20260907", "eu-central-1")
        assert first != _signing_key("secret", "20260906", "us-east-1")
        assert first != _signing_key("other", "20260906", "eu-central-1")

    def test_it_matches_the_specification(self) -> None:
        """Computed here independently rather than compared to itself: a
        test that calls the function twice proves only that it is
        deterministic."""
        expected = b"AWS4secret"
        for part in ("20260906", "eu-central-1", "s3", "aws4_request"):
            expected = hmac.new(expected, part.encode(), hashlib.sha256).digest()

        assert _signing_key("secret", "20260906", "eu-central-1") == expected


class TestTheAuthorisationHeader:
    def _headers(self, **overrides: object) -> dict[str, str]:
        arguments: dict[str, object] = {
            "method": "PUT",
            "host": "s3.example.test",
            "path": "/arena64-backups/production/a.enc",
            "payload_sha256": "a" * 64,
            "now": WHEN,
        }
        arguments.update(overrides)
        return _authorisation(TARGET, **arguments)  # type: ignore[arg-type]

    def test_it_names_the_algorithm_credential_and_signed_headers(self) -> None:
        header = self._headers()["Authorization"]

        assert header.startswith("AWS4-HMAC-SHA256 ")
        assert "Credential=AKIAEXAMPLE/20260906/eu-central-1/s3/aws4_request" in header
        assert "SignedHeaders=host;x-amz-content-sha256;x-amz-date" in header

    def test_the_payload_hash_is_signed(self) -> None:
        """`UNSIGNED-PAYLOAD` would let the object be altered in flight
        without the signature failing. On the artefact a restore depends on,
        one pass that was happening anyway is worth it."""
        assert self._headers()["x-amz-content-sha256"] == "a" * 64
        assert (
            self._headers()["Authorization"]
            != self._headers(payload_sha256="b" * 64)["Authorization"]
        )

    @pytest.mark.parametrize(
        ("field", "value"),
        [("method", "GET"), ("path", "/other/key"), ("host", "s3.other.test")],
    )
    def test_every_signed_component_changes_the_signature(self, field: str, value: str) -> None:
        assert self._headers()["Authorization"] != self._headers(**{field: value})["Authorization"]

    def test_the_secret_never_appears_in_a_header(self) -> None:
        """The signature is derived from it; the key itself is never sent."""
        rendered = " ".join(self._headers().values())

        assert TARGET.secret_access_key not in rendered


class TestRefusals:
    def test_an_endpoint_that_is_not_a_url(self, tmp_path: Path) -> None:
        archive = tmp_path / "a.enc"
        archive.write_bytes(b"x")

        with pytest.raises(OffsiteUploadError, match="not a URL"):
            upload(
                archive,
                target=dataclasses.replace(TARGET, endpoint="s3.example.test"),
                sha256="a" * 64,
            )
