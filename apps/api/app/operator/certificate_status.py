"""When the TLS certificate expires — A64-028.6A §26, closing part of P3-4.

## The blind spot this closes

A64-028.6 recorded certificate expiry as an open gap: "Caddy renews
automatically and logs failures; nothing probes the result." Caddy has since
been replaced by nginx, which does not renew at all — the job belongs to a
`certbot` container now — so the gap got both larger and more consequential
in the same change.

A certificate that quietly stops renewing works perfectly for eighty-nine
days and then takes the whole site down at once. That is the failure this
metric exists for, and it is the reason the signal is **the certificate
itself** rather than the renewal job's exit status. A renewal that reports
success and writes nothing, a renewal container that is not running at all,
a volume mounted at the wrong path — none of those produce a failure log,
and all of them produce an expiring certificate.

## Why the worker reads it

The same shape as the backup status, and the same reason: the container that
owns the file cannot answer a scrape. `certbot` runs a command on a timer and
serves nothing; nginx serves everything but is not the application. The
worker mounts the certificate directory read-only and publishes the number.

**Only the public certificate is read.** `fullchain.pem` contains no private
key, and the private key is never mounted into any application container.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cryptography import x509

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CertificateStatus:
    """A certificate's validity window, as read from the file on disk."""

    not_before: datetime
    not_after: datetime
    subject: str

    def seconds_remaining(self, now: datetime) -> float:
        return (self.not_after - now).total_seconds()


def read(path: Path) -> CertificateStatus | None:
    """The leaf certificate at `path`, or `None`.

    `None` for a missing, unreadable or malformed file rather than an
    exception, because every one of those is a condition the alert must
    fire on and none is a reason to fail a scrape. `BackupNeverSucceeded`
    established the pattern: an **absent** metric is what a
    "this has never worked" alert is written against, and a zero would read
    as "expired just now", which is a different incident.

    `x509.load_pem_x509_certificates` returns the whole chain; the first
    entry is the leaf, and the leaf's expiry is the one a browser rejects.
    An intermediate expiring is the CA's problem and is not observable here.
    """
    try:
        chain = x509.load_pem_x509_certificates(path.read_bytes())
    except (OSError, ValueError):
        # Not `exception`: a missing certificate during first issuance is
        # ordinary, and a stack trace every scrape would bury the one that
        # matters.
        logger.warning("certificate_unreadable", extra={"path": str(path)})
        return None

    if not chain:
        logger.warning("certificate_empty", extra={"path": str(path)})
        return None

    leaf = chain[0]
    return CertificateStatus(
        not_before=leaf.not_valid_before_utc,
        not_after=leaf.not_valid_after_utc,
        # The common name, for a log line. Never a metric label: a
        # deployment with several certificates would be several series, and
        # this process reads exactly one.
        subject=leaf.subject.rfc4514_string(),
    )


def days_remaining(path: Path, *, now: datetime | None = None) -> float | None:
    """Convenience for an operator command and for tests."""
    status = read(path)
    if status is None:
        return None
    return status.seconds_remaining(now or datetime.now(UTC)) / 86400


__all__ = ["CertificateStatus", "days_remaining", "read"]
