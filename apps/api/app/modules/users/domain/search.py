"""`SearchTerm` — what a player typed, once it is safe to search with.

Framework-free like the rest of `domain/` (architecture.md §8). No SQL, no
session, no knowledge that PostgreSQL is involved — this type answers one
question, *is this a searchable term and what exactly are we searching
for*, and it is the only place that answers it.

## Why a value object rather than a validated string parameter

Three rules travel together here and every one of them is a security
control rather than a nicety: a minimum length, a ban on content-free
terms, and the escaping of pattern metacharacters. A caller that applied
two of the three would have a working search with a hole in it, and the
hole would be invisible in review because the two lines that *are* there
look correct.

Making it a type means the repository's signature cannot accept a raw
string, so there is no path to the database that skips them.

## What is deliberately *not* done here

**Case folding, accent stripping and Unicode normalisation.** They are done
in SQL, by the same expression the index is built on — see
`SqlAlchemyUserRepository.search`.

That is the opposite of what this codebase does for `username_folded`,
where Python's `fold_username` and PostgreSQL's generated column implement
the same rule twice in two languages and a contract test pins them
together. That duplication is tolerable there because it is *one*
comparison on an exact-match lookup, and a drift produces a failed login
somebody reports.

Here it would be intolerable. A drift between a Python-normalised term and
a PostgreSQL-normalised column produces a search that silently returns
nothing for the affected characters — nobody reports "I searched for a name
with an umlaut and got no results", they conclude the person is not on the
platform. So the term crosses into SQL as close to raw as safety allows,
and one expression normalises both sides.

`fold_username`'s docstring records the concrete case: PostgreSQL's
`lower()` is not Python's `casefold()`, and the two already disagree about
`ß`.
"""

import re
from dataclasses import dataclass
from typing import Final

from app.modules.users.domain.exceptions import InvalidSearchTerm

#: A64-013.1's bounds, in characters, applied **after** trimming.
#:
#: Two is a real floor rather than a round number: a single character
#: matches a substantial fraction of any user table, which is a full scan
#: wearing a query's clothes and the cheapest possible enumeration probe.
#: Fifty matches `DISPLAY_NAME_MAX_LENGTH`, because a term longer than the
#: longest possible name cannot match anything and there is no reason to
#: send one to the database to find that out.
SEARCH_TERM_MIN_LENGTH: Final = 2
SEARCH_TERM_MAX_LENGTH: Final = 50

#: Page size for search, deliberately smaller than the platform's
#: `MAX_PAGE_SIZE` of 100.
#:
#: A search row is not a cheap row. Every result is a *composed* public
#: profile — identity, ratings, statistics and presence, each gated by its
#: own privacy flag — so a page costs more to build than a roster page of
#: the same length, and it is served on an endpoint whose whole risk profile
#: is somebody asking for many of them quickly.
#:
#: Fifty is also the point past which a ranked result stops being useful: if
#: the person you are looking for is not in the first fifty matches for a
#: term, a longer page is not the fix — a better term is.
DEFAULT_SEARCH_PAGE_SIZE: Final = 20
MAX_SEARCH_PAGE_SIZE: Final = 50

#: The three characters `LIKE` treats specially. `\` is included because it
#: is the escape character itself — escaping `%` and `_` while leaving `\`
#: alone lets a term ending in a backslash escape the delimiter that
#: follows it.
_LIKE_METACHARACTERS: Final = ("\\", "%", "_")

#: Characters a client may not send at all, as opposed to characters that
#: are merely escaped.
#:
#: `%` and `*` are the two things somebody reaching for a wildcard actually
#: types, and A64-013.1 requires wildcard searches to be rejected. They are
#: escaped *as well* — see `_escape_like` — so this ban is the second lock
#: rather than the only one, and its real job is to answer the client
#: honestly: a `422` saying "wildcards are not supported" is a better
#: answer than silently searching for a literal percent sign and returning
#: nothing.
#:
#: **`_` is deliberately absent from this set.** It is a legal username
#: character (`_USERNAME_PATTERN`), so `player_one` is a name somebody has,
#: and rejecting it would make a large share of the platform's handles
#: unsearchable. It is escaped instead, which is the correct handling: a
#: literal underscore, matched literally.
_REJECTED_CHARACTERS: Final = frozenset({"%", "*"})

