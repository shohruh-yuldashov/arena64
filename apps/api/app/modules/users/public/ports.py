"""The ports other modules may depend on — BE-03's published surface.

A64-010 deliberately published none, on the grounds that "publishing a
port before there is a caller is speculative generality... The first real
consumer adds the narrow port it actually needs." `auth` (A64-011.1) is
that first consumer, and `UserAccountCreator` is that narrow port: one
method, exactly what registration needs, and nothing that would let a
consumer read or mutate anything else.

A64-011.2 added the second: `UserCredentialStore`, for login. It is a
separate protocol rather than two more methods on `UserAccountCreator`
because registration and sign-in are different consumers with different
risk — a future component that may create accounts must not automatically
gain the ability to read password hashes.

Note what is *still* not published: there is no way here to fetch an
arbitrary user, list users, or change a profile. The value of a published
surface is entirely in what it withholds.
"""

from typing import Protocol
from uuid import UUID

from app.modules.users.public.credentials import UserCredentials
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


class UserCredentialStore(Protocol):
    """Reads the credential material for a sign-in, and accepts a rehash.

    Two methods, both of which `auth` demonstrably needs and neither of
    which lets a consumer do anything else — it cannot list accounts,
    cannot read a hash by user id, and cannot set a hash to an arbitrary
    value without already knowing the current one.
    """

    async def find_credentials_by_email(self, email: str) -> UserCredentials | None:
        """`None` when no account has that address — **not** an exception.

        This is the one place in the module where absence must be an
        ordinary return value rather than a `UserNotFound`. `auth` has to
        take exactly the same code path, spending exactly the same time,
        for a known and an unknown address; an exception here would make
        "unknown" the cheap branch and hand an attacker an oracle for
        which addresses have accounts. `UserService.find_by_email` raises
        and stays as it is — this is a different question with a
        different answer.

        Matching is case-insensitive (AC-1): the address is normalised the
        same way registration normalised it before storing.
        """
        ...

    async def replace_password_hash(
        self,
        user_id: UUID,
        *,
        expected_hash: str,
        new_hash: str,
    ) -> bool:
        """Upgrades a stored hash in place, returning whether it applied.

        `expected_hash` makes this a compare-and-swap rather than a blind
        write, and that matters even before a change-password flow exists:
        a rehash-on-login is computed from a hash read *earlier* in the
        request, and an unconditional `UPDATE` would silently revert a
        credential changed in between — turning a security upgrade into a
        credential rollback. `False` means the row moved underneath us,
        which is a no-op, not a failure.

        Never changes what the password *is*: the caller has already
        verified the same plaintext against `expected_hash`, and
        `new_hash` encodes that identical plaintext under stronger
        parameters (database.md §14.2's rehash-on-login).
        """
        ...
