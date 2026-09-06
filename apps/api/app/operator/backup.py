"""PostgreSQL backup and restore — A64-028.3, closing A64-028.1's P0-4.

    python -m app.operator.backup create  --into /var/backups/arena64
    python -m app.operator.backup verify  /var/backups/arena64/<name>.dump
    python -m app.operator.backup restore /var/backups/arena64/<name>.dump \
        --target postgresql://... --i-understand-this-overwrites

## Why this exists

A64-028.1 found the platform had no backup of any kind: PostgreSQL lives in
a Docker volume on one host, and "a named volume is not a backup, and an
untested restore is not one either". Everything durable Arena64 owns —
accounts, matches, the move log the game rebuilds from, ratings, tournaments,
the append-only audit trail — is in that one database.

## Why `pg_dump -Fc` and not a base backup

The custom format is a single file, compressed, and `pg_restore` can read a
table list out of it without a server — which is what makes `verify` a real
check rather than a file-exists test. It is also version-portable in the
direction that matters: a dump taken from 17 restores into 17 or later.

What it does **not** give is point-in-time recovery. That decision, and the
residual risk it leaves, is recorded in
`docs/05-operations/backup-restore.md` — it needs WAL archiving to somewhere
this repository does not own, and A64-028.6 owns the deployment that would.

## What an off-host recovery needs

Three things, and nothing from the machine that is gone:

    <prefix>/<name>.dump        the encrypted archive
    <prefix>/<name>.dump.json   its manifest — checksum, revision, format
    the encryption key          stored somewhere neither of those is

`create` uploads both objects; `verify` and `restore` then work against a
directory holding the pair, exactly as they do on the host that wrote it.
A64-030.4C's drill found the manifest was staying behind, which made the
off-host copy a file nobody could check and the documented restore refuse.

## Why not a shell script

`pg_dump` is one subprocess; everything else here is the part that goes
wrong. A partial file that looks like a backup, a destination that silently
fell back to /tmp, a password in `ps` output, a retention sweep that deletes
the wrong generation — those are the failures worth typing carefully, and
they are worth `ruff`, `mypy --strict` and tests, which a shell script in
this repository would get none of.

## Two properties this is built around

**A partial backup can never look like a finished one.** `pg_dump` writes to
a `.partial` path; it is renamed onto the real name only after it has exited
zero *and* its checksum has been recorded. A crash, a full disk or a killed
process leaves a file whose name says what it is.

**The password never reaches a command line.** It is passed to the
subprocess in `PGPASSWORD`, because `ps` is readable by every account on the
host, and log lines carry the destination and the database name — never the
DSN.
"""

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess  # noqa: S404 — pg_dump is the tool; the point is to call it
import sys
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from urllib.parse import unquote, urlsplit

from app.config.environment import current_environment
from app.config.settings import get_settings
from app.operator import backup_crypto, backup_offsite, backup_status
from app.operator.backup_offsite import OffsiteTarget

logger = logging.getLogger(__name__)

#: How many backups `create` keeps in a destination before removing the
#: oldest. Seven daily backups is a week to notice a problem in — the
#: interval is the scheduler's business (A64-028.6), so this counts
#: generations rather than days.
DEFAULT_KEEP: Final = 7

#: `pg_dump -Fc`. Everything downstream — `verify`, `restore`, the metadata's
#: `format` field — assumes it, and a dump in another format is a dump this
#: tool cannot check.
_FORMAT: Final = "custom"

_STAMP: Final = "%Y%m%dT%H%M%SZ"


class BackupError(RuntimeError):
    """Anything that means "there is no usable backup at the end of this"."""


def _dsn() -> str:
    return get_settings().postgres.dsn.get_secret_value()


def _libpq(dsn: str) -> tuple[list[str], dict[str, str], str]:
    """Connection arguments, the environment carrying the password, and the
    database name.

    SQLAlchemy's `postgresql+asyncpg://` is not a libpq URI, and handing it to
    `pg_dump` produces an error about an unknown scheme that reads like a
    configuration problem rather than a translation one.
    """
    parts = urlsplit(re.sub(r"^postgresql\+\w+://", "postgresql://", dsn))
    database = parts.path.lstrip("/")
    if not database:
        raise BackupError("The DSN names no database.")

    arguments = ["--host", parts.hostname or "localhost", "--dbname", database]
    if parts.port:
        arguments += ["--port", str(parts.port)]
    if parts.username:
        arguments += ["--username", unquote(parts.username)]

    environment = dict(os.environ)
    if parts.password:
        # Never `--password` and never in argv: `ps` is world-readable.
        environment["PGPASSWORD"] = unquote(parts.password)
    return arguments, environment, database


