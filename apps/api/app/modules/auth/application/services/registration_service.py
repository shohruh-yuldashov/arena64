"""`RegistrationService` — the one use case A64-011.1 implements.

Orchestrates; does not compute (services.md §3.2). The password *policy*
lives in `domain/validators.py`, the *hashing* behind a port, and the
uniqueness rules and the transaction in `users`. What lives here is the
order those happen in, which is the one thing genuinely specific to
registration.

## The flow, and why each step is where it is

    1. validate the password        before hashing, so a rejected password
                                    never costs ~19 MiB and 20ms of Argon2
    2. hash it                      off the event loop, in a worker thread
    3. drop the plaintext           it does not travel past this method
    4. delegate to `users`          which validates username/email, checks
                                    uniqueness, inserts and commits — one
                                    transaction, owned there

Username and email validation is deliberately **not** repeated here.
`users` owns those rules and enforces them in `UserService.create_user`
(and the database enforces uniqueness authoritatively, BE-06). A second
copy in `auth` would be two policies for one concept — and the one that
drifts is always the copy, never the original.

## Why there is no transaction in this method

`users.public.UserAccountCreator.create` is one committed transaction.
Wrapping it in an `auth` transaction would be a cross-module service call
inside an open transaction, which services.md BE-05 forbids: the
resulting lock-acquisition order is something nobody can reason about,
and a partial failure leaves one module committed and the other rolled
back with no record that reconciliation is owed. When `auth` gains tables
of its own (sessions, A64-011.2), anything spanning both modules becomes
an event, not a shared transaction.
"""

import logging

from app.modules.auth.application.commands import NewAccount, RegisterUser
from app.modules.auth.application.ports import PasswordHasher
from app.modules.auth.domain.validators import validate_password
from app.modules.users.public import UserAccountCreator, UserRead

logger = logging.getLogger(__name__)


class RegistrationService:
    def __init__(
        self,
        *,
        accounts: UserAccountCreator,
        password_hasher: PasswordHasher,
    ) -> None:
        self._accounts = accounts
        self._hasher = password_hasher

    async def register(self, command: RegisterUser) -> UserRead:
        """Registers a new account and returns its public view.

        Raises `WeakPassword` when the policy rejects the password, and
        propagates `users`' `InvalidUsername` / `InvalidEmail` /
        `UsernameAlreadyExists` / `EmailAlreadyExists` unchanged — a caller
        branches on the type, and re-wrapping them here would only lose it
        (services.md §7.2 rule 2).
        """
        plaintext = command.password.get_secret_value()

        # Before hashing, not after: a password that fails the policy must
        # not be allowed to consume Argon2's deliberately expensive memory
        # and CPU first. On a public, unauthenticated endpoint that
        # ordering is an availability property, not a micro-optimisation.
        validate_password(plaintext)

        password_hash = await self._hasher.hash(plaintext)

        # Drops this frame's reference before the remaining awaits. This
        # does **not** scrub the string from memory — CPython gives no
        # such guarantee and `command.password` still holds it — so it is
        # not a defence against a memory dump. What it does do is keep the
        # plaintext out of this frame's locals for everything below, which
        # matters because error reporters routinely capture frame locals
        # alongside a traceback, and the calls below are the ones that can
        # raise.
        del plaintext

        created = await self._accounts.create(
            NewAccount(
                username=command.username,
                email=command.email,
                password_hash=password_hash,
                preferred_language=command.preferred_language,
                timezone=command.timezone,
                display_name=command.display_name,
            )
        )

        # `user_id` only. Not the username, not the email — a registration
        # log line is a permanent record, and an address in it is personal
        # data in a system with different retention and broader read access
        # than the database (services.md §8.5). The id is enough to join
        # back to everything else for anyone entitled to see it.
        logger.info("user_registered", extra={"user_id": str(created.id)})
        return created
