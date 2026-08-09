"""Administrative authority, as data — A64-024.1.

`database.md` §10.4 specified this table before anything needed it, and its
note is the whole design in one line: **"Moderator authority is data, and
its grant is auditable."**

## Why a grant record and not a flag on `users.User`

A boolean column answers "is this account an admin" and nothing else. This
answers four questions a platform with administrators actually has to
answer, and it does so without a second mechanism:

    who granted it     `granted_by` — an authority nobody can attribute is
                       one nobody can review
    when              `granted_at`
    is it still live  `revoked_at is None`
    what happened     a revoked row stays, so the history of who held
                      authority is readable rather than overwritten

`users.User` is also the wrong owner. `app/operator/__init__.py` records
that it holds "is_active, is_verified — and nothing else", and DM-06's rule
is that a module keyed by `player_id` owns its own facts. Administrative
authority is `admin`'s fact about an account, not `users`' fact about a
person.

## Why revocation is a timestamp rather than a delete

Deleting the row would make a demotion indistinguishable from a grant that
never happened, which is precisely the question an audit asks after an
incident. It also makes the live-grant check a partial index rather than a
full scan, and gives §13.4's future `audit_entry` something to reference.

## One role, and why the column is still an enum

`AdminRole` has a single member today. The column exists as an enum rather
than as an implied "any row means admin" because `database.md` §10.4 names
it, because a second role (`moderator` — §13.3's sanction author) is the
obvious next one, and because a table whose meaning is carried by its
*existence* cannot gain a second meaning without a migration that rewrites
what every existing row meant.

It is deliberately **not** a permission engine. There is no capability set,
no policy evaluation and no inheritance — §2 of A64-024.1 forbids inventing
one, and nothing on this platform has yet asked a question that a role name
cannot answer.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class AdminRole(StrEnum):
    """What authority a grant confers.

    Closed, and lowercase on the wire like every other enum this platform
    persists (`agreed_draw`, `not_a_participant`, `good_game`).
    """

    ADMIN = "admin"
    """Full administrative authority over the platform.

    The only member, and the coarsest one. A narrower `moderator` — able to
    act on reports and sanctions but not on platform configuration — is the
    obvious second, and it is absent because no surface distinguishes them
    yet. Adding it is one member and one row per grant; splitting a boolean
    would have been a migration.
    """


@dataclass(frozen=True, slots=True)
class RoleAssignment:
    """One grant of one role to one account.

    Frozen: a grant is a fact about a moment. Revoking does not mutate it —
    `revoke` returns the revoked form, so the value that was true before is
    still the value that was true before, and a caller cannot silently
    rewrite history in place.
    """

    id: UUID
    account_id: UUID
    role: AdminRole
    granted_by: UUID | None
    """Who granted it, or `None` for the **first** grant on a deployment.

    Nullable for exactly one reason and it is worth stating: the first
    administrator cannot have been granted authority by an administrator,
    because there was none. Every subsequent grant names one, and
    `AdminRoleService.grant` requires it — see `app/operator/admin.py` on
    why the bootstrap is an operator command rather than a route.
    """

    granted_at: datetime
    revoked_at: datetime | None = None

    @property
    def is_live(self) -> bool:
        """Whether this grant confers authority right now.

        The only question the authorization path asks. Expressed here
        rather than as `revoked_at is None` at each call site, so a future
        expiry — a time-boxed grant, say — changes one property instead of
        every guard.
        """
        return self.revoked_at is None

    def revoke(self, *, at: datetime) -> "RoleAssignment":
        """This grant, ended.

        Idempotent: revoking an already-revoked grant returns it unchanged
        rather than moving the instant. The first revocation is when
        authority actually ended, and a retry must not rewrite it.
        """
        if self.revoked_at is not None:
            return self
        return RoleAssignment(
            id=self.id,
            account_id=self.account_id,
            role=self.role,
            granted_by=self.granted_by,
            granted_at=self.granted_at,
            revoked_at=at,
        )


__all__ = ["AdminRole", "RoleAssignment"]