def _require(tool: str) -> str:
    found = shutil.which(tool)
    if found is None:
        raise BackupError(
            f"{tool} is not on PATH. This command needs the PostgreSQL client "
            "tools; see docs/05-operations/backup-restore.md."
        )
    return found


def _run(command: list[str], environment: dict[str, str], *, what: str) -> None:
    """A subprocess whose failure is an exception, with its stderr attached.

    `pg_dump` says useful things on stderr and returns non-zero, and a
    wrapper that dropped either would turn a permissions problem into a
    mystery.
    """
    completed = subprocess.run(  # noqa: S603 — argv list, never a shell
        command, env=environment, capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        raise BackupError(
            f"{what} failed with exit code {completed.returncode}: "
            f"{completed.stderr.strip() or '(no output)'}"
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _alembic_head(arguments: list[str], environment: dict[str, str]) -> str | None:
    """The migration this database is on, read with `psql`.

    Recorded because a dump restored against the wrong code is the failure a
    restore drill is meant to catch: the schema is whatever it was, and only
    the revision says which.
    """
    completed = subprocess.run(  # noqa: S603
        [
            _require("psql"),
            *arguments,
            "--tuples-only",
            "--no-align",
            "--command",
            "SELECT version_num FROM public.alembic_version",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    revision = completed.stdout.strip()
    return revision or None


def metadata_path(dump: Path) -> Path:
    return dump.with_suffix(dump.suffix + ".json")


def create(
    destination: Path,
    *,
    keep: int = DEFAULT_KEEP,
    key: bytes | None = None,
    offsite: OffsiteTarget | None = None,
) -> Path:
    """Writes one backup and returns its path.

    Raises `BackupError` for every failure. There is deliberately no partial
    success: either a complete, checksummed dump with metadata beside it
    exists at the end, or this raises and the destination holds a `.partial`
    file whose name says so.

    ## `key`, and the plaintext that never touches the disk — A64-028.7 (P2-8)

    With a key, `pg_dump` writes to **stdout** and the bytes are encrypted as
    they stream past. The obvious implementation — dump to a file, encrypt
    the file, delete the plaintext — leaves every email address and password
    hash on disk for the length of the encryption, and leaves them there for
    good if the process dies in between. A `finally` that unlinks is not an
    answer: it does not run when the machine loses power, which is one of
    the events a backup exists for.

    Without a key the behaviour is exactly what it was, because `local`
    development has nothing to protect and an operator restoring by hand
    should not need one. A deployed tier is refused at configuration time
    (`Settings._guard_production_backup`), so an unencrypted production
    backup cannot be produced by forgetting a flag.
    """
    dump_tool = _require("pg_dump")
    arguments, environment, database = _libpq(_dsn())
    environment_name = current_environment().value

    destination.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime(_STAMP)
    final = destination / f"arena64-{environment_name}-{stamp}.dump"
    partial = final.with_suffix(".dump.partial")

    logger.info(
        "backup_started",
        extra={"database": database, "destination": str(destination), "target": final.name},
    )
    try:
        if key is None:
            _run(
                [
                    dump_tool,
                    *arguments,
                    "--format=custom",
                    "--no-password",
                    "--file",
                    str(partial),
                ],
                environment,
                what="pg_dump",
            )
        else:
            _dump_encrypted(
                [dump_tool, *arguments, "--format=custom", "--no-password"],
                environment,
                target=partial,
                key=key,
            )
        if not partial.exists() or partial.stat().st_size == 0:
            raise BackupError("pg_dump exited zero but wrote nothing.")

        # The checksum is of the artefact **as stored** — the ciphertext
        # when encrypted. It answers "did this file arrive intact", which is
        # a question about the bytes on disk and in the off-host copy; the
        # GCM tag is what answers "is the plaintext authentic", and the two
        # are different guarantees.
        checksum = _sha256(partial)
        revision = _alembic_head(arguments, environment)
        # The rename is the commit point. Until it happens the file is named
        # `.partial`, so a crash anywhere above cannot leave something a
        # restore would pick up.
        partial.rename(final)
        metadata_path(final).write_text(
            json.dumps(
                {
                    "created_at": datetime.now(UTC).isoformat(),
                    "environment": environment_name,
                    "database": database,
                    "format": _FORMAT,
                    # So `verify` and `restore` know to ask for a key rather
                    # than handing ciphertext to `pg_restore` and reporting
                    # a corrupt archive.
                    "encrypted": key is not None,
                    "alembic_revision": revision,
                    "sha256": checksum,
                    "bytes": final.stat().st_size,
                    "pg_dump": _tool_version(dump_tool),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
    except Exception:
        partial.unlink(missing_ok=True)
        logger.exception("backup_failed", extra={"database": database})
        # Recorded before the raise, so a destination whose backups have
        # been failing says so — A64-028.6 §20. The last success is kept
        # alongside: "the last attempt failed" and "the last good copy is
        # from Tuesday" are different facts and an operator needs both.
        backup_status.record_failure(destination, at=datetime.now(UTC))
        raise

    logger.info(
        "backup_completed",
        extra={"backup": final.name, "bytes": final.stat().st_size, "revision": revision},
    )
    # A note beside the dumps, so "when did a backup last succeed" is
    # answerable without a shell on the backup host — A64-028.6 §20.
    backup_status.record_success(destination, archive=final.name, at=datetime.now(UTC))
    # Off-host **after** the local archive is complete and stamped, so a
    # failed upload leaves a verified local copy rather than nothing —
    # A64-028.7, the second half of P2-8.
    if offsite is not None:
        manifest = metadata_path(final)
        try:
            key_name = backup_offsite.upload(final, target=offsite, sha256=checksum)
            # **The manifest goes off-host too, and second** — A64-030.4C.3.
            #
            # `verify` reads the checksum and the revision out of the file
            # beside the dump, and `restore` calls `verify`, so an off-host
            # copy of the ciphertext alone is not restorable: after the host
            # is lost there is nothing to check the download against and
            # nothing that says which schema is in it. A64-030.4C's drill
            # found this the only way it can be found — by restoring from
            # off-host and discovering the tooling refuses.
            #
            # **Second, not first.** The manifest is what makes the pair
            # usable, so uploading it last means an interrupted copy leaves a
            # dump with no manifest — which the restore path already refuses
            # — rather than a manifest promising a dump that is not there.
            # Failing closed is the direction that does not lose data.
            backup_offsite.upload(manifest, target=offsite, sha256=_sha256(manifest))
        except backup_offsite.OffsiteUploadError:
            # Recorded and re-raised. An upload that fails silently leaves an
            # operator believing they have an off-host copy, which is worse
            # than knowing they do not — and the local archive is still
            # there, so this is a partial success reported as a failure
            # rather than a loss.
            #
            # The pair is one recoverable unit: a dump that arrived without
            # its manifest is not an off-host backup, and is recorded as a
            # failure for the same reason no upload at all would be.
            logger.exception("backup_offsite_failed", extra={"backup": final.name})
            backup_status.record_offsite_failure(destination, at=datetime.now(UTC))
            raise
        backup_status.record_offsite_success(
            destination, archive=final.name, key=key_name, at=datetime.now(UTC)
        )

    prune(destination, keep=keep)
    return final


def _dump_encrypted(
    command: list[str], environment: dict[str, str], *, target: Path, key: bytes
) -> None:
    """`pg_dump` to stdout, encrypted into `target` as it streams.

    The plaintext exists only in a pipe buffer and one 4 MiB chunk of this
    process's memory. It is never a file, so there is no window in which a
    crash leaves one and no cleanup path that has to be right.

    `pg_dump`'s stderr is captured rather than inherited, for the reason
    `_run` gives: it says useful things about a failure and none of them
    should reach a log unfiltered.
    """
    with subprocess.Popen(  # noqa: S603 — the command is built above, not user input
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    ) as process:
        if process.stdout is None:  # pragma: no cover — Popen with PIPE always sets it
            raise BackupError("pg_dump produced no stdout to read.")
        with target.open("wb") as sealed:
            backup_crypto.encrypt_stream(process.stdout, sealed, key=key)
        _, stderr = process.communicate()

    if process.returncode != 0:
        raise BackupError(
            f"pg_dump exited {process.returncode}: {stderr.decode(errors='replace').strip()}"
        )


@contextmanager
def _decrypted(dump: Path, *, key: bytes | None) -> Iterator[Path]:
    """The archive as something `pg_restore` can open.

    An unencrypted archive is yielded as itself — no copy, no temporary
    file, nothing to clean up.

    An encrypted one is decrypted into a temporary file, because
    `pg_restore` needs to seek and a pipe cannot. This is the **only** place
    a plaintext dump legitimately exists on disk, and it is bounded three
    ways: the file is created `0600` by `mkstemp`, it lives beside the
    archive rather than in a shared `/tmp` that other users can list, and it
    is removed in a `finally` — so a failed restore does not leave the
    database's contents lying next to its backup.

    What that cannot survive is the machine losing power mid-restore. The
    window is a restore rather than a backup, which is the rarer and
    supervised of the two, and it is stated in
    `docs/05-operations/backup-restore.md` rather than left to be
    discovered.
    """
    if key is None:
        yield dump
        return

    handle, name = tempfile.mkstemp(prefix=".restore-", suffix=".dump", dir=str(dump.parent))
    plaintext = Path(name)
    try:
        with os.fdopen(handle, "wb") as target, dump.open("rb") as source:
            backup_crypto.decrypt_stream(source, target, key=key)
        yield plaintext
    finally:
        plaintext.unlink(missing_ok=True)


def _tool_version(tool: str) -> str:
    completed = subprocess.run(  # noqa: S603
        [tool, "--version"], capture_output=True, text=True, check=False
    )
    return completed.stdout.strip() or "unknown"


def prune(destination: Path, *, keep: int) -> list[Path]:
    """Removes all but the newest `keep` backups. Returns what it removed.

    Newest by *name*, which is a UTC timestamp in a sortable format — not by
    mtime, which a copy or a restore-from-tape would rewrite.
    """
    if keep < 1:
        raise BackupError("keep must be at least 1; refusing to delete every backup.")

    backups = sorted(destination.glob("arena64-*.dump"))
    removed: list[Path] = []
    for stale in backups[: max(0, len(backups) - keep)]:
        metadata_path(stale).unlink(missing_ok=True)
        stale.unlink()
        removed.append(stale)
    if removed:
        logger.info("backup_pruned", extra={"removed": [path.name for path in removed]})
    return removed


def verify(dump: Path, *, key: bytes | None = None) -> dict[str, Any]:
    """Checks a backup without a database, and returns its metadata.

    Three things, and each catches a different way a backup is not one:

      checksum   the file is the file that was written. Silent corruption on
                 the way to or from off-host storage is the case
      listable   `pg_restore --list` parses the archive's table of contents,
                 so a truncated or wrong-format file fails here rather than
                 halfway through a restore at 3am
      non-empty  a listing with no entries is a dump of nothing

    An encrypted archive is decrypted into a temporary file first, which is
    the one place a plaintext dump legitimately touches the disk: the
    listing is `pg_restore`'s and it needs a file. It is written under
    `0600` in a directory this process owns and removed on every path,
    including the failing one — see `_decrypted`.
    """
    if not dump.exists():
        raise BackupError(f"No such backup: {dump}")

    metadata_file = metadata_path(dump)
    if not metadata_file.exists():
        raise BackupError(f"No metadata beside {dump.name}; it may be an interrupted backup.")
    metadata: dict[str, Any] = json.loads(metadata_file.read_text())

    actual = _sha256(dump)
    if actual != metadata.get("sha256"):
        raise BackupError(
            f"Checksum mismatch for {dump.name}: the file is not the one that was written."
        )

    if metadata.get("encrypted") and key is None:
        raise BackupError(
            f"{dump.name} is encrypted. Supply the key it was written with "
            "(BACKUP_ENCRYPTION_KEY) — it is not stored with the archive."
        )

    with _decrypted(dump, key=key if metadata.get("encrypted") else None) as readable:
        completed = subprocess.run(  # noqa: S603
            [_require("pg_restore"), "--list", str(readable)],
            capture_output=True,
            text=True,
            check=False,
        )
    if completed.returncode != 0:
        raise BackupError(
            f"pg_restore could not read {dump.name}: {completed.stderr.strip() or '(no output)'}"
        )
    entries = [line for line in completed.stdout.splitlines() if line and not line.startswith(";")]
    if not entries:
        raise BackupError(f"{dump.name} contains no objects.")

    logger.info("backup_verified", extra={"backup": dump.name, "objects": len(entries)})
    return {**metadata, "objects": len(entries)}


def restore(dump: Path, *, target: str, confirmed: bool, key: bytes | None = None) -> None:
    """Restores into `target`, which must be an existing empty database.

    **The target is always explicit.** There is no "restore into the
    configured database", because the configured database is production and a
    tool that can be pointed at it by forgetting an argument is a tool that
    will be. The confirmation flag is the second lock.
    """
    if not confirmed:
        raise BackupError(
            "Refusing to restore without --i-understand-this-overwrites. "
            "This writes into the target database."
        )
    metadata = verify(dump, key=key)
    arguments, environment, database = _libpq(target)
    logger.info("restore_started", extra={"backup": dump.name, "database": database})
    with _decrypted(dump, key=key if metadata.get("encrypted") else None) as readable:
        _run(
            [
                _require("pg_restore"),
                *arguments,
                "--no-password",
                "--exit-on-error",
                "--single-transaction",
                str(readable),
            ],
            environment,
            what="pg_restore",
        )
    logger.info("restore_completed", extra={"backup": dump.name, "database": database})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.operator.backup",
        description="Back up and restore the Arena64 database.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser(
        "keygen", help="Print a fresh base64 encryption key. Store it away from the backups."
    )

    creating = commands.add_parser("create", help="Write a new backup.")
    creating.add_argument("--into", type=Path, required=True, help="Destination directory.")
    creating.add_argument("--keep", type=int, default=DEFAULT_KEEP, help="Generations to keep.")

    checking = commands.add_parser("verify", help="Check a backup's checksum and contents.")
    checking.add_argument("dump", type=Path)

    restoring = commands.add_parser("restore", help="Restore a backup into a named database.")
    restoring.add_argument("dump", type=Path)
    restoring.add_argument("--target", required=True, help="libpq URI of the database to write.")
    restoring.add_argument(
        "--i-understand-this-overwrites",
        action="store_true",
        dest="confirmed",
        help="Required. Restoring writes into the target database.",
    )
    return parser


def _key() -> bytes | None:
    """The encryption key, from configuration.

    Read here rather than passed on the command line, and that is the whole
    of it: an argument is visible in `ps` to every user on the host and ends
    up in shell history. `SecretStr` also keeps it out of a traceback.
    """
    encoded = get_settings().observability.backup_encryption_key
    return None if encoded is None else backup_crypto.parse_key(encoded.get_secret_value())


def _offsite() -> OffsiteTarget | None:
    """The off-host target, from configuration.

    `None` when nothing is configured, which is `local`'s state and is
    refused in a deployed tier by `Settings._guard_production_backup` —
    an off-host copy is not an enhancement, it is what makes the word
    backup true.
    """
    observability = get_settings().observability
    endpoint = observability.backup_offsite_endpoint
    bucket = observability.backup_offsite_bucket
    access = observability.backup_offsite_access_key_id
    secret = observability.backup_offsite_secret_access_key
    if endpoint is None or bucket is None or access is None or secret is None:
        return None
    return OffsiteTarget(
        endpoint=endpoint,
        bucket=bucket,
        region=observability.backup_offsite_region,
        access_key_id=access.get_secret_value(),
        secret_access_key=secret.get_secret_value(),
        prefix=observability.backup_offsite_prefix,
    )


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "keygen":
            # Printed, never stored. The operator puts it in their secret
            # manager; this command has no idea where that is.
            print(backup_crypto.generate_key())  # noqa: T201
            return 0
        if arguments.command == "create":
            path = create(arguments.into, keep=arguments.keep, key=_key(), offsite=_offsite())
            print(f"wrote {path}")  # noqa: T201 — an operator command's output
        elif arguments.command == "verify":
            metadata = verify(arguments.dump, key=_key())
            print(  # noqa: T201
                f"{arguments.dump.name}: {metadata['objects']} objects, "
                f"{metadata['bytes']} bytes, revision {metadata['alembic_revision']}"
            )
        else:
            restore(
                arguments.dump,
                target=arguments.target,
                confirmed=arguments.confirmed,
                key=_key(),
            )
            print(f"restored {arguments.dump.name}")  # noqa: T201
    except BackupError as error:
        print(f"error: {error}", file=sys.stderr)  # noqa: T201
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
