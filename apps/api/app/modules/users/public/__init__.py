"""The **only** package other modules may import from `users` — BE-03.

Everything else under `app.modules.users` is private. The rule exists
because Python's import system will happily let `game` reach into
`users.infrastructure.models` and query the table directly, and rule R-1
(architecture.md §7) forbids exactly that — but forbidding it in prose does
not stop it at the hundredth pull request. One named surface makes the rule
expressible as a single import-linter contract ("nothing may import
`app.modules.users` except `app.modules.users.public`"), which turns a
convention into a build failure.

What is published, and why only this much:

  `UserId`               the identifier every other context refers to a
                         player by (DM-06: `player_id` is the only
                         cross-context reference)
  `UserRead`             the account holder's own view — what registration
                         returns to the person who just registered
  `UserSummary`          the minimal public view, for rendering who
                         someone is without loading their whole profile
  `UserAccountCreator`   the narrow port `auth` uses to register a user
  `NewUserAccount`       that port's input shape
  the four exceptions    so a consumer can branch on the outcome

Deliberately **not** published: the `User` entity (it is mutable, and a
consumer holding one could change fields this module is responsible for),
the repository port (R-1: reach a module through its services, never its
storage), and `UserService` itself — `auth` gets the one method it needs
through `UserAccountCreator`, not the whole class.
"""

from uuid import UUID

from app.modules.users.domain.exceptions import (
    EmailAlreadyExists,
    InvalidEmail,
    InvalidUsername,
    UsernameAlreadyExists,
    UserNotFound,
)
from app.modules.users.public.dtos import UserRead, UserSummary
from app.modules.users.public.ports import NewUserAccount, UserAccountCreator

# The cross-context player identifier. An alias rather than a `NewType`
# because it crosses a JSON boundary in both directions and every consumer
# already holds it as a plain UUID; a stricter wrapper would be stripped at
# the first `model_dump()` anyway.
type UserId = UUID

__all__ = [
    "EmailAlreadyExists",
    "InvalidEmail",
    "InvalidUsername",
    "NewUserAccount",
    "UserAccountCreator",
    "UserId",
    "UserNotFound",
    "UserRead",
    "UserSummary",
    "UsernameAlreadyExists",
]
