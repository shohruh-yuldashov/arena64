"""The repository contract — repositories.md §2, §4, §10.

Deliberately **not** a generic `BaseRepository` with `get`/`list`/`filter`/
`save` methods. repositories.md §10 names exactly that shape as the first
anti-pattern in the document: "every module inherits methods it does not
want, and `filter` becomes the generic query API §4 forbids." A repository's
real contract is a set of *use-case-named* queries and mutations specific to
its own aggregate — `find_pairable_opponents`, `list_recent_matches_for_player`
— and there is nothing generic to hoist out of that.

What genuinely is common across every repository, and is captured here, is
the shape of its relationship to the transaction it participates in — not a
shape for the data it moves.
"""

from typing import Protocol


class Repository(Protocol):
    """Marker for a concrete repository port.

    A repository:
      - is constructed with the active unit of work's session, and never
        opens, commits, or rolls back a transaction itself
        (repositories.md §4, §5.1);
      - exposes only methods named for the use case they serve — never a
        generic accessor (repositories.md §4, §10);
      - returns domain entities or explicit read DTOs, never ORM rows, a
        result object, or the session itself (repositories.md §4).

    A concrete port for a specific aggregate is declared as its own Protocol
    in that module's `application/` layer (architecture.md AD-06) and
    satisfies this marker structurally. There is no method to inherit here
    — that absence is the point.
    """
