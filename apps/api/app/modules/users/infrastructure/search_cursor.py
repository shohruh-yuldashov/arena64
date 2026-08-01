"""The search cursor — a keyset over `(rank, username_folded, id)`, bound to
the term it was issued for.

Infrastructure rather than domain, for the reason
`app.core.pagination.encode_cursor`'s docstring gives: a cursor is an opaque
encoding of an ordering key, and *which* key is the query's business. This
one belongs to the ranked search in `SqlAlchemyUserRepository.search` and to
nothing else.

## Why the term is part of the cursor

`rank` is not a property of a player. It is a property of a player
*relative to a term* — `alice` ranks 0 for the term "alice" and 3 for the
term "lic". So a cursor issued for one term and replayed against another
resumes at a position that means nothing: the rows before it were never
ranked, and the page it returns is a silent, arbitrary slice.

The failure is invisible without this guard, which is why it is worth the
sixteen bytes. A client paging results and then editing the search box —
the single most common thing a search UI does — would otherwise get a page
of plausible-looking results that skips an unpredictable number of people.

Bound **case-insensitively**: the digest is taken over the casefolded term,
so a client that re-sends `Alice` where it first sent `alice` continues
rather than being refused. Accents are *not* folded here, because doing so
would mean reimplementing PostgreSQL's `unaccent` in Python — the exact
drift this module's neighbours go out of their way to avoid. A client that
changes an accent mid-pagination starts a new search, which is the honest
outcome and not one anybody does by accident.

## Why the digest and not the term itself

The cursor is handed to the client and comes back from it. A cursor
carrying the readable term would put what somebody searched for into their
browser history, their referrer headers and any proxy log on the path —
which is precisely the disclosure `ProfileSearchService` refuses to make in
the platform's own logs.

Sixteen hex characters of SHA-256. Not a security boundary and not claimed
as one: a cursor is already unauthenticated, `decode_cursor` already
validates only shape, and a caller determined to forge one can. What this
prevents is the *accidental* mismatch, which is the one that actually
happens.
"""

import hashlib
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from app.core.pagination import decode_cursor, encode_cursor
from app.modules.users.domain.exceptions import InvalidSearchCursor

#: Truncated deliberately — see this module's docstring on why this is a
#: collision guard rather than a security control. Sixteen hex characters is
#: 64 bits, which makes an accidental collision between two terms a
#: non-event and keeps the cursor short enough to sit in a query string.
_DIGEST_LENGTH: Final = 16

#: How many values a decoded cursor must carry. Checked explicitly so that a
#: cursor from an older encoding fails as a `422` naming the problem rather
#: than as an `IndexError` five lines later.
_CURSOR_FIELDS: Final = 4


def term_digest(term: str) -> str:
    """A stable, short fingerprint of a search term.

    Casefolded first, so paging is not broken by a client that changes the
    capitalisation of what it sends. `casefold` rather than `lower` because
    nothing here has to agree with PostgreSQL — this value never reaches
    SQL, so the stronger Unicode folding is free to be the right one.
    """
    folded = term.casefold().encode("utf-8")
    return hashlib.sha256(folded).hexdigest()[:_DIGEST_LENGTH]


@dataclass(frozen=True, slots=True)
class SearchCursor:
    """Where the previous page stopped, in the ordering the query imposes.

    The three keyset values are exactly the `ORDER BY` of
    `SqlAlchemyUserRepository.search`, in the same order, which is the
    invariant that makes the cursor correct: a keyset that omitted any of
    them, or listed them differently, would skip or repeat rows at a page
    boundary rather than fail visibly.
    """

    rank: int
    """The ranking bucket of the last row on the previous page — 0 for an
    exact username match through 3 for a partial one."""

    username_folded: str
    """The last row's folded username. Unique platform-wide (`UP-1`), so
    `(rank, username_folded)` is already a total order; `id` follows it as
    insurance rather than as a tiebreak that is needed today."""

    player_id: UUID
    """The last row's id."""

    def encode(self, *, term: str) -> str:
        """The opaque string a client passes back."""
        return encode_cursor(term_digest(term), self.rank, self.username_folded, self.player_id)

    @classmethod
    def decode(cls, cursor: str, *, term: str) -> "SearchCursor":
        """Parses a cursor, or raises `InvalidSearchCursor` (422).

        Rejects three separate things with the same exception, because a
        client can do nothing different about any of them: an unparseable
        cursor, a cursor of the wrong shape, and a cursor issued for another
        term. The message distinguishes the last one, since that is the one
        a developer will actually hit and the fix is different — restart
        paging rather than stop corrupting the value.
        """
        try:
            values = decode_cursor(cursor)
        except ValueError as error:
            raise InvalidSearchCursor("The pagination cursor is malformed.") from error

        if len(values) != _CURSOR_FIELDS:
            raise InvalidSearchCursor("The pagination cursor is malformed.")

        digest, rank, username_folded, player_id = values

        if digest != term_digest(term):
            raise InvalidSearchCursor(
                "That cursor belongs to a different search. Start again from the first page."
            )

        # `decode_cursor` returns JSON-native values and deliberately does
        # not reconstruct types (its docstring is explicit), so the shape is
        # checked here rather than assumed. A cursor whose rank arrived as a
        # string is malformed, not a rank of zero.
        if not isinstance(rank, int) or not isinstance(username_folded, str):
            raise InvalidSearchCursor("The pagination cursor is malformed.")

        try:
            return cls(
                rank=rank,
                username_folded=username_folded,
                player_id=UUID(str(player_id)),
            )
        except ValueError as error:
            raise InvalidSearchCursor("The pagination cursor is malformed.") from error
