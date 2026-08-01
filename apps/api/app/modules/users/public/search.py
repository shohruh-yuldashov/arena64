"""The shapes `PublicProfileSearcher` is defined in terms of — A64-013.1.

Published from `users.public` because BR-2 requires a `public/` port to be
defined in terms of `public/` types only. Frozen dataclasses rather than
Pydantic DTOs, for the reason `statistics.public.PlayerStatistics` is one:
`profiles` consumes these from its *application* layer and hands the
identities straight into `profiles.domain`, and a domain layer that
imported a framework to hold a value object would be importing a framework
into the one layer architecture.md §8 keeps framework-free.

## Why the query is an object rather than four parameters

Because of the fourth one. `term`, `limit` and `cursor` are what a client
sends; `exclude_player_ids` is what the *platform* decides, and the whole
point of A64-013.1's "prepare architecture for future blocking" is that the
set grows without the port changing shape.

A four-parameter signature would work today and would force every caller
and every fake to be edited on the day `friends` adds blocks — which is
precisely the change this task is asked to make cheap.
"""

from dataclasses import dataclass, field
from uuid import UUID

from app.modules.users.public.dtos import PublicUserProfile


@dataclass(frozen=True, slots=True)
class UserSearchQuery:
    """One search, fully specified.

    Frozen: a query is a description of what was asked for, not a builder.
    Nothing downstream may widen it — a repository that could add an id to
    `exclude_player_ids` would be a repository making a visibility
    decision.
    """

    term: str
    """What the player typed, trimmed and already validated by
    `users.domain.search.SearchTerm`.

    A plain `str` on this boundary rather than the value object itself,
    because `SearchTerm` is domain-private and BR-2 forbids defining a
    published port in terms of an unpublished type. The service on the far
    side re-parses it, which is not duplicated work worth removing: it is
    what makes the rules hold for a caller that did not go through HTTP.
    """

    limit: int
    """How many results to return. Bounded at the edge by
    `MAX_SEARCH_PAGE_SIZE`; the repository over-fetches by one to detect a
    further page without a second query."""

    cursor: str | None = None
    """An opaque cursor from a previous page of **this same term**, or
    `None` for the first page.

    Never constructed by a client. See `UserSearchPage.next_cursor`.
    """

    exclude_player_ids: frozenset[UUID] = field(default_factory=frozenset)
    """Players who must not appear in the results, whatever they match.

    **The blocking seam, and it is load-bearing today rather than reserved
    for later.** A64-013.1 excludes blocking and asks for the architecture
    that will carry it; this is that architecture, and it is exercised on
    every request because the caller already puts the searcher's own id in
    it — see `ProfileSearchService.search`.

    That matters more than it looks. A parameter that is always empty is a
    parameter nobody has proven works: the SQL branch that applies it would
    be dead code until the day `friends` ships, which is the worst possible
    day to discover it was wrong. Having one real member from the first
    release means the exclusion path is covered by every search test on the
    platform.

    A `frozenset` because the query is frozen and because membership is the
    only operation anyone performs on it. Order carries no meaning, and a
    list would invite a caller to think it did.
    """


@dataclass(frozen=True, slots=True)
class UserSearchPage:
    """One page of results, plus what is needed to ask for the next.

    Deliberately **not** `app.core.pagination.CursorPage[PublicUserProfile]`.
    That type is a Pydantic model in `core/`, which would drag Pydantic into
    the values `profiles.domain` holds — the same reason the identities
    below are `PublicUserProfile` DTOs carried whole rather than unpacked.
    The wire shape is the presentation layer's, and it is built from this.

    No `total`, matching `CursorPageInfo` and for RP-03's reason: a count
    over a `LIKE` match gets slower the deeper the term reaches, and a
    search that reports "1 of 4,912 results" has paid for a number nobody
    scrolls to.
    """

    identities: tuple[PublicUserProfile, ...]
    """The matching players, in rank order, already redacted by `users`'
    own mapper — a hidden country is already `None` here.

    A `tuple` because the page is frozen and callers only iterate it.
    """

    next_cursor: str | None
    """Opaque; pass back unchanged to continue. `None` when this is the
    last page.

    Bound to the term it was produced for: the cursor carries a digest of
    the normalised term, and presenting it alongside a different `q` raises
    `InvalidSearchCursor` rather than resuming at a rank that means nothing
    for the new term.
    """

    @property
    def has_more(self) -> bool:
        """Whether a further page exists.

        Derived from `next_cursor` rather than stored beside it, so the two
        cannot disagree — a page reporting `has_more=True` with no cursor is
        a client that loops forever.
        """
        return self.next_cursor is not None
