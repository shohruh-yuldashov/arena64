"""`ProfileSearchResults` — a ranked page of composed public profiles.

Framework-free (architecture.md §8), like `PublicProfile` beside it, and for
the same reason: this is the thing being rendered rather than the wire
format. A second consumer — a server-rendered directory page under AD-24, a
gateway pushing search results over a socket — composes the same object
rather than reaching for the searcher and the composer itself.

## Why a type rather than a bare `tuple[list[PublicProfile], str | None]`

Because the cursor and the profiles are one answer, and a tuple invites
callers to take half of it. The property below is the concrete case: whether
another page exists is *derived* from the cursor, and a shape that carried
both separately would let them disagree — a `has_more=True` with no cursor
is a client that loops forever.

It is deliberately **not** `app.core.pagination.CursorPage[PublicProfile]`.
That type is a Pydantic model in `core/`, and `domain/` may not import a
framework; the presentation layer builds the `CursorPage` from this.
"""

from dataclasses import dataclass

from app.modules.profiles.domain.profile import PublicProfile


@dataclass(frozen=True, slots=True)
class ProfileSearchResults:
    """One page of matches, in rank order.

    Frozen, and holding a `tuple` rather than a `list`, because the order
    *is* the result: `profiles` arrives ranked (exact username, then
    username prefix, then display-name prefix, then partial) and a caller
    that sorted it would be discarding the only thing the search computed
    beyond membership.
    """

    profiles: tuple[PublicProfile, ...]
    """The matching players, already composed and already redacted.

    Every element is the same `PublicProfile` a profile page renders, so a
    privacy rule applied there applies here by construction rather than by
    a second implementation agreeing with the first.

    Empty for a term nobody matches — the ordinary outcome, never an error.
    """

    next_cursor: str | None
    """Opaque; pass back unchanged with the **same** term to continue.
    `None` on the last page."""

    @property
    def has_more(self) -> bool:
        """Whether a further page exists. Derived, never stored — see this
        module's docstring."""
        return self.next_cursor is not None
