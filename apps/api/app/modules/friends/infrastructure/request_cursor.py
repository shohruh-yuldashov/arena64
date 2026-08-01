"""The friend-request list cursor — a keyset over `(created_at, id)`.

Infrastructure rather than domain, for the reason
`app.core.pagination.encode_cursor` gives: a cursor encodes an *ordering
key*, and which key is the query's business. This one belongs to the two
list queries in `SqlAlchemyFriendRequestRepository` and to nothing else.

## Why not `app.repositories.pagination.paginate_cursor`

That helper exists and is the platform's default, and it does not fit here
for one specific reason: it orders **ascending**. A request list is newest
first — the thing you have not answered yet is the thing you just received
— so this keyset descends, which inverts both the `ORDER BY` and the cursor
predicate.

Passing a descending order into a helper written for ascending would mean
the comparison and the ordering disagreeing, and the symptom is an empty
second page rather than an error. Widening `paginate_cursor` with a
direction flag was the alternative and is what CLAUDE.md §2.3 rules out: a
boolean that selects behaviour is two functions wearing one name, and the
two would differ in the operator *and* the tuple order.

## Why the term is not part of this cursor

Unlike `users.infrastructure.search_cursor`, there is nothing to bind to.
A search cursor carries a rank computed against a term, so replaying it
against another term is meaningless; a request cursor carries an instant and
an id, which mean the same thing on every page of every filter. The party
is not encoded either — it comes from the access token, so a cursor cannot
be replayed against somebody else's list.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from app.core.pagination import decode_cursor, encode_cursor
from app.modules.friends.domain.exceptions import InvalidFriendRequestCursor

#: How many values a decoded cursor must carry. Checked explicitly so a
#: cursor from an older encoding fails as a `422` naming the problem rather
#: than as an `IndexError` two lines later.
_CURSOR_FIELDS: Final = 2


@dataclass(frozen=True, slots=True)
class RequestCursor:
    """Where the previous page stopped, in the ordering the query imposes.

    The two values are exactly the `ORDER BY`, in the same order — the
    invariant that makes a keyset correct rather than approximate. `id` is
    the unique tiebreak: `created_at` alone is not unique, and a keyset
    without one skips or repeats rows at a page boundary.
    """

    created_at: datetime
    request_id: UUID

    def encode(self) -> str:
        return encode_cursor(self.created_at, self.request_id)

    @classmethod
    def decode(cls, cursor: str) -> "RequestCursor":
        """Parses a cursor, or raises `InvalidFriendRequestCursor` (422).

        `decode_cursor` returns JSON-native values and deliberately does not
        reconstruct types, so both are parsed here rather than assumed — a
        cursor whose instant arrived as a number is malformed, not an
        instant at the epoch.
        """
        try:
            values = decode_cursor(cursor)
        except ValueError as error:
            raise InvalidFriendRequestCursor("The pagination cursor is malformed.") from error

        if len(values) != _CURSOR_FIELDS:
            raise InvalidFriendRequestCursor("The pagination cursor is malformed.")

        raw_created_at, raw_id = values
        try:
            return cls(
                created_at=datetime.fromisoformat(str(raw_created_at)),
                request_id=UUID(str(raw_id)),
            )
        except ValueError as error:
            raise InvalidFriendRequestCursor("The pagination cursor is malformed.") from error
