"""What an administrator may read about accounts — A64-024.3.

A **separate published port** from `UserSearch`, and the split is the whole
design rather than a naming preference.

`users.public.search` answers a *player's* question: it applies privacy,
excludes blocked players, and searches usernames and display names because
that is what one player knows about another. Widening it for the admin
console would have widened it for every caller — including the public
profile search — and the widening would have been invisible at the call
sites that did not want it.

This answers an *operator's* question instead: every account, unfiltered by
privacy, findable by the two identifiers an operator actually has.

## What it deliberately cannot do

There is no write here. Not a status change, not a role, not a password
reset — nothing on this port can alter an account, so a compromised admin
transport could enumerate and could change nothing. A64-024.3 is read-only
(see `specs/admin.md` §10), and this port is where that is structural
rather than a promise.

## Why `email` is on it at all

An operator's most common starting point is a support request, and a
support request carries an address. Omitting it would make the console
unusable for the one task it exists for, and would push operators back to
`psql` — which is a worse place for this data to be read.

It is not on `AdminUserRecord` by accident: §5 of A64-024.3 lists it as
exposed deliberately, and everything genuinely secret — the password hash,
tokens, OTP material — is absent from this type and therefore unreachable
through it.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class AdminUserRecord:
    """One account, as an administrator sees it in a list.

    Primitive-only and **deliberately small**. Everything a console row
    renders and nothing more: no bio, no country, no avatar key, no
    preferences, and none of the credential material that lives on the same
    table.
    """

    id: UUID
    username: str
    email: str
    display_name: str | None
    is_active: bool
    is_verified: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AdminAccountSummary:
    """How many accounts arrived recently — A64-024.9.

    **Two windows and no total.** A platform's account count is an
    unbounded `COUNT(*)` that answers nothing an operator acts on, and it
    grows more expensive every day precisely because it never shrinks. What
    is worth knowing is whether registration is happening at the rate it
    was, which is a bounded range scan over `ix_user__created_at_id`.
    """

    registered_last_day: int
    registered_last_week: int


@dataclass(frozen=True, slots=True)
class AdminUserFilters:
    """The two filters the current schema supports without inventing indexes.

    `None` means "either" for both, so an unfiltered list is the default and
    a caller states only what it narrows.

    **An admin-role filter is deliberately absent** — §4 permits only
    filters the schema supports naturally, and role lives in another schema
    (`admin.role_assignment`). Filtering on it would mean either a
    cross-schema join, which DB-03 forbids, or fetching every administrator
    and post-filtering, which breaks keyset pagination. The role is
    *displayed* instead, batched by the caller.
    """

    is_active: bool | None = None
    is_verified: bool | None = None


@dataclass(frozen=True, slots=True)
class AdminUserPage:
    """One page, and the cursor that continues it.

    `next_cursor` is `None` exactly when there is no further page — decided
    by over-fetching one row rather than by a second `COUNT(*)`, which on
    this table would be a sequential scan per page.

    There is **no total count**, deliberately. An operator needs "are there
    more" and not "there are 41,208", and the second costs a full scan on
    every page of every search.
    """

    records: Sequence[AdminUserRecord]
    next_cursor: str | None


class AdministrativeUserDirectory(Protocol):
    """Reads accounts for the admin console. **No write exists.**"""

    async def list_accounts(
        self,
        *,
        term: str | None,
        filters: AdminUserFilters,
        limit: int,
        cursor: str | None,
    ) -> AdminUserPage:
        """One page of accounts, newest first.

        `term` matches a **username or an email address by prefix**, both of
        which are covered by unique btree indexes (`uq_user__username_folded`,
        `uq_user__email`). Substring matching on email is not offered: it
        cannot use either index and would be a sequential scan on every
        keystroke, which §3 rules out.

        `None` lists everything, ordered by `(created_at, id)` — the
        composite `ix_user__created_at_id` exists for exactly this, and the
        `id` tiebreak is what makes the keyset total rather than
        approximately ordered.

        Bounded by `limit` whatever the term. There is no unbounded form
        and no query language: a caller supplies a term, two optional
        booleans and a cursor, and can express nothing else.
        """
        ...

    async def accounts_by_ids(self, user_ids: Sequence[UUID]) -> Mapping[UUID, AdminUserRecord]:
        """Every named account, in **one** query — A64-024.4 §8.

        The batch the Matches console needs: a page of matches names up to
        twice as many players, and resolving each one individually is the
        N+1 §8 exists to forbid.

        **Incomplete on purpose**: an id that matches nothing is simply
        absent from the mapping rather than raising or mapping to a
        placeholder. A match whose participant was erased is a real state,
        and the caller renders the id it already has.

        An empty sequence returns an empty mapping without touching the
        database.
        """
        ...

    async def account_summary(
        self, *, since_day: datetime, since_week: datetime
    ) -> AdminAccountSummary:
        """Registrations in two windows, in **one** statement.

        Both counts come from a single range scan from `since_week` with a
        `FILTER` for the shorter window — two statements would scan the same
        index twice for the same rows, and the shorter window is a subset of
        the longer one by construction.

        The instants are passed in rather than read here: this port has no
        clock, and a reader that took `now()` from the database would give
        an answer the caller cannot reproduce in a test.
        """
        ...

    async def find_account(self, user_id: UUID) -> AdminUserRecord | None:
        """One account, or `None` if no such id exists.

        `None` rather than raising, and rather than distinguishing "no such
        account" from anything else: the caller is already an authenticated
        administrator, so there is no enumeration concern here — what there
        is, is a route that should answer `404` without a `try`.
        """
        ...


__all__ = [
    "AdminAccountSummary",
    "AdminUserFilters",
    "AdminUserPage",
    "AdminUserRecord",
    "AdministrativeUserDirectory",
]
