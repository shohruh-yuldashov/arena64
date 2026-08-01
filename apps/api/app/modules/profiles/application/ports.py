"""The ports `profiles` programs against — AD-06: declared in
`application/`, satisfied by `infrastructure/` or by another module's
published surface.

Four sources now, and only one of them is still a placeholder. Two of the
four are **not declared here at all**, which is the more interesting half:

    PublicProfileReader   `users.public` — identity. Real.
    PresenceProvider      `users.public` — presence. Real (A64-012.7).
    StatisticsProvider    declared below. Real since A64-012.6, reading
                          `statistics.public` through an adapter.
    RatingProvider        declared below. Still a placeholder — a `rating`
                          module does not exist.

## Why two of the four are consumed as another module's port

`PublicProfileReader` and `PresenceProvider` belong to `users`. They are
imported and used as-is rather than redeclared here, because a local
re-declaration would be a second definition of a contract that already has
an owner, and BR-2 requires a `public/` port be consumed in terms of the
DTOs it publishes.

`StatisticsProvider` is declared here even though `statistics.public`
publishes a structurally identical `StatisticsReader`, and the difference
is not inconsistency. That module's own `public/__init__.py` gives the
argument: `NoMatchesStatisticsProvider` is a fallback that must keep working
when `statistics` is switched off entirely, so it cannot be defined in terms
of that module's port — collapsing the two would make the fallback depend on
the thing it exists to replace.

Presence needs no such split, because both of *its* implementations
(`RedisPresenceProvider`, `NoPresenceProvider`) live inside `users`, which
owns the concept. The fallback and the real adapter answer to the same
owner, so there is nothing for a second port to decouple.

## Why the unbuilt source gets a port rather than an inline default

`ProfileService` could return `PlayerRatings.unrated()` directly and save
two files. The ports exist because of what happens next: when `rating`
ships, the difference between those two designs is a *new adapter* against
an interface the service already uses, versus editing the service that
serves the platform's most-read public endpoint.

That is AD-08's shape for cross-context reads, and it is also the only way
the placeholder can be honest. A default buried in a service reads as
data; a `UnratedRatingProvider` in `infrastructure/` cannot be mistaken
for one — its name is the disclosure.

This is not the "empty `login()` waiting to be filled in" that this
codebase refuses elsewhere. A stub is a *hole* that reads as supported.
These are total, correct implementations of a well-defined state — a
player who has played nothing genuinely has no rating and no record — and
A64-012.1 asks for exactly that ("For now return default values if game
statistics are not yet implemented"). The distinction is that these
produce the right answer for every player today, and will keep producing
the right answer for a brand-new account after `rating` ships.

`PublicProfileReader` is deliberately **not** redeclared here. It is
`users.public`'s type, imported and used as-is: a local re-declaration
would be a second definition of a contract that already has an owner, and
BR-2 requires a `public/` port be consumed in terms of the DTOs it
publishes.
"""

from collections.abc import Mapping, Sequence
from typing import Protocol
from uuid import UUID

from app.modules.profiles.domain.ratings import PlayerRatings
from app.modules.statistics.public import PlayerStatistics
from app.modules.users.public import ViewerRelationship


class RatingProvider(Protocol):
    """Reads a player's current rating in every reported category.

    A `Protocol`, not an ABC, so the placeholder and a future `rating`
    adapter satisfy it structurally without either inheriting from
    anything this module owns.

    Takes a `UUID` — DM-06's `player_id`, the only reference that crosses a
    context boundary. Deliberately not a `PublicUserProfile`: a rating
    system has no business receiving a display name, and a port that
    accepted one would make `profiles` the reason it could read it.
    """

    async def ratings_for(self, player_id: UUID) -> PlayerRatings:
        """Every category, always.

        Returns a complete `PlayerRatings` rather than a partial map, so a
        player with no games in a category yields that category's starting
        snapshot rather than a missing key. A profile whose `ratings`
        object varies in shape by player is one every client has to write
        defensive code against.

        Never raises for an unknown player: a player with no ratings is
        the ordinary case, not a failure, and it is the same answer as a
        player who does not exist — which the caller has already
        established does not apply, because identity is resolved first.
        """
        ...

    async def ratings_for_many(self, player_ids: Sequence[UUID]) -> Mapping[UUID, PlayerRatings]:
        """Every category for a page of players — A64-013.1.

        **Complete**: every id asked for has an entry, for the same reason
        the singular form returns every category — a caller must never have
        to write a fallback for a shape that is always total.

        Free today, because the only implementation returns a constant and
        a loop over it would cost nothing measurable. It exists anyway, and
        that is a deliberate exception to this codebase's usual refusal to
        build for a caller that does not exist: the *caller* very much
        exists (user search renders fifty players at once), and it is the
        day `rating` becomes a real network read that a loop here turns
        into fifty round trips on a page. Widening the port now costs five
        lines; discovering it then costs an incident.
        """
        ...


