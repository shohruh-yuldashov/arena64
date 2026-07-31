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

  `UserId`        the identifier every other context refers to a player by
                  (DM-06: `player_id` is the only cross-context reference)
  `UserSummary`   the minimal read shape, for a module that needs to render
                  who someone is without loading their whole profile
  `UserNotFound`  so a consumer can branch on a missing user

Deliberately **not** published: the `User` entity (it is mutable, and a
consumer holding one could change fields this module is responsible for),
the repository port (R-1: reach a module through its services, never its
storage), and `UserService` itself — no other module needs to call it yet,
and publishing a port before there is a caller is speculative generality
(CLAUDE.md §1 rule 7). The first real consumer adds the narrow port it
actually needs.
"""

from uuid import UUID

from app.modules.users.domain.exceptions import UserNotFound
from app.modules.users.presentation.schemas import UserSummary

# The cross-context player identifier. An alias rather than a `NewType`
# because it crosses a JSON boundary in both directions and every consumer
# already holds it as a plain UUID; a stricter wrapper would be stripped at
# the first `model_dump()` anyway.
type UserId = UUID

__all__ = ["UserId", "UserNotFound", "UserSummary"]
