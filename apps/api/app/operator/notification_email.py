"""Operator diagnostics for the notification email queue — A64-021.5 §21.

    python -m app.operator.notification_email status
    python -m app.operator.notification_email smoke --to someone@example.com

`status` **only reads**. There is no resend, no flush, no retry and no "send
this notification now": §20 makes notification email server-controlled, and
a command that could send somebody else's notification is the capability an
attacker would want from a compromised operator shell.

`smoke` sends **one fixed message to an address the operator types** — never
a notification, never a stored recipient, never a default address. It is the
one way to answer "is the Resend credential in this environment actually
working" without waiting for a tournament, and it is deliberately not
reachable from pytest, from HTTP, or from startup (§8).

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
from app.config.environment import describe_env_file
from app.config.settings import get_settings
from app.database.session_manager import DatabaseSessionManager
from app.modules.notifications.presentation.dependencies import build_email_delivery_reader
from app.platform.email import EmailMessage, build_email_provider, can_deliver_email


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


async def smoke(recipient: str) -> str | None:
    """Sends one fixed message through this environment's real transport.

    Builds the provider the **same way** the application does — same
    selection, same credential, same sender — because a smoke test that
    constructed its own would prove a different code path works.

    Returns the provider's message id, so an operator can find the message in
    Resend's dashboard rather than only in their inbox.
    """
    settings = get_settings()
    provider = build_email_provider(settings.environment, settings.email)
    return await provider.send(
        EmailMessage(
            to=recipient,
            subject="Arena64 — transport check",
            text_body=(
                "This message confirms that Arena64's outbound email transport "
                "is configured in this environment.\n\n"
                "It was sent by an operator command and is not a notification.\n"
            ),
            html_body=(
                '<p style="font-family:sans-serif">This message confirms that '
                "Arena64's outbound email transport is configured in this "
                "environment.</p>"
                '<p style="font-family:sans-serif;color:#666">It was sent by an '
                "operator command and is not a notification.</p>"
            ),
            # **Deliberately absent.** A fixed key would make the second
            # smoke test in 24 hours return the first one's result without
            # sending anything, which is precisely the opposite of what this
            # command is for.
        )
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.operator.notification_email",
        description="Report the notification email queue, and check the transport.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Count deliveries by status.")

    check = commands.add_parser("smoke", help="Send one test message through the transport.")
    check.add_argument(
        "--to",
        required=True,
        help="Where to send it. Required — there is deliberately no default address.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    settings = get_settings()
    configure_logging(level=settings.app.log_level, environment=settings.environment)
    arguments = _parser().parse_args(argv)

    if arguments.command == "smoke":
        return _smoke(arguments.to)

    try:
        counts = asyncio.run(status())
    except Exception as failure:  # noqa: BLE001 — an operator wants the reason, not a traceback
        print(f"could not read the delivery queue: {failure}", file=sys.stderr)  # noqa: T201
        return 1

    # Said before the numbers, because it changes what every one of them
    # means. A process that read no configuration file reports a healthy
    # empty queue and a disabled channel, which is indistinguishable from a
    # correctly configured platform with nothing to do.
    print(f"config: {describe_env_file(settings.environment)}")  # noqa: T201
    print(f"transport: {'resend' if can_deliver_email(settings.email) else 'none (console)'}")  # noqa: T201
    print(f"sender: {settings.email.from_name} <{settings.email.from_address}>")  # noqa: T201
    print(f"origin: {settings.app.public_url}")  # noqa: T201

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


def _smoke(recipient: str) -> int:
    """Runs one send and reports it in a terminal.

    A transport that cannot deliver is reported as a **failure**, because
    that is what an operator ran this to find out. The recipient is echoed —
    they typed it, it is on their screen already, and confirming where it
    went is the point of the command; nothing writes it to a log.
    """
    try:
        message_id = asyncio.run(smoke(recipient))
    except Exception as failure:  # noqa: BLE001 — an operator wants the reason
        print(f"send failed: {failure}", file=sys.stderr)  # noqa: T201
        return 1

    if message_id is None:
        # `ConsoleEmailProvider` returns no id because nothing accepted the
        # message. In a local environment that is the expected answer and
        # says so; anywhere else it means no credential is configured.
        print(  # noqa: T201
            f"no transport accepted the message for {recipient} — "
            "the console provider was used, so RESEND_API_KEY is not set here",
            file=sys.stderr,
        )
        return 1

    print(f"sent to {recipient} (provider message id: {message_id})")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "smoke", "status"]
