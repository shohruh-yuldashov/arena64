"""`friends` — the social graph: requests, friendships and blocks.

architecture.md §6 reserves this bounded context and `specs/friends.md`
describes it. A64-013.2 builds the first third of it — `FriendRequest` and
nothing else. `Friendship` is A64-013.3 and `Block` is A64-013.5, and
neither has a table, a type or a stub here: this module contains what it
implements.

Everything other modules may import lives in `public/`. Everything else,
including the ORM model and the repository, is private (BE-03).
"""
