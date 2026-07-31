"""app.core.pagination — repositories.md RP-03."""

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.core.pagination import (
    CursorPage,
    CursorPageInfo,
    CursorPageParams,
    OffsetPage,
    OffsetPageParams,
    PageInfo,
    decode_cursor,
    encode_cursor,
)


class TestCursorEncoding:
    def test_round_trips_mixed_value_types(self) -> None:
        cursor = encode_cursor("2026-07-31T00:00:00Z", 42, 3.5, None)
        assert decode_cursor(cursor) == ["2026-07-31T00:00:00Z", 42, 3.5, None]

    def test_is_url_safe(self) -> None:
        # A cursor travels in a query string; anything that needs
        # percent-encoding there defeats the point of an opaque token.
        cursor = encode_cursor("has spaces & special/chars?", 1)
        assert all(c not in cursor for c in " &/?+")

    def test_two_equal_keysets_produce_equal_cursors(self) -> None:
        # Stability matters: the same position in the same ordering must
        # always encode identically, or a client-cached cursor silently
        # stops matching what the server would produce today.
        assert encode_cursor("a", 1) == encode_cursor("a", 1)

    def test_decode_rejects_garbage(self) -> None:
        with pytest.raises(ValueError, match="malformed pagination cursor"):
            decode_cursor("not-a-real-cursor!!!")

    def test_decode_rejects_a_non_array_payload(self) -> None:
        import base64

        # Valid base64/JSON, but not the list shape a cursor must be.
        cursor = base64.urlsafe_b64encode(b'{"not": "a list"}').decode("ascii")
        with pytest.raises(ValueError, match="malformed pagination cursor"):
            decode_cursor(cursor)


class TestPageParams:
    def test_offset_defaults(self) -> None:
        params = OffsetPageParams()
        assert params.offset == 0
        assert params.limit == 20

    def test_offset_rejects_a_negative_offset(self) -> None:
        with pytest.raises(PydanticValidationError):
            OffsetPageParams(offset=-1)

    def test_offset_rejects_a_limit_beyond_the_platform_maximum(self) -> None:
        with pytest.raises(PydanticValidationError):
            OffsetPageParams(limit=101)

    def test_cursor_defaults_to_no_cursor(self) -> None:
        params = CursorPageParams()
        assert params.cursor is None
        assert params.limit == 20

    def test_cursor_rejects_a_limit_beyond_the_platform_maximum(self) -> None:
        with pytest.raises(PydanticValidationError):
            CursorPageParams(limit=1000)


class TestPageShapes:
    def test_offset_page_serialises_items_and_page_info(self) -> None:
        page = OffsetPage[str](
            items=["a", "b"], page=PageInfo(total=2, limit=20, offset=0, has_more=False)
        )
        dumped = page.model_dump()
        assert dumped["items"] == ["a", "b"]
        assert dumped["page"]["total"] == 2

    def test_cursor_page_has_no_total(self) -> None:
        page = CursorPage[str](
            items=["a"], page=CursorPageInfo(next_cursor=encode_cursor("a"), has_more=True)
        )
        assert "total" not in page.page.model_dump()
