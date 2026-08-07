"""Operator commands for accounts — A64-021.5H.

    python -m app.operator.accounts verify --email someone@example.com
    python -m app.operator.accounts verify --email one@x.com --email two@x.com

One command, and it exists for two real needs rather than for a test:

    support      "I never got the email" is the single most common account
                 problem any platform has, and the answer cannot be "create
                 another account". Somebody has to be able to confirm an
                 address out of band, having established identity by other
                 means
    seeding      the end-to-end suite's fixture accounts. Every product page
                 requires a verified address now, so a suite whose accounts
                 are unverified tests the verification screen fourteen times

See `app/operator/__init__.py` for why this is a process profile rather than
an `/api/v1/admin` route. It matters more here than for most: an endpoint
that could mark an address verified is an endpoint that removes email
verification from the platform, and it would be reachable by anything that
could reach the API.

## Why it does not print a code

The obvious sibling — "issue a code and show it to me" — is deliberately
absent. It is not possible anyway (what is stored is a keyed verifier, not
the code), and it would be worse if it were: a support tool that reads a
live credential aloud is a phishing script with a company logo on it.

This command changes state and reveals nothing. That is the narrower and
the more auditable capability, and it is the one somebody actually needs.
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence

from app.common.logging import configure_logging
from app.config.settings import get_settings
from app.core.clock import SystemClock
from app.database.session_manager import DatabaseSessionManager
from app.database.unit_of_work import SessionUnitOfWork
from app.modules.users.application.services.user_service import UserService
from app.modules.users.infrastructure.repositories.user_repository import (
    SqlAlchemyUserRepository,
)
from app.modules.users.public import UserProfileService


async def verify(emails: Sequence[str]) -> dict[str, bool]:
    """Marks each address verified. Maps address -> whether it changed.

    Idempotent: an already-verified account is reported as such rather than
    treated as an error, because "make sure this account is verified" is
    what the caller means and it is already true.

    Takes a **sequence** rather than one address so that seeding fourteen
    fixture accounts costs one process rather than fourteen. An unknown
    address still fails the whole run — a caller who named an account that
    does not exist has a typo, and quietly verifying the other thirteen
    would hide it.

    Opens its own session manager, like every operator command: this runs as
    its own process and has no application to borrow one from.
    """
    settings = get_settings()
    database = DatabaseSessionManager(settings.postgres)
    changed: dict[str, bool] = {}
    try:
        async with database.session_factory() as session:
            # The same graph a request builds, assembled by hand because
            # this process has no request to resolve `Depends` against.
            users = UserService(
                users=SqlAlchemyUserRepository(session),
                unit_of_work=SessionUnitOfWork(session),
                clock=SystemClock(),
            )
            profiles = UserProfileService(users)
            for email in emails:
                account = await profiles.find_by_email(email)
                if account is None:
                    raise LookupError(f"no account for {email}")
                if account.is_verified:
                    changed[email] = False
                    continue

                # Through `users`' own service, which owns the invariant
                # that nothing else flips this flag — the same method `auth`
                # calls when a code is redeemed. An operator command that
                # wrote the column directly would be a second writer.
                await users.mark_email_verified(account.id)
                changed[email] = True
        return changed
    finally:
        await database.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.operator.accounts",
        description="Account maintenance.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    confirm = commands.add_parser("verify", help="Mark an email address verified.")
    confirm.add_argument(
        "--email",
        required=True,
        action="append",
        dest="emails",
        metavar="ADDRESS",
        help="The address to confirm. Repeat to verify several in one process.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    settings = get_settings()
    configure_logging(level=settings.app.log_level, environment=settings.environment)
    arguments = _parser().parse_args(argv)

    try:
        outcomes = asyncio.run(verify(arguments.emails))
    except LookupError as missing:
        print(str(missing), file=sys.stderr)  # noqa: T201 — an operator's terminal
        return 1
    except Exception as failure:  # noqa: BLE001 — an operator wants the reason
        print(f"could not verify: {failure}", file=sys.stderr)  # noqa: T201
        return 1

    for email, changed in outcomes.items():
        print(f"{email}: verified" if changed else f"{email}: already verified")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "verify"]
