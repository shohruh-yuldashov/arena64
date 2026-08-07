"""The keyset cursor the two challenge lists use — A64-022.2 §8.

Infrastructure rather than domain, for the reason
`app.core.pagination.encode_cursor` gives: a cursor encodes an *ordering
key*, and which key is the query's business.

## Why this is not `friends.infrastructure.ListCursor`

It is the same shape over the same ordering key, and it is a separate class
because `friends` and `matchmaking` are separate bounded contexts —
`.importlinter`'s "module internals are private" contract forbids reaching
into another module's infrastructure, and a shared cursor would be exactly
that reach.

Hoisting both into `app/core/pagination.py` is the alternative and is a
better end state; it is not done here because it means moving a class three
`friends` queries depend on, which is a refactor rather than part of this
feature (CLAUDE.md §7.3).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from app.core.error_codes import ErrorCode
from app.core.exceptions import ValidationError
from app.core.pagination import decode_cursor, encode_cursor

_CURSOR_FIELDS: Final = 2


class InvalidChallengeCursor(ValidationError):
    """A cursor that did not come from this API, or was edited.

    A `422` rather than an empty page: a client that sent a malformed cursor
    has a bug, and answering "no more results" would hide it behind what
    looks like the end of the list.
    """

    default_code = ErrorCode.INVALID_CURSOR


@dataclass(frozen=True, slots=True)
class ChallengeCursor:
    """Where the previous page stopped, in the ordering the query imposes.

    The two values are exactly the `ORDER BY`, in the same order — the
    invariant that makes a keyset correct rather than approximate. `id` is
    the unique tiebreak: `created_at` alone is not unique, and a keyset
    without one skips or repeats rows at a page boundary.
    """

    created_at: datetime
    row_id: UUID

    def encode(self) -> str:
        return encode_cursor(self.created_at, self.row_id)

    @classmethod
    def decode(cls, cursor: str) -> "ChallengeCursor":
        """Parses a cursor, or raises `InvalidChallengeCursor`.

        `decode_cursor` returns JSON-native values and deliberately does not
        reconstruct types, so both are parsed here rather than assumed — a
        cursor whose instant arrived as a number is malformed, not an
        instant at the epoch.
        """
        try:
            values = decode_cursor(cursor)
        except ValueError as error:
            raise InvalidChallengeCursor("The pagination cursor is malformed.") from error

        if len(values) != _CURSOR_FIELDS:
            raise InvalidChallengeCursor("The pagination cursor is malformed.")

        raw_created_at, raw_id = values
        try:
            return cls(
                created_at=datetime.fromisoformat(str(raw_created_at)),
                row_id=UUID(str(raw_id)),
            )
        except ValueError as error:
            raise InvalidChallengeCursor("The pagination cursor is malformed.") from error


__all__ = ["ChallengeCursor", "InvalidChallengeCursor"]
