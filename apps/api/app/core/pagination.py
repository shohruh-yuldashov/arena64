"""Pagination — repositories.md RP-03: keyset (cursor) pagination is the
default because offset pagination gets slower the deeper a page goes and is
unstable under concurrent inserts; offset remains legitimate only for "admin
search over small, bounded result sets where jump-to-page is a genuine
requirement." Both shapes live here because both are legitimate per that
rule — this module does not pick a winner, the query that uses it does.
"""

import base64
import json
import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.core.constants import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE

# `datetime` and `uuid.UUID` are here because they are the two ordering-key
# types every realistic keyset actually uses — `(created_at, id)`, per this
# module's own worked example and database.md's still-open keyset-key
# question. Both round-trip through `encode_cursor`'s `default=str` as
# plain strings; `decode_cursor` intentionally does not try to guess a
# value back into a `datetime` or `UUID` — see `decode_cursor`'s docstring.
CursorValue = str | int | float | uuid.UUID | datetime | None


def encode_cursor(*values: CursorValue) -> str:
    """Encodes an opaque, URL-safe cursor from an ordered tuple of keyset
    values — the "ordering key" RP-03 requires for pagination that is
    stable under concurrent writes and constant-cost at any depth.

    Opaque is deliberate: a client must never construct or interpret a
    cursor, only pass one back unchanged. That is what keeps the encoding
    free to change later, and what stops a client from "paging" by
    fabricating one — `decode_cursor` still validates shape, but not that
    the values inside ever corresponded to a real row.

    Concrete, typed cursors for a specific ordering (e.g. a match-history
    cursor of `(created_at, match_id)`, per database.md's still-open
    keyset-key question) are defined by the module that owns that query —
    this function is the untyped primitive underneath, not a replacement
    for that decision.
    """
    raw = json.dumps(list(values), separators=(",", ":"), default=str)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str) -> list[CursorValue]:
    """The inverse of `encode_cursor`. Raises `ValueError` on a malformed
    cursor; callers translate that into `app.core.exceptions.ValidationError`
    — a client that sends a corrupted cursor sent malformed input, not a
    request for a resource that happens not to exist.

    Returns whatever JSON itself can represent — `str`, `int`, `float`,
    `None` — never a reconstructed `UUID` or `datetime`, even though
    `encode_cursor` accepts both: JSON has no native type for either, and
    guessing which string was originally which is exactly the kind of
    implicit behaviour CLAUDE.md §2.1 rules out. A caller that needs the
    original type back (`app.repositories.pagination.paginate_cursor`)
    binds the decoded string against the target column's own type instead
    of trying to parse it in Python first.
    """
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        decoded = json.loads(raw)
    except Exception as exc:
        raise ValueError("malformed pagination cursor") from exc

    if not isinstance(decoded, list):
        raise ValueError("malformed pagination cursor")
    return decoded


class OffsetPageParams(BaseModel):
    """Query parameters for the offset form. See this module's docstring
    for when offset is the correct choice — it is the documented
    exception, never the default for a growing collection."""

    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)


class CursorPageParams(BaseModel):
    """Query parameters for the keyset form — RP-03's default."""

    cursor: str | None = Field(default=None)
    limit: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)


class PageInfo(BaseModel):
    """Metadata for an offset-paginated result."""

    total: int
    limit: int
    offset: int
    has_more: bool


class OffsetPage[T](BaseModel):
    """An offset-paginated result. Wrap in `app.core.responses.ApiResponse`
    at the route — `ApiResponse[OffsetPage[SomeDTO]]`."""

    items: list[T]
    page: PageInfo


class CursorPageInfo(BaseModel):
    """Metadata for a keyset-paginated result. Deliberately no `total`:
    RP-03's whole point is avoiding a count that gets slower the deeper a
    page goes, so a keyset result never claims to know how many rows
    exist in total.
    """

    next_cursor: str | None
    has_more: bool


class CursorPage[T](BaseModel):
    """A keyset-paginated result — RP-03's default shape. Wrap in
    `app.core.responses.ApiResponse` at the route —
    `ApiResponse[CursorPage[SomeDTO]]`."""

    items: list[T]
    page: CursorPageInfo
