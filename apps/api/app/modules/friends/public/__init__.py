"""The **only** package other modules may import from `friends` — BE-03.

Everything else under `app.modules.friends` is private. The rule exists
because Python's import system will happily let `profiles` reach into
`friends.infrastructure.models` and query the table directly, and R-1
(architecture.md §7) forbids exactly that — but forbidding it in prose does
not stop it at the hundredth pull request. One named surface makes the rule
a single import-linter contract.

What is published, and why only this much:

  `FriendshipReader`   answers "which of these players is this player
                       friends with", and can do nothing else (A64-013.3)

Deliberately **not** published: the `Friendship` aggregate (it is mutable,
and a consumer holding one could end a relationship this module is
responsible for), `FriendshipRepository` (R-1: reach a module through its
services, never its storage), `FriendRequest` and everything about it, and
both services.

## Why the published port is a *reader* and nothing more

Its one consumer is `profiles`, which needs to know whether a viewer is a
friend in order to evaluate `VisibilityLevel.FRIENDS` — and that is all it
needs. A port that also created or ended friendships would hand the module
serving the platform's highest-volume public read the ability to modify the
social graph.

The narrowing is the same one `users.public` makes twelve times over.
"""

from app.modules.friends.public.ports import FriendshipReader

__all__ = ["FriendshipReader"]
