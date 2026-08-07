"""Operator diagnostics for the notification email queue — A64-021.5 §21.

    python -m app.operator.notification_email status

One command, and it **only reads**. There is no resend, no flush, no retry
and no "send this one now": §20 makes notification email server-controlled,
and a command that could send an arbitrary message is the same capability an
attacker would want from a compromised operator shell.

See `app/operator/__init__.py` for why this is a process profile rather than
an `/api/v1/admin` route.

## What it deliberately cannot tell you

Whose email is failing. The output is counts by status and nothing else —
no recipient, no address, no notification id (§21, §23). An operator asking
*"is the channel healthy"* gets an answer; one asking *"did Alice get her
email"* does not, and that is the correct boundary for a shell that runs
outside the request path with no audit trail of its own.

If a specific delivery has to be investigated, the delivery row is in the
database behind the same access controls every other table is, and reading
it is a deliberate act rather than a command's side effect.

## Exit codes

    0  read successfully
    1  the read failed

Not "1 if anything is failing". A queue with failed deliveries is a normal
state — a bounced address stays failed forever — and an exit code that
treated it as an error would make this unusable in a health check.
"""

import argparse
import asyncio
import sys
from collections.abc import Mapping, Sequence

from app.common.logging import configure_logging
from app.config.settings import get_settings
from app.database.session_manager import DatabaseSessionManager
from app.modules.notifications.presentation.dependencies import build_email_delivery_reader


async def status() -> Mapping[str, int]:
    """How many deliveries sit in each status.

    Opens its own session manager, like every operator command: this runs as
    its own process and has no application to borrow one from.
    """
    settings = get_settings()
    database = DatabaseSessionManager(settings.postgres)
    try:
        async with database.session_factory() as session:
            return await build_email_delivery_reader(session).counts_by_status()
    finally:
        await database.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.operator.notification_email",
        description="Report the notification email delivery queue.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Count deliveries by status.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    settings = get_settings()
    configure_logging(level=settings.app.log_level, environment=settings.environment)
    _parser().parse_args(argv)

    try:
        counts = asyncio.run(status())
    except Exception as failure:  # noqa: BLE001 — an operator wants the reason, not a traceback
        print(f"could not read the delivery queue: {failure}", file=sys.stderr)  # noqa: T201
        return 1

    if not settings.notification_email.enabled:
        # Said first, because it changes how every number below reads: with
        # the channel off nothing is claimed, so a growing `pending` is the
        # queue waiting rather than the worker failing.
        print("channel: disabled (NOTIFICATION_EMAIL_ENABLED=false)")  # noqa: T201
    else:
        print("channel: enabled")  # noqa: T201

    # Sorted, so two runs are diffable. Every status is printed even at
    # zero: a missing line reads as "not measured" and a zero reads as
    # "measured, none" — and the difference matters at 3am.
    for name in ("pending", "sent", "skipped", "failed"):
        print(f"{name}={counts.get(name, 0)}")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "status"]
