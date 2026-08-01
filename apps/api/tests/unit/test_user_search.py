"""`SearchTerm` and the search cursor — the two pieces of A64-013.1 that
are pure logic.

Everything about *finding* people needs PostgreSQL and lives in
`tests/contract/test_user_search_api.py`. What is here is the input filter
and the pagination token, both of which are security controls and neither of
which needs a database to assert.

A64-013.1 asks for essential tests only. The empty-query rejection it names
is here; the rest of this file is the small set of properties that would be
*silently* wrong rather than loudly broken — an escape that reintroduces the
wildcard it removes, a cursor that resumes against the wrong term.
"""

import base64
from uuid import UUID

import pytest

from app.modules.users.domain.exceptions import InvalidSearchCursor, InvalidSearchTerm
from app.modules.users.domain.search import (
    SEARCH_TERM_MAX_LENGTH,
    SEARCH_TERM_MIN_LENGTH,
    SearchTerm,
)
from app.modules.users.infrastructure.search_cursor import SearchCursor

PLAYER_ID = UUID("019fb9ea-0a0c-7cec-9c5f-402727c31a96")


class TestRejectedTerms:
    """A64-013.1's "reject empty queries", and the three rules beside it."""

    @pytest.mark.parametrize(
        "raw",
        ["", "   ", "\t\n", " "],
        ids=["empty", "spaces", "whitespace", "non-breaking-space"],
    )
    def test_an_empty_or_blank_term_is_rejected(self, raw: str) -> None:
        """Trimming happens first, so a term of nothing but whitespace is
        empty rather than short — and the message says so, which is the
        difference between a client fixing the bug and adding a character."""
        with pytest.raises(InvalidSearchTerm, match="required"):
            SearchTerm.parse(raw)

    def test_a_single_character_is_rejected(self) -> None:
        """One character matches a substantial fraction of any user table:
        a full scan wearing a query's clothes, and the cheapest possible
        enumeration probe."""
        with pytest.raises(InvalidSearchTerm, match="at least"):
            SearchTerm.parse("a")

    def test_a_term_past_the_maximum_is_rejected(self) -> None:
        with pytest.raises(InvalidSearchTerm, match="at most"):
            SearchTerm.parse("a" * (SEARCH_TERM_MAX_LENGTH + 1))

    @pytest.mark.parametrize("raw", ["%", "%%", "ali%", "*", "ali*ce"], ids=str)
    def test_a_wildcard_is_rejected_rather_than_searched_for(self, raw: str) -> None:
        """A64-013.1: reject wildcard searches. Refused *before* the length
        check, so `%` is told it is a wildcard rather than told it is too
        short — the first message is actionable and the second is not."""
        with pytest.raises(InvalidSearchTerm, match="Wildcard"):
            SearchTerm.parse(raw)

    @pytest.mark.parametrize("raw", ["--", "...", "!!", "  ??  "], ids=str)
    def test_a_term_with_no_alphanumeric_content_is_rejected(self, raw: str) -> None:
        """Punctuation alone cannot identify a player, and is the shape a
        scan probe takes once the explicit wildcards are refused."""
        with pytest.raises(InvalidSearchTerm, match="letter or digit"):
            SearchTerm.parse(raw)


class TestAcceptedTerms:
    def test_surrounding_whitespace_is_trimmed(self) -> None:
        assert SearchTerm.parse("  alice  ").value == "alice"

    def test_the_minimum_length_applies_after_trimming(self) -> None:
        """`" a "` is one character, not three. Checking before trimming
        would accept a term that then matches nothing."""
        with pytest.raises(InvalidSearchTerm, match="at least"):
            SearchTerm.parse("  a  ")

    def test_an_underscore_is_accepted_because_usernames_contain_them(self) -> None:
        """`_` is a `LIKE` metacharacter *and* a legal username character.
        Rejecting it would make a large share of the platform's handles
        unsearchable, so it is escaped instead — matched literally."""
        term = SearchTerm.parse("player_one")

        assert term.value == "player_one"
        assert term.pattern == "player\\_one"

    def test_a_backslash_is_escaped_before_the_characters_it_could_escape(self) -> None:
        """The ordering inside `_escape_like` is the assertion. Escaping `%`
        before `\\` would turn a trailing backslash into an escape for the
        delimiter that follows it, reintroducing the wildcard the escaping
        removes."""
        assert SearchTerm.parse("a\\b").pattern == "a\\\\b"

    def test_the_length_reported_is_of_the_trimmed_term(self) -> None:
        """The only number derived from a term that may be logged —
        A64-013.1 forbids the text itself."""
        assert SearchTerm.parse("  alice  ").length == len("alice")

    def test_the_minimum_is_accepted_at_exactly_the_boundary(self) -> None:
        assert SearchTerm.parse("a" * SEARCH_TERM_MIN_LENGTH).value == "a" * SEARCH_TERM_MIN_LENGTH


class TestSearchCursor:
    def test_a_cursor_round_trips(self) -> None:
        cursor = SearchCursor(rank=2, username_folded="alice", player_id=PLAYER_ID)

        decoded = SearchCursor.decode(cursor.encode(term="ali"), term="ali")

        assert decoded == cursor

    def test_a_cursor_from_another_term_is_refused(self) -> None:
        """The reason the term is in the cursor at all. `rank` is a property
        of a player *relative to a term*, so replaying a cursor against a
        different term resumes at a position that means nothing — silently
        skipping an unpredictable number of people.

        Editing the search box mid-pagination is the single most common
        thing a search UI does, which is what makes this worth guarding.
        """
        cursor = SearchCursor(rank=0, username_folded="alice", player_id=PLAYER_ID).encode(
            term="ali"
        )

        with pytest.raises(InvalidSearchCursor, match="different search"):
            SearchCursor.decode(cursor, term="bob")

    def test_a_cursor_survives_a_change_of_case_in_the_term(self) -> None:
        """Bound case-insensitively: a client that re-sends `Ali` where it
        first sent `ali` is continuing the same search, and refusing it
        would be a guard firing on correct usage."""
        cursor = SearchCursor(rank=1, username_folded="alice", player_id=PLAYER_ID).encode(
            term="ali"
        )

        assert SearchCursor.decode(cursor, term="ALI").rank == 1

    @pytest.mark.parametrize(
        "cursor",
        ["not-base64", "", "eyJhIjogMX0=", "W10="],
        ids=["not-base64", "empty", "not-a-list", "wrong-length"],
    )
    def test_a_malformed_cursor_is_a_422_rather_than_a_crash(self, cursor: str) -> None:
        """A cursor arrives from a client and may be anything. Every
        rejection is the same exception because a caller can do nothing
        different about any of them."""
        with pytest.raises(InvalidSearchCursor, match="malformed"):
            SearchCursor.decode(cursor, term="ali")

    def test_the_readable_term_never_appears_in_the_cursor(self) -> None:
        """A cursor lands in browser history, referrer headers and proxy
        logs. Putting what somebody searched for there would make the
        disclosure `ProfileSearchService` refuses to make in the platform's
        own logs."""
        encoded = SearchCursor(rank=3, username_folded="x", player_id=PLAYER_ID).encode(
            term="gilbert"
        )

        assert "gilbert" not in encoded
        assert b"gilbert" not in base64.urlsafe_b64decode(encoded)