#: A term has to contain something that can identify a person. Usernames
#: are `[a-zA-Z0-9_]` and display names may be any script, so "alphanumeric
#: in any language" is the widest correct definition.
_HAS_ALPHANUMERIC = re.compile(r"\w", re.UNICODE)


@dataclass(frozen=True, slots=True)
class SearchTerm:
    """A term that is safe to build a `LIKE` pattern from.

    Frozen, and constructed only through `parse` below. Holding the raw and
    the escaped forms together is what lets the repository build a pattern
    without re-deriving anything, and what lets the presentation layer log
    a *length* without ever holding a reason to log the text.
    """

    value: str
    """The trimmed term, exactly as the player typed it apart from
    surrounding whitespace.

    Still un-normalised: not lowercased, not accent-stripped. SQL does
    both, to the term and to the column, with one expression — see this
    module's docstring.

    **Never logged.** A64-013.1: "never log raw search text." A search log
    is a record of who looked for whom, and on a platform with private
    accounts that is more sensitive than the profile it leads to.
    """

    pattern: str
    """`value` with every `LIKE` metacharacter escaped.

    Ready to be wrapped in `%...%` or suffixed with `%` by the repository,
    which supplies the `ESCAPE '\\'` clause. Precomputed here rather than in
    the repository so that "the term is escaped" is a property of the type
    and not of remembering to call something.
    """

    @property
    def length(self) -> int:
        """What may be logged about a search — A64-013.1 asks for the query
        *length*, and this is the only number derived from the term that is
        safe to record."""
        return len(self.value)

    @classmethod
    def parse(cls, raw: str) -> "SearchTerm":
        """Validates and escapes, or raises `InvalidSearchTerm` (422).

        The order of the checks is the contract, because each message tells
        the client something different and the first one to fire is the one
        they see:

            1. trim, then reject empty — "you sent nothing"
            2. reject an explicit wildcard — "that is not supported here"
            3. length bounds — "too short to be useful, too long to match"
            4. reject a term with no alphanumeric character — "that cannot
               identify anybody"

        Rejecting *before* the length check would tell a client sending
        `%%` that their term is too short, which is true and unhelpful.

        Every rejection is a `422` carrying a message about the *shape* of
        the term and never about whether anything matched — an endpoint
        that answered differently for a term that found nobody would be an
        enumeration oracle regardless of what it returned.
        """
        term = raw.strip()

        if not term:
            raise InvalidSearchTerm("A search term is required.")

        rejected = _REJECTED_CHARACTERS.intersection(term)
        if rejected:
            raise InvalidSearchTerm(
                f"Wildcard searches are not supported; remove {' and '.join(sorted(rejected))}."
            )

        if len(term) < SEARCH_TERM_MIN_LENGTH:
            raise InvalidSearchTerm(
                f"A search term must be at least {SEARCH_TERM_MIN_LENGTH} characters."
            )
        if len(term) > SEARCH_TERM_MAX_LENGTH:
            raise InvalidSearchTerm(
                f"A search term must be at most {SEARCH_TERM_MAX_LENGTH} characters."
            )

        if not _HAS_ALPHANUMERIC.search(term):
            # Punctuation alone cannot identify a player, and a term of
            # nothing but separators is the shape a scan probe takes once
            # the explicit wildcards above are refused.
            raise InvalidSearchTerm("A search term must contain at least one letter or digit.")

        return cls(value=term, pattern=_escape_like(term))


def _escape_like(value: str) -> str:
    """Escapes `LIKE`'s three metacharacters with a backslash.

    `\\` **first**, and the ordering is load-bearing: escaping `%` before
    `\\` would turn `100%` into `100\\%` and then into `100\\\\%`, which
    matches a literal backslash followed by anything — the wildcard the
    escaping was meant to remove, reintroduced by the escaping itself.

    The repository pairs this with an explicit `ESCAPE '\\'`. PostgreSQL's
    default escape character *is* the backslash, so the clause is
    redundant today and is written anyway: `standard_conforming_strings`
    and the `LIKE` escape default are separately configurable, and a
    pattern whose safety depends on a server setting is one that is safe
    until somebody changes the setting.
    """
    escaped = value
    for character in _LIKE_METACHARACTERS:
        escaped = escaped.replace(character, f"\\{character}")
    return escaped
