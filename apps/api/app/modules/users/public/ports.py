"""The ports other modules may depend on — BE-03's published surface.

A64-010 deliberately published none, on the grounds that "publishing a
port before there is a caller is speculative generality... The first real
consumer adds the narrow port it actually needs." `auth` (A64-011.1) is
that first consumer, and `UserAccountCreator` is that narrow port: one
method, exactly what registration needs, and nothing that would let a
consumer read or mutate anything else.

Note what is still *not* published. There is no way here to fetch a user,
list users, change a profile, or reach a `password_hash` — login will need
the last of those, and A64-011.2 should add a second, equally narrow port
for it rather than widening this one into a general-purpose user API. The
value of a published surface is entirely in what it withholds.
"""

from typing import Protocol

from app.modules.users.public.dtos import UserRead


class NewUserAccount(Protocol):
    """The shape `UserAccountCreator.create` accepts.

    A `Protocol` rather than a concrete dataclass so that `auth` can pass
    its own command object without either module importing the other's
    internals — `auth` builds a `RegisterUser`, and it satisfies this
    structurally.

    `password_hash` is an **already-hashed** credential. This module does
    not hash, verify, or inspect it; whoever calls the port has already
    applied the platform's hashing parameters. That split is the whole
    reason `auth` exists: hashing cost, algorithm choice and rotation are
    security decisions with an owner, and it is not `users`.
    """

    @property
    def username(self) -> str: ...

    @property
    def email(self) -> str: ...

    @property
    def password_hash(self) -> str: ...

    @property
    def preferred_language(self) -> str: ...

    @property
    def timezone(self) -> str: ...

    @property
    def display_name(self) -> str | None: ...


class UserAccountCreator(Protocol):
    """Creates a user account and returns its public view.

    Raises `UsernameAlreadyExists` / `EmailAlreadyExists` (also published
    from this package) when a uniqueness rule rejects the request, and the
    relevant `Invalid*` error when a field fails validation. A caller
    branches on those types; it never inspects a message.

    The whole call is one transaction, committed before it returns. A
    caller must not wrap it in a transaction of its own — services.md
    BE-05 forbids a cross-module service call inside an open transaction,
    because the resulting lock-acquisition order is something nobody can
    reason about and a partial failure would leave one module committed
    and the other rolled back with no record that reconciliation is owed.
    """

    async def create(self, account: NewUserAccount) -> UserRead: ...
