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
from app.modules.users.public.dtos import PublicUserProfile, UserRead


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


class UserProfileReader(Protocol):
    """Reads one account's own view by identifier.

    The third narrow port, added by A64-011.5 for `GET /auth/me` and
    `POST /auth/refresh`. Both need the *profile* behind an identifier the
    caller has already proven — a token's `sub`, or a session's
    `user_id` — and neither may reach into `users`' internals to get it
    (R-1: reach a module through its services, never its storage).

    Separate from `UserAccountCreator` and `UserCredentialStore` for the
    reason those are separate from each other: creating an account,
    reading a password hash and reading a profile are three different
    capabilities, and a component granted one should not thereby hold the
    others. A single `UserService`-shaped port would hand `auth` the
    ability to rename people.

    Read-only by construction. There is no way here to change anything,
    which is what makes it safe to hand to any module that needs to render
    "who is this".
    """

    async def get_profile(self, user_id: UUID) -> UserRead:
        """The account's own view.

        Raises `UserNotFound` rather than returning `None`: every caller
        holds an identifier it has already authenticated, so absence is a
        genuine failure — an account deleted while a valid token was still
        in flight — rather than an ordinary outcome. Making that the
        exceptional path means no caller writes `if user is None` on a
        branch that should never be taken.
        """
        ...

    async def find_by_email(self, email: str) -> UserRead | None:
        """The same view, by address, **returning `None` when absent.**

        The opposite convention to `get_profile` above, and deliberately
        so — the difference is what the caller knows. `get_profile` takes
        an identifier the caller already authenticated, so absence is a
        failure. This takes an address a *stranger* typed into a resend
        form, where absence is the most ordinary outcome there is.

        Raising here would be the leak rather than a nicety:
        `EmailVerificationService.resend_verification` must behave
        identically for a known and an unknown address, and an exception
        is a branch. Matching is case-insensitive (AC-1).
        """
        ...


class EmailVerifier(Protocol):
    """Marks an account's address verified.

    The fourth narrow port, added by A64-011.6. `auth` owns the *proof* —
    it issues the token, checks the expiry and enforces one-time use —
    and `users` owns the *column*. This port is the seam between them,
    and it is one method wide for the reason the other three are: a
    component that may confirm an email must not thereby gain the ability
    to rename people or read password hashes.

    Deliberately **not** paired with an `unverify`. Nothing on the
    platform un-proves ownership of an address; an email *change* is a
    different transition that A64-011.7 or later will model as verifying
    a new address, not as reverting this one.
    """

    async def mark_email_verified(self, user_id: UUID) -> UserRead:
        """Idempotent — verifying an already-verified account succeeds and
        writes nothing. Raises `UserNotFound` if the account is gone,
        which on this flow means it was deleted between the token being
        issued and the link being clicked.
        """
        ...


class PasswordResetter(Protocol):
    """Replaces an account's stored password hash.

    The fifth narrow port, added by A64-011.7. The split is the same one
    the other four make and it lands harder here than anywhere: `auth`
    owns the *proof* — it issues the reset token, checks the expiry,
    enforces one-time use, and computes the Argon2id hash — while `users`
    owns the *column*. Neither can do the other's half.

    **Separate from `UserCredentialStore`, which already has a
    password-writing method.** That is not an oversight and merging them
    would be a real loss. `UserCredentialStore` exists to serve sign-in:
    it can read a password hash, and its write is a compare-and-swap that
    can only re-encode a password the caller has already verified. This
    port cannot read anything and its write is unconditional. A component
    holding both could read a hash and then overwrite it; a component
    holding only this one can replace a credential but never learn what it
    was replacing, which is precisely the authority a reset flow needs and
    the most it should have.

    Deliberately **not** paired with a `get_password_hash`. Nothing about
    a reset requires knowing the old credential — that is what makes it a
    *reset* rather than a change — and a reader here would hand the
    published surface the one capability `UserCredentialStore` keeps
    behind an email lookup.
    """

    async def reset_password(self, user_id: UUID, *, new_hash: str) -> None:
        """Stores `new_hash` as the account's credential, unconditionally.

        No `expected_hash`, unlike `UserCredentialStore`. A recovery flow
        must win a concurrent write rather than lose it: by the time this
        is called the caller has consumed a one-time token and is about to
        revoke every session, and there is nothing useful it could do with
        a declined compare-and-swap except leave somebody locked out of
        their own account.

        Raises `UserNotFound` if the account is gone, which on this flow
        means it was deleted between the link being issued and clicked.
        Returns `None`: absence is raised, so there is no second outcome
        for a return value to report.

        **Does not verify, sanitise or inspect `new_hash`.** It is an
        opaque already-hashed credential, exactly as on `NewUserAccount`.
        The password policy is `auth`'s, applied before hashing.
        """
        ...


class PublicProfileReader(Protocol):
    """Reads the view a stranger may see, by username.

    The sixth narrow port, added by A64-012.1 for `GET /profiles/{username}`.

    **Separate from `UserProfileReader` even though both read profiles**,
    and the split is the whole security design of that endpoint rather
    than bookkeeping. `UserProfileReader` returns `UserRead`, which carries
    the account holder's own email; it exists for `GET /auth/me` and for
    the refresh path, where the caller has already proven the identity it
    is reading. This one is reached by an anonymous request naming somebody
    else, so it returns `PublicUserProfile` — a type with no email field at
    all.

    The consequence worth stating: the `profiles` module cannot leak an
    address, because it is never handed one. That is a stronger guarantee
    than a code review, and it survives a `model_dump()` written by
    somebody who has not read this docstring.

    Read-only by construction, like `UserProfileReader`. There is no way
    here to change anything, which is what makes it safe to hand to a
    module serving unauthenticated traffic.
    """

    async def find_public_profile(self, username: str) -> PublicUserProfile | None:
        """The public view of the account holding `username`, or `None`.

        **Case-insensitive** (UP-1): matching is on the folded form, the
        same one uniqueness is enforced on, so `Alice`, `alice` and `ALICE`
        resolve to one account. The returned `username` preserves the
        casing the player chose.

        Returns `None` rather than raising, and that is a deliberate
        difference from `UserProfileReader.get_profile`. The difference is
        what the caller knows: `get_profile` takes an identifier the caller
        has already authenticated, so absence is a genuine failure. This
        takes a name a stranger typed into a URL, where absence is the most
        ordinary outcome there is — and an exception is a branch, which on
        a public endpoint is a thing that can be timed.

        **A deactivated account has no public profile** and is reported as
        `None`, identically to a username nobody registered — the same
        return, with nothing for a caller to branch on.

        That rule is enforced here rather than by the consumer because
        `users` owns `is_active`. The alternative would be publishing the
        flag on `PublicUserProfile` so a consumer could apply it, and
        "which accounts are deactivated" is itself a disclosure — see
        `PublicProfileService` for the argument. A consumer of this port
        cannot render a withdrawn account even if it tries.
        """
        ...
