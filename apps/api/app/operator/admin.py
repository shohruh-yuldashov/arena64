"""Operator commands for administrative roles — A64-024.1 §11.

    python -m app.operator.admin grant  --email someone@example.com
    python -m app.operator.admin revoke --email someone@example.com --by boss@example.com

## Why the first administrator is made here and nowhere else

§11 forbids a public "become admin" endpoint, a signup parameter, a role
selector, a hidden query parameter and automatic promotion by email. What
remains is an out-of-band mechanism, and this repository already has the
right one: `app/operator/__init__.py` established the **process** as a real
boundary — "whoever can run a command on the host is already trusted with
the database".

That docstring also predicted this exact task: *"When the Administration
epic ships a role, these commands become the thing its routes call."* This
is the first half of that. `AdminRoleService` is the shared use case; a
future `/admin/roles` route will call the same object behind
`CurrentAdmin`.

## Difficult to invoke accidentally, and it names its target

`--email` rather than an id, because an operator has an address and not a
UUID, and because a mistyped UUID could plausibly match somebody. The
command **prints the account it is about to change and requires
`--yes`** — a promotion that happens on a bare typo is the accident §11
asks to prevent.

There is no default administrator and no hardcoded credential. This grants
authority to an account that already exists and has already signed up
normally; it creates nothing.

## The two refusals that matter

`grant` on a fresh deployment goes through `AdminRoleService.bootstrap`,
which refuses once **any** administrator exists — so the unattributed path
closes behind itself and this command stops being a back door the moment it
has been used. After that, a grant made here is attributed to the operator
account named by `--by`.

`revoke` refuses to remove the **last** administrator, because granting
requires an administrator and `bootstrap` refuses while one exists: the
combination would otherwise let a single command lock a deployment out of
its own admin surface permanently.

## Both commands are audited — A64-024.8

Each writes an `admin.audit_entry` in the same transaction as the grant it
makes or ends. `--by` names the administrator behind the change and the
entry records them; without it the entry records an **operator** action with
no account, which is what a first grant made from a shell actually is.

Recording a stand-in account instead would be worse than recording none: a
reader would have no way to tell it from a real grant by that person.
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.common.logging import configure_logging
from app.config.settings import get_settings
from app.core.clock import SystemClock
from app.database.session_manager import DatabaseSessionManager
from app.database.unit_of_work import SessionUnitOfWork
from app.modules.admin.application.services import AdminRoleService, AuditRecorder
from app.modules.admin.domain.roles import AdminRole
from app.modules.admin.infrastructure.repositories import (
    SqlAlchemyAuditEntryRepository,
    SqlAlchemyRoleAssignmentRepository,
)
from app.modules.users.application.services.user_service import UserService
from app.modules.users.infrastructure.repositories.user_repository import (
    SqlAlchemyUserRepository,
)
from app.modules.users.public import UserProfileService


def _role_service(session: AsyncSession) -> AdminRoleService:
    """The role service, with its recorder, over one session.

    Built here rather than inline in both commands so the two cannot drift
    — a `revoke` wired without the recorder would silently stop auditing,
    and nothing would fail.
    """
    clock = SystemClock()
    return AdminRoleService(
        assignments=SqlAlchemyRoleAssignmentRepository(session),
        audit=AuditRecorder(entries=SqlAlchemyAuditEntryRepository(session), clock=clock),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )


async def _resolve(profiles: UserProfileService, email: str) -> tuple[UUID, str]:
    """The account behind an address, or a failure naming it.

    Returns the id **and** the username so the caller can print what it is
    about to change. An operator confirming "grant admin to 019f…-a3c1" has
    not confirmed anything.
    """
    account = await profiles.find_by_email(email)
    if account is None:
        raise LookupError(f"no account for {email}")
    return account.id, account.username


async def grant(*, email: str, by_email: str | None) -> str:
    """Grants `ADMIN`. Returns a line describing what happened.

    With no `--by`, this is the deployment's **first** administrator and
    goes through `bootstrap`, which refuses if one already exists. With
    `--by`, the grant is attributed to that operator's account — which must
    itself hold `ADMIN`, checked here rather than assumed.
    """
    settings = get_settings()
    database = DatabaseSessionManager(settings.postgres)
    try:
        async with database.session_factory() as session:
            profiles = UserProfileService(
                UserService(
                    users=SqlAlchemyUserRepository(session),
                    unit_of_work=SessionUnitOfWork(session),
                    clock=SystemClock(),
                )
            )
            roles = _role_service(session)

            account_id, username = await _resolve(profiles, email)

            if by_email is None:
                await roles.bootstrap(account_id=account_id, role=AdminRole.ADMIN)
                return f"granted admin to {username} ({email}) — first administrator"

            granter_id, granter_name = await _resolve(profiles, by_email)
            # The granter must actually be an administrator. Without this
            # an operator could attribute a grant to any account at all,
            # which would make `granted_by` a field nobody can trust — and
            # §12's attribution invariant depends on trusting it.
            if AdminRole.ADMIN not in await roles.roles_for(granter_id):
                raise PermissionError(f"{by_email} is not an administrator")

            await roles.grant(account_id=account_id, role=AdminRole.ADMIN, granted_by=granter_id)
            return f"granted admin to {username} ({email}) — by {granter_name}"
    finally:
        await database.close()


async def revoke(*, email: str, by_email: str | None) -> str:
    """Revokes `ADMIN`. Refuses to remove the last administrator.

    `--by` names the administrator ending the grant and must itself hold
    `ADMIN`, checked here for the same reason `grant` checks it: an
    attribution nobody verified is an attribution nobody can trust.
    """
    settings = get_settings()
    database = DatabaseSessionManager(settings.postgres)
    try:
        async with database.session_factory() as session:
            profiles = UserProfileService(
                UserService(
                    users=SqlAlchemyUserRepository(session),
                    unit_of_work=SessionUnitOfWork(session),
                    clock=SystemClock(),
                )
            )
            roles = _role_service(session)
            account_id, username = await _resolve(profiles, email)

            revoked_by: UUID | None = None
            attribution = "operator"
            if by_email is not None:
                revoked_by, revoker_name = await _resolve(profiles, by_email)
                if AdminRole.ADMIN not in await roles.roles_for(revoked_by):
                    raise PermissionError(f"{by_email} is not an administrator")
                attribution = f"by {revoker_name}"

            await roles.revoke(account_id=account_id, role=AdminRole.ADMIN, revoked_by=revoked_by)
            return f"revoked admin from {username} ({email}) — {attribution}"
    finally:
        await database.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.operator.admin",
        description="Administrative role maintenance.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    give = commands.add_parser("grant", help="Grant the admin role to an account.")
    give.add_argument("--email", required=True, help="The account to promote.")
    give.add_argument(
        "--by",
        dest="by_email",
        default=None,
        help="The administrator making the grant. Omit only for the first administrator.",
    )
    give.add_argument(
        "--yes",
        action="store_true",
        help="Confirm. Without it the command prints what it would do and stops.",
    )

    take = commands.add_parser("revoke", help="Revoke the admin role from an account.")
    take.add_argument("--email", required=True)
    take.add_argument(
        "--by",
        dest="by_email",
        default=None,
        help="The administrator making the revocation. Omit to record it as an operator action.",
    )
    take.add_argument("--yes", action="store_true")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Returns a process exit code and prints one line.

    **`--yes` is required to change anything.** A dry run prints the
    intended change and exits `0` without touching the database, so an
    operator who typed the wrong address finds out before the grant rather
    than after it.
    """
    settings = get_settings()
    configure_logging(level=settings.app.log_level, environment=settings.environment)
    arguments = _parser().parse_args(argv)

    if not arguments.yes:
        target = arguments.email
        print(f"would {arguments.command} admin for {target} — re-run with --yes to apply")
        return 0

    try:
        if arguments.command == "grant":
            print(asyncio.run(grant(email=arguments.email, by_email=arguments.by_email)))
        else:
            print(asyncio.run(revoke(email=arguments.email, by_email=arguments.by_email)))
    except Exception as failure:  # noqa: BLE001 — an operator reads a line, not a traceback
        print(f"error: {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
