"""Whether a backup happened, readable without a shell — A64-028.6 §20.

## The gap this closes

A64-028.3 gave the platform a backup command that takes, verifies and
restores a `pg_dump -Fc` archive, and left two things to this task: the
schedule, and the answer to "did it run". Nothing recorded a last-success
time. The only evidence a backup had ever succeeded was a sortable filename
in a directory an operator had to go and look at, on a host they had to be
able to reach.

That is the shape of every backup failure anybody has ever written about:
the backups stop, nothing says so, and the fact is discovered on the day the
restore is needed.

## Why a file in the destination and not a database row

The destination is the thing whose health is in question. A row in
PostgreSQL would say "a backup of PostgreSQL succeeded" using the database
that the backup exists to survive the loss of — and would be gone in exactly
the scenario an operator most needs to know when the last good copy was
taken.

A JSON file beside the dumps travels with them, survives the database
entirely, and is readable by anything.

## What "stale" means and who decides

This module reports an **age**. It does not decide what is too old, because
that is a recovery-point objective and belongs to the deployment rather
than to the code — `docs/05-operations/backup-restore.md` §3 states the
target and the alert rule states the threshold. What is fixed here is that
the age is always answerable.
"""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

logger = logging.getLogger(__name__)

#: Written beside the dumps, in the destination the backup was taken to.
STATUS_FILENAME: Final = "arena64-backup-status.json"

#: Seconds. A destination that has never held a successful backup reports
#: this rather than zero: zero is indistinguishable from "just now", which
#: is the reading that would silence the alert that matters most.
NEVER: Final = float("inf")


@dataclass(frozen=True, slots=True)
class BackupStatus:
    """The last outcome, and how long ago it was."""

    succeeded_at: datetime | None
    failed_at: datetime | None
    last_outcome: str
    archive: str | None
    offsite_at: datetime | None = None
    offsite_outcome: str = "unknown"

    def age_seconds(self, now: datetime) -> float:
        if self.succeeded_at is None:
            return NEVER
        return max(0.0, (now - self.succeeded_at).total_seconds())


def record_success(destination: Path, *, archive: str, at: datetime) -> None:
    """Stamps a successful backup.

    Best-effort by construction: a failure to *write the note* must not fail
    the backup that already succeeded, because the archive on disk is worth
    more than the record of it. The failure is logged at `ERROR`, which is
    the right level — an operator whose status file has stopped updating is
    flying blind even though the backups are fine.
    """
    _write(
        destination,
        {
            "succeeded_at": at.astimezone(UTC).isoformat(),
            "failed_at": _existing(destination).get("failed_at"),
            "last_outcome": "succeeded",
            "archive": archive,
        },
    )


def record_failure(destination: Path, *, at: datetime) -> None:
    """Stamps a failed attempt, **keeping** the last success.

    Both, deliberately. "The last attempt failed" and "the last good copy is
    from Tuesday" are different facts and an operator needs both: the first
    says something is broken, the second says how much is at risk.

    No reason string. A failure reason is an exception message, and an
    exception message from a `pg_dump` invocation can carry a connection
    string — the log line has it, redacted, and this file is written to a
    directory that may be shared storage.
    """
    _write(
        destination,
        {
            "succeeded_at": _existing(destination).get("succeeded_at"),
            "failed_at": at.astimezone(UTC).isoformat(),
            "last_outcome": "failed",
            "archive": _existing(destination).get("archive"),
        },
    )


def record_offsite_success(destination: Path, *, archive: str, key: str, at: datetime) -> None:
    """Stamps a successful off-host copy — A64-028.7 (P2-8).

    A **separate** timestamp from the local backup's, and that separation is
    the point: a deployment whose archives are written locally and never
    uploaded has a fresh `succeeded_at` and a stale `offsite_at`, and only
    the second says the host loss it is meant to survive is still fatal.
    """
    existing = _existing(destination)
    _write(
        destination,
        {
            **existing,
            "offsite_at": at.astimezone(UTC).isoformat(),
            "offsite_archive": archive,
            "offsite_key": key,
            "offsite_outcome": "succeeded",
        },
    )


def record_offsite_failure(destination: Path, *, at: datetime) -> None:
    """Stamps a failed upload, **keeping** the last success.

    Both facts again: "the last upload failed" and "the last off-host copy
    is from Tuesday" are different, and an operator needs the second to know
    how much is at risk.
    """
    existing = _existing(destination)
    _write(
        destination,
        {
            **existing,
            "offsite_failed_at": at.astimezone(UTC).isoformat(),
            "offsite_outcome": "failed",
        },
    )


def read(destination: Path) -> BackupStatus:
    """The last recorded outcome, or an empty status.

    A missing or unreadable file reads as "never succeeded" rather than
    raising. That is the safe direction: an operator whose status file was
    deleted should see the alert that a backup is overdue, not an error
    from the thing that was supposed to tell them.
    """
    raw = _existing(destination)
    return BackupStatus(
        succeeded_at=_instant(raw.get("succeeded_at")),
        failed_at=_instant(raw.get("failed_at")),
        last_outcome=str(raw.get("last_outcome", "unknown")),
        archive=raw.get("archive") if isinstance(raw.get("archive"), str) else None,
        offsite_at=_instant(raw.get("offsite_at")),
        offsite_outcome=str(raw.get("offsite_outcome", "unknown")),
    )


def _existing(destination: Path) -> dict[str, str]:
    path = destination / STATUS_FILENAME
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write(destination: Path, payload: dict[str, Any]) -> None:
    path = destination / STATUS_FILENAME
    try:
        destination.mkdir(parents=True, exist_ok=True)
        # Written whole and renamed, like the dump itself: a status file
        # truncated by a crash mid-write would read as "never succeeded"
        # and page somebody about a backup that was fine.
        partial = path.with_suffix(".json.partial")
        partial.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        partial.replace(path)
    except OSError:
        logger.exception("backup_status_write_failed", extra={"destination": str(destination)})


def _instant(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


__all__ = [
    "NEVER",
    "STATUS_FILENAME",
    "BackupStatus",
    "read",
    "record_failure",
    "record_success",
]
