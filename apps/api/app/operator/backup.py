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
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from urllib.parse import unquote, urlsplit

from app.config.environment import current_environment
from app.config.settings import get_settings
from app.operator import backup_status

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


def create(destination: Path, *, keep: int = DEFAULT_KEEP) -> Path:
    """Writes one backup and returns its path.

    Raises `BackupError` for every failure. There is deliberately no partial
    success: either a complete, checksummed dump with metadata beside it
    exists at the end, or this raises and the destination holds a `.partial`
    file whose name says so.
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
        _run(
            [dump_tool, *arguments, "--format=custom", "--no-password", "--file", str(partial)],
            environment,
            what="pg_dump",
        )
        if not partial.exists() or partial.stat().st_size == 0:
            raise BackupError("pg_dump exited zero but wrote nothing.")

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
    prune(destination, keep=keep)
    return final


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


def verify(dump: Path) -> dict[str, Any]:
    """Checks a backup without a database, and returns its metadata.

    Three things, and each catches a different way a backup is not one:

      checksum   the file is the file that was written. Silent corruption on
                 the way to or from off-host storage is the case
      listable   `pg_restore --list` parses the archive's table of contents,
                 so a truncated or wrong-format file fails here rather than
                 halfway through a restore at 3am
      non-empty  a listing with no entries is a dump of nothing
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

    completed = subprocess.run(  # noqa: S603
        [_require("pg_restore"), "--list", str(dump)], capture_output=True, text=True, check=False
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


def restore(dump: Path, *, target: str, confirmed: bool) -> None:
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
    verify(dump)
    arguments, environment, database = _libpq(target)
    logger.info("restore_started", extra={"backup": dump.name, "database": database})
    _run(
        [
            _require("pg_restore"),
            *arguments,
            "--no-password",
            "--exit-on-error",
            "--single-transaction",
            str(dump),
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


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "create":
            path = create(arguments.into, keep=arguments.keep)
            print(f"wrote {path}")  # noqa: T201 — an operator command's output
        elif arguments.command == "verify":
            metadata = verify(arguments.dump)
            print(  # noqa: T201
                f"{arguments.dump.name}: {metadata['objects']} objects, "
                f"{metadata['bytes']} bytes, revision {metadata['alembic_revision']}"
            )
        else:
            restore(arguments.dump, target=arguments.target, confirmed=arguments.confirmed)
            print(f"restored {arguments.dump.name}")  # noqa: T201
    except BackupError as error:
        print(f"error: {error}", file=sys.stderr)  # noqa: T201
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
