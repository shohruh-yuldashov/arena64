"""Operator commands for Web Push — A64-021.6 §33, A64-021.7 §15.

    python -m app.operator.push_keys generate
    python -m app.operator.push_keys status

## Why generating a key pair is a command and not a startup fallback

A browser commits to the public key when it subscribes, and its push service
will refuse anything not signed by the matching private half. So changing
the pair does not "rotate a credential" — it **invalidates every existing
subscription, immediately and permanently**, and every browser must
subscribe again.

A platform that generated a pair when it could not find one would therefore
do that silently on any restart that lost its configuration, and the symptom
would be push quietly ceasing to work for everybody who had already enabled
it. There is no error, no failed request, and nothing in a log: the sends
succeed and land nowhere.

So it is a thing somebody types, once, whose output they paste into a secret
manager.

## What `status` reports — A64-021.7

Configuration **and** the delivery queue, in one command, because those are
the two halves of "is push working here" and an operator at 3am should not
have to know they live apart.

The email channel already answered both from
`python -m app.operator.notification_email status`; push answered only the
first, and `build_push_delivery_reader` — built for exactly this in
A64-021.6 — was never called by anything. So an operator could see that a
key pair existed and had no way at all to see whether a single push had ever
been delivered, retried or abandoned. That is the gap this closes.

Aggregate counts only. Nothing here can name a recipient, an endpoint or a
notification: `PushDeliveryRepository.counts_by_status` is the whole surface,
and it returns a mapping of status to integer.

## What `status` will and will not print

Whether a pair is configured, whether the two halves match, and the subject.
**Never the private key**, and there is deliberately no command that prints
one — a support tool that reads a signing key aloud is a way to lose it.

The *public* key is printed, because it is public: every browser that
subscribes is given it, and an operator needs to be able to check that the
one a deployment holds is the one a frontend is offering.
"""

import argparse
import asyncio
import sys
from collections.abc import Mapping, Sequence

from app.common.logging import configure_logging
from app.config.environment import describe_env_file
from app.config.settings import get_settings
from app.database.session_manager import DatabaseSessionManager
from app.modules.notifications.presentation.dependencies import build_push_delivery_reader
from app.platform.push import build_vapid_keys, generate_key_pair


async def delivery_counts() -> Mapping[str, int]:
    """How many push deliveries sit in each status.

    Opens its own session manager, like every operator command: this runs as
    its own process and has no application to borrow one from.

    Typed as the **port** at the seam (`build_push_delivery_reader` returns
    `PushDeliveryRepository`), so this command can count and cannot claim a
    delivery, send one, revoke a device, or learn whose it is.
    """
    settings = get_settings()
    database = DatabaseSessionManager(settings.postgres)
    try:
        async with database.session_factory() as session:
            return await build_push_delivery_reader(session).counts_by_status()
    finally:
        await database.close()


def generate() -> tuple[str, str]:
    """A fresh pair, as the two base64url values that get configured."""
    return generate_key_pair()


def _print_generated() -> int:
    private, public = generate()
    # Written as env assignments so the output can be pasted directly, and
    # with the warning attached rather than in a document nobody opens next
    # to a terminal.
    print("# A64-021.6 — Web Push (VAPID) key pair.")  # noqa: T201 — an operator's terminal
    print("# Store the private key in a secret manager. Never commit it.")  # noqa: T201
    print("# Changing this pair invalidates EVERY existing subscription.")  # noqa: T201
    print(f"VAPID_PUBLIC_KEY={public}")  # noqa: T201
    print(f"VAPID_PRIVATE_KEY={private}")  # noqa: T201
    return 0


def _print_status() -> int:
    """What this process is configured with, and what the queue is doing.

    Never the signing key.
    """
    settings = get_settings().push

    # Which file the configuration came from, first. A process that read no
    # file reports an unconfigured channel and an empty queue, which is
    # indistinguishable from a correctly configured platform with nothing to
    # do — the same trap `notification_email status` names.
    print(f"config: {describe_env_file(get_settings().environment)}")  # noqa: T201

    if settings.vapid_private_key is None or settings.vapid_public_key is None:
        print("push: not configured — VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY are unset")  # noqa: T201
        print("push: this process reports the push channel unavailable")  # noqa: T201
        # The queue is still worth printing: rows enqueued by a node that
        # *was* configured do not disappear because this one is not, and a
        # growing `pending` here is exactly how somebody discovers that the
        # worker tier lost its keys.
        _print_queue()
        return 0

    try:
        keys = build_vapid_keys(settings)
    except ValueError as invalid:
        # The failure worth catching here: a pair that is present and wrong
        # takes the API down at boot, and an operator needs to be able to
        # find out *why* without reading a traceback in a crash loop.
        print(f"push: MISCONFIGURED — {invalid}", file=sys.stderr)  # noqa: T201
        return 1

    if keys is None:
        # Unreachable given the guard above, and handled rather than
        # asserted: `build_vapid_keys` owns the "is it configured" rule, and
        # a second copy of it here that could drift is worse than a branch.
        print("push: not configured", file=sys.stderr)  # noqa: T201
        return 1

    print("push: configured")  # noqa: T201
    print(f"push: subject={keys.subject}")  # noqa: T201
    print(f"push: public_key={keys.public_key_base64}")  # noqa: T201
    _print_queue()
    return 0


def _print_queue() -> None:
    """The delivery queue, by status.

    Every status is printed even at zero: a missing line reads as "not
    measured" and a zero reads as "measured, none", and the difference
    matters at 3am. Sorted into a fixed order so two runs are diffable.

    A queue that cannot be read is reported and does not fail the command —
    the configuration above is still the answer somebody came for, and a
    database that is down is a different alarm than a misconfigured key.
    """
    try:
        counts = asyncio.run(delivery_counts())
    except Exception as failure:  # noqa: BLE001 — an operator wants the reason
        print(f"queue: unavailable ({failure})", file=sys.stderr)  # noqa: T201
        return

    for name in ("pending", "sent", "skipped", "failed"):
        print(f"{name}={counts.get(name, 0)}")  # noqa: T201


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.operator.push_keys",
        description="Web Push (VAPID) key management.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("generate", help="Print a fresh VAPID key pair.")
    commands.add_parser(
        "status", help="Show the key configuration and the delivery queue by status."
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "generate":
        # Deliberately **before** `configure_logging` and without reading
        # settings: generating a key pair must work on a machine that has no
        # database, no Redis and no configuration at all, which is exactly
        # the machine somebody is on when they are setting one up.
        return _print_generated()

    settings = get_settings()
    configure_logging(level=settings.app.log_level, environment=settings.environment)
    return _print_status()


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["delivery_counts", "generate", "main"]
