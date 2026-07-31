"""Command objects — the typed inputs `RegistrationService` accepts.

services.md §3.3 prohibits a service from accepting a Pydantic *request*
model: it would couple the use case to one wire format, so a v2 API or a
non-HTTP caller would force a service rewrite.
"""

from dataclasses import dataclass, field

from pydantic import SecretStr


@dataclass(frozen=True, slots=True)
class RegisterUser:
    """A registration request, with the password still in plaintext.

    `password` is a `SecretStr`, not a `str`, and that is load-bearing
    rather than decorative. This object is exactly the kind of thing that
    ends up in a log line, an exception's `repr`, or a debugger frame —
    `dataclass` generates a `__repr__` that prints every field. `SecretStr`
    renders as `**********`, so the one field that must never be written
    down cannot be, even by accident (dependency-injection.md §2.4: "typed
    as secret values that do not render in reprs — prevents accidental
    exposure through an exception's repr, the most common leak path").

    Reading it requires an explicit `.get_secret_value()`, which is a
    visible, greppable act rather than an implicit one.
    """

    username: str
    email: str
    password: SecretStr
    preferred_language: str
    timezone: str
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class AuthenticateUser:
    """A sign-in attempt.

    `password` is a `SecretStr` for the reason `RegisterUser.password` is
    — see that docstring. It matters more here, if anything: a failed
    sign-in is the outcome most likely to be logged, traced, or captured
    by an error reporter, and it is the one carrying a password somebody
    typed *and got wrong*, which on the next attempt is very often the
    right one.
    """

    email: str
    password: SecretStr


@dataclass(frozen=True, slots=True)
class NewAccount:
    """What `auth` hands to `users.public.UserAccountCreator`.

    Structurally satisfies that port's `NewUserAccount` protocol, so
    neither module imports the other's command type — `auth` builds this,
    `users` accepts anything with these attributes, and the two stay
    decoupled (BR-2).

    Distinct from `RegisterUser` because the password has been *replaced*
    by its hash by this point, not merely accompanied by it. Keeping the
    plaintext on the same object past the hashing step would leave it
    reachable — and therefore loggable — in every frame downstream, for no
    reason at all. There is deliberately no field here that could hold it.
    """

    username: str
    email: str
    password_hash: str = field(repr=False)
    preferred_language: str
    timezone: str
    display_name: str | None = None
