"""Per-event property schemas — analytics.md §39.

**Not `dict[str, Any]`.** The collector accepts input from browsers and the
projector reads payloads written months ago; both need a shape that says
what is allowed rather than a mapping that says nothing.

## Why the enums are declared here rather than imported

`variant`, `speed_class`, `outcome` and `termination_reason` all exist as
domain enums, and §20 of the document asks for those to be reused. This
module deliberately declares its own instead, and the reason is versioning
rather than layering:

An analytics event's properties are **stored** and read back by a query
written a year later. If a domain enum member were renamed — a refactor the
domain is entitled to make — every historical row would keep the old string
while new rows carried the new one, and no `event_version` bump would have
happened, because from the domain's point of view nothing about analytics
changed. §7's rule ("a changed meaning a reader cannot detect") would have
been broken by a rename in a different module.

So the vocabularies are decoupled, and `test_analytics_enum_mapping.py`
asserts they still describe the same set in both directions. A domain
rename then fails a test that names analytics, which is where the decision
about a version bump belongs.
"""

from enum import StrEnum


class Variant(StrEnum):
    """Mirrors `ProductVariant`. Asserted total against it by a test."""

    RUSSIAN_8X8 = "russian_8x8"


class SpeedClass(StrEnum):
    """Mirrors `rating.SpeedClass`."""

    BULLET = "bullet"
    BLITZ = "blitz"
    RAPID = "rapid"
    CLASSICAL = "classical"
    CORRESPONDENCE = "correspondence"


class Outcome(StrEnum):
    """Mirrors `game.domain.result.MatchOutcome` — the **result**
    vocabulary, not the offer one.

    `game` has two enums spelled `MatchOutcome`: this one, and the
    acceptance vocabulary in `public.metrics` that `OfferResolution` below
    mirrors. Naming them apart here is deliberate — importing the wrong one
    is a mistake the mapping test caught while this module was being
    written. `NONE` is an aborted match — §32 excludes it
    from both sides of the completion rate."""

    WIN = "win"
    DRAW = "draw"
    NONE = "none"


class TerminationReason(StrEnum):
    """Mirrors `game.TerminationReason`.

    Eleven members, and the classification in analytics.md §32 depends on
    every one of them being present — a missing member would silently move
    games out of a denominator.
    """

    NO_LEGAL_MOVES = "no_legal_moves"
    ALL_PIECES_CAPTURED = "all_pieces_captured"
    RESIGNATION = "resignation"
    ABORT = "abort"
    AGREED_DRAW = "agreed_draw"
    REPETITION = "repetition"
    MOVE_LIMIT = "move_limit"
    FLAG = "flag"
    FLAG_INSUFFICIENT_MATERIAL = "flag_insufficient_material"
    ABANDONMENT = "abandonment"
    ADJUDICATION = "adjudication"


class WinnerSide(StrEnum):
    """Mirrors `PlayerSide`. Absent rather than `None` for a draw."""

    LIGHT = "light"
    DARK = "dark"


class MatchOrigin(StrEnum):
    """Where a match came from. **Analytics-only** — the domain has no such
    enum, because a match knows its pairing and does not need a category.

    It exists because "are tournament games completed as often as queue
    games" is a product question and the alternative is joining three
    tables at query time.
    """

    QUEUE = "queue"
    CHALLENGE = "challenge"
    REMATCH = "rematch"
    TOURNAMENT = "tournament"


class QueueType(StrEnum):
    """Mirrors `matchmaking.QueueType` — the pool a ticket joined."""

    RANKED = "ranked"
    CASUAL = "casual"


class QueueExit(StrEnum):
    """Why a queue ticket ended without a pairing — M7b's dimension."""

    CANCELLED = "cancelled"
    EXPIRED = "expired"


class OfferResolution(StrEnum):
    """How a match offer ended.

    Mirrors `game.public.metrics.MatchOutcome`, which is the *acceptance*
    vocabulary — see `Outcome` on why the two are named apart here.
    """

    BOTH_ACCEPTED = "both_accepted"
    DECLINED = "declined"
    EXPIRED = "expired"


class ChallengeResolution(StrEnum):
    """How a friend challenge ended — M17.

    Decline, cancellation and expiry are separate members on purpose:
    merging them into "not accepted" is how a product stops being able to
    tell rejection from indifference.
    """

    ACCEPTED = "accepted"
    DECLINED = "declined"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class CtaPlacement(StrEnum):
    """Where a registration call to action was activated — M2's dimension.

    The five places `pages/landing` and the public chrome actually offer
    one. A sixth would be a new member here and in the client's typed map.
    """

    HERO = "hero"
    HEADER = "header"
    FOOTER = "footer"
    CLOSING = "closing"
    TOURNAMENT = "tournament"


class ShareSurface(StrEnum):
    """What was shared. One member today, and that is the taxonomy's
    answer — `ShareButton` appears on a tournament and nowhere else."""

    TOURNAMENT = "tournament"


class ShareMechanism(StrEnum):
    """Which path the share took, so the two can be compared.

    Never the shared URL, never the clipboard's contents — §38.
    """

    SHARE_SHEET = "share_sheet"
    CLIPBOARD = "clipboard"


class TournamentFormat(StrEnum):
    """Mirrors `tournament.TournamentFormat`.

    All four members, though only single elimination is implemented. The
    mapping test is total in both directions, so a format that ships later
    is a compile-time reminder rather than an unrecognised string in a
    column somebody is grouping by.
    """

    SINGLE_ELIMINATION = "single_elimination"
    DOUBLE_ELIMINATION = "double_elimination"
    SWISS = "swiss"
    ROUND_ROBIN = "round_robin"
    ARENA = "arena"


class TournamentStatus(StrEnum):
    """Mirrors `tournament.TournamentStatus`, for the status a public
    tournament page was viewed in."""

    DRAFT = "draft"
    REGISTRATION_OPEN = "registration_open"
    REGISTRATION_CLOSED = "registration_closed"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