class StatisticsProvider(Protocol):
    """Reads a player's aggregate match record."""

    async def statistics_for(self, player_id: UUID) -> PlayerStatistics:
        """The counts, always. Zeroes for a player who has finished no
        matches — see `RatingProvider.ratings_for` on why absence is a
        value here rather than an exception."""
        ...

    async def statistics_for_many(
        self, player_ids: Sequence[UUID]
    ) -> Mapping[UUID, PlayerStatistics]:
        """A page of records in one round trip — A64-013.1.

        **Complete**: every id asked for has an entry, defaulting to the
        empty record. A caller indexes rather than defaulting per site.

        Added for user search, which renders up to fifty players at once.
        Looping `statistics_for` would be fifty statements per page — the
        N+1 pattern CLAUDE.md §10.4 names, on the first endpoint this
        module serves that returns a collection.

        Never raises, and an empty sequence costs no round trip.
        """
        ...


class ViewerRelationshipProvider(Protocol):
    """Answers what a viewer is to each of a page of players — A64-013.3.

    The port that makes `VisibilityLevel.FRIENDS` real. Before this task
    every viewer was a `STRANGER` because nothing could compute anything
    else; `PublicProfileComposer` now resolves the relationship per player
    and every privacy gate honours it without changing.

    ## Why `profiles` declares this rather than consuming `friends.public`

    `PublicProfileReader` and `PresenceProvider` are consumed directly from
    `users.public`, so the obvious move is to consume `FriendshipReader`
    from `friends.public` the same way. This module declares its own port
    instead, for the reason `StatisticsProvider` exists beside
    `StatisticsReader`:

    **the fallback must not depend on the module it replaces.**
    `NoRelationshipsProvider` has to keep working when `friends` is
    unreachable or switched off, and a port defined in terms of
    `friends.public` could not. It also keeps the *direction* of the
    dependency clean — `profiles.application` and `profiles.domain` never
    name `friends`, and only `profiles.infrastructure` and the composition
    root do, which is what stops the two modules from becoming mutually
    entangled at the layers that matter.

    ## Batch only

    There is no single-player form, and its absence is deliberate: this runs
    on the composition path, so a per-player call would multiply every
    profile render on the platform (CLAUDE.md §10.4).
    """

    async def relationships_for(
        self, viewer_id: UUID, player_ids: Sequence[UUID]
    ) -> Mapping[UUID, ViewerRelationship]:
        """What `viewer_id` is to each of `player_ids`.

        **Complete**: every id asked for has an entry, defaulting to
        `STRANGER`, so a caller indexes rather than writing a fallback at
        each site — the line somebody eventually writes as `.get(id)` alone
        and then treats `None` as truthy.

        Never raises. A relationship that cannot be determined is
        `STRANGER`, which is the safe direction: an unavailable social graph
        must narrow what a viewer sees, never widen it.

        An empty `player_ids` returns an empty mapping without touching
        anything.
        """
        ...


class BlockedPlayersProvider(Protocol):
    """Who a viewer must never be shown — A64-013.5.

    Separate from `ViewerRelationshipProvider` above even though both read
    the same graph through the same published port, because what differs is
    the *capability*: search needs an exclusion set and never resolves a
    relationship, while composition resolves relationships and never
    excludes anybody. One provider serving both would hand each of them the
    other's.

    Per **viewer**, not per candidate, so one call answers a page of any
    length — which is why there is no batch form and none is needed.
    """

    async def blocked_ids_for(self, player_id: UUID) -> frozenset[UUID]:
        """Every player to exclude from this viewer's results, in **either**
        direction.

        Symmetric even though a block is not: a blocked player must not find
        the blocker either, or the asymmetry itself would be the signal BL-1
        withholds.

        Never raises. An empty set means no blocks — the common case — and
        is also what the fallback returns, which **never fabricates a
        block**: inventing restrictions from missing data is how a kill
        switch becomes an outage.

        A `frozenset` because it is handed straight to
        `UserSearchQuery.exclude_player_ids`, which is frozen.
        """
        ...
