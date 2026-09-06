"""Off-host backup storage — A64-028.7, the second half of P2-8.

A64-028.1's finding had two parts and encryption answers one of them. This
answers the other: **backups live on a volume beside the database they
protect**, so the host loss that makes a restore necessary takes the backups
with it. A volume is not a backup; a copy on another machine is.

## S3-compatible, and no new dependency

The target is the S3 REST API rather than a vendor SDK: it is what every
object store worth using speaks — AWS, Cloudflare R2, Backblaze B2,
Hetzner, MinIO — so the deployment chooses a provider by setting an
endpoint rather than by changing this file.

`boto3` would be the obvious client and is deliberately not used.
`CLAUDE.md` §2.6 asks whether an existing dependency does the job: `httpx`
is already here, `hmac` and `hashlib` are the standard library, and what is
actually needed is one `PUT` with a SigV4 signature. That is the whole of
this module, and it is testable against the MinIO the compose file already
runs.

## What is signed, and why the payload hash is free

SigV4 requires the SHA-256 of the body. `backup.create` has already computed
exactly that for the archive's checksum, so the caller passes it in rather
than the file being read twice. `UNSIGNED-PAYLOAD` would avoid the hash
entirely and is not used: a signed payload hash means the object cannot be
altered in flight without the signature failing, which on the artefact a
restore depends on is worth one pass that was happening anyway.

## What this module deliberately does not do

**It does not encrypt.** The archive arrives sealed — `backup_crypto` does
that at dump time, before anything touches a disk. Encrypting here would
mean a plaintext file existed until upload, which is the failure the
streaming design exists to avoid.

**It does not decide retention on the remote.** Object stores have
lifecycle rules, and reimplementing them over a REST API is a worse version
of a feature every provider already has. The runbook says to configure one.
"""

import hashlib
import hmac
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

logger = logging.getLogger(__name__)

_ALGORITHM = "AWS4-HMAC-SHA256"
_SERVICE = "s3"


class OffsiteUploadError(Exception):
    """The archive did not reach the remote.

    Raised rather than logged-and-swallowed: an upload that fails silently
    leaves an operator believing they have an off-host copy, which is worse
    than knowing they do not.
    """


@dataclass(frozen=True, slots=True)
class OffsiteTarget:
    """Where an archive is copied to, and with what credentials."""

    endpoint: str
    bucket: str
    region: str
    access_key_id: str
    secret_access_key: str
    prefix: str = ""

    def key_for(self, name: str) -> str:
        return f"{self.prefix.strip('/')}/{name}" if self.prefix.strip("/") else name


def _signing_key(secret: str, stamp: str, region: str) -> bytes:
    """SigV4's derived key: date, region, service, then the terminator.

    Four chained HMACs, which is the specification's own construction — the
    point of it is that a signing key is scoped to one day and one region,
    so a leaked signature cannot be replayed against another.
    """
    key = f"AWS4{secret}".encode()
    for part in (stamp, region, _SERVICE, "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    return key


def _authorisation(
    target: OffsiteTarget,
    *,
    method: str,
    host: str,
    path: str,
    payload_sha256: str,
    now: datetime,
) -> dict[str, str]:
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    stamp = now.strftime("%Y%m%d")
    scope = f"{stamp}/{target.region}/{_SERVICE}/aws4_request"

    headers = {
        "host": host,
        "x-amz-content-sha256": payload_sha256,
        "x-amz-date": amz_date,
    }
    signed = ";".join(sorted(headers))
    canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in sorted(headers))
    canonical = "\n".join([method, path, "", canonical_headers, signed, payload_sha256])
    to_sign = "\n".join(
        [_ALGORITHM, amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()]
    )
    signature = hmac.new(
        _signing_key(target.secret_access_key, stamp, target.region),
        to_sign.encode(),
        hashlib.sha256,
    ).hexdigest()

    return {
        **{name: value for name, value in headers.items() if name != "host"},
        "Authorization": (
            f"{_ALGORITHM} Credential={target.access_key_id}/{scope}, "
            f"SignedHeaders={signed}, Signature={signature}"
        ),
    }


def upload(archive: Path, *, target: OffsiteTarget, sha256: str, timeout: float = 300.0) -> str:
    """Copies `archive` to the remote and returns the object key.

    `sha256` is the archive's checksum, already computed by `backup.create`.
    Passing it in rather than recomputing keeps this to one pass over a file
    that may be gigabytes.

    Streams from disk: `httpx` sends the file handle, so peak memory is a
    buffer rather than the archive.
    """
    key = target.key_for(archive.name)
    split = urlsplit(target.endpoint)
    if not split.scheme or not split.netloc:
        raise OffsiteUploadError(f"BACKUP_OFFSITE_ENDPOINT is not a URL: {target.endpoint!r}")

    # Path-style addressing (`/bucket/key`) rather than virtual-host style.
    # Every S3-compatible store accepts it; a self-hosted MinIO on a bare
    # hostname is the one that cannot do the alternative.
    path = f"/{target.bucket}/{quote(key)}"
    url = f"{split.scheme}://{split.netloc}{path}"

    headers = _authorisation(
        target,
        method="PUT",
        host=split.netloc,
        path=path,
        payload_sha256=sha256,
        now=datetime.now(UTC),
    )

    logger.info(
        "backup_offsite_upload_started",
        # The bucket and key, never the endpoint's credentials and never the
        # signature. Both are in the headers and neither is logged.
        extra={"bucket": target.bucket, "object": key, "bytes": archive.stat().st_size},
    )
    try:
        with archive.open("rb") as body:
            response = httpx.put(
                url,
                content=body,
                headers={**headers, "Content-Length": str(archive.stat().st_size)},
                timeout=timeout,
            )
    except httpx.HTTPError as error:
        raise OffsiteUploadError(
            f"the archive could not be sent to {split.netloc}: {type(error).__name__}"
        ) from error

    if response.status_code >= 400:
        # The provider's body, not the request's — it names the reason
        # (`SignatureDoesNotMatch`, `NoSuchBucket`, `AccessDenied`) and
        # carries nothing secret.
        raise OffsiteUploadError(
            f"{split.netloc} refused the upload with {response.status_code}: "
            f"{response.text[:200].strip()}"
        )

    logger.info("backup_offsite_upload_completed", extra={"bucket": target.bucket, "object": key})
    return key


__all__ = ["OffsiteTarget", "OffsiteUploadError", "upload"]
