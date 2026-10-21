"""An account's identity at an outside provider — A64-031.E1.

`domain-model.md` §18 Q-22 asked whether sign-in through an identity
provider would be offered, and recorded the shape it would take: another
way to prove the same account, never a second account. This is that
shape, for the one provider the platform offers (ADR-008).

## Why it lives in `users`

`database.md` §4.3 drew it as a `kind = 'oauth'` row in `auth.credential`.
That table was never built: A64-010 put `email` and `password_hash` on
`users.user` (see `infrastructure/models.py`, "two documented
deviations"), so on this platform *every* credential is `users`' data. An
external identity follows the password it sits beside — and, decisively,
an account created through Google and the link that makes it reachable
must be **one transaction**. Split across two modules they would be two
commits (`registration_service.py` on BE-05), and a failure between them
leaves an account nobody can sign in to.

## Why the subject and never the address

`subject` is the provider's stable, never-reassigned identifier for the
person (OpenID Connect Core §2, `sub`). An address can change at the
provider and can be recycled to somebody else; keying on it would hand
an account to whoever holds the address next. The address is consulted
exactly once — to notice that an Arena64 account already uses it — and
never stored here.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

#: OpenID Connect leaves `sub` at "never exceeding 255 ASCII characters".
SUBJECT_MAX_LENGTH: Final = 255


class IdentityProvider(StrEnum):
    """The providers an account may be linked to. One today."""

    GOOGLE = "google"


@dataclass(frozen=True, slots=True)
class ExternalIdentity:
    """One person at one provider."""

    provider: IdentityProvider
    subject: str

    def __post_init__(self) -> None:
        # Raises `ValueError`, not a domain error: a subject reaches here
        # only from a token whose signature this platform has verified, so
        # a malformed one is a defect upstream, never something a player
        # typed.
        if not self.subject or len(self.subject) > SUBJECT_MAX_LENGTH:
            raise ValueError("An external subject is 1 to 255 characters.")
        if not self.subject.isascii() or not self.subject.isprintable():
            raise ValueError("An external subject is printable ASCII.")
