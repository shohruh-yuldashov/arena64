"""`tournament`'s domain errors — SPEC-TOURNAMENT.

Typed rather than a message, so a caller branches on a fact instead of on
string matching (CLAUDE.md §9.5). Every one is a `DomainError`, because
each describes a request that is *wrong* rather than an infrastructure
failure — the platform's handler maps the taxonomy to status codes and no
framework exception is raised here.
"""

from app.core.exceptions import DomainError


class UnsupportedTournamentFormat(DomainError):
    """A format this release does not run — SPEC-TOURNAMENT §2, T-1.

    Only `SINGLE_ELIMINATION` ships in v0.x. The others are deferred rather
    than impossible, and each is a pairing strategy plus a bracket model
    away — so this refuses at construction rather than letting a tournament
    exist that nothing can pair.
    """


class InvalidCapacity(DomainError):
    """A field size outside 2…128 — T-2.

    Two, because one player is not a tournament. A hundred and twenty-eight,
    because that is the largest field this release commits to and a bracket
    is a power of two: the cap is a product decision, not an arithmetic
    limit.
    """


class InvalidTournamentTransition(DomainError):
    """A lifecycle move the state machine does not allow.

    Named rather than a boolean return, because every caller's response is
    the same — refuse — and a silent no-op would let an operator believe a
    tournament started when it did not.
    """


class InvalidRoundNumber(DomainError):
    """Rounds are numbered from 1 and are contiguous.

    A gap makes "which round is this" unanswerable from the record, which
    is the same argument MT-5 makes about the move log.
    """


class PublishedRoundIsImmutable(DomainError):
    """A published round's pairings do not change — SPEC-TOURNAMENT §6.

    Once a round is published, players have read it. Changing it afterwards
    would mean the bracket somebody planned against is not the bracket their
    result is recorded in.
    """


class InvalidBracketPosition(DomainError):
    """A node outside its depth's range.

    Depth `d` of a single-elimination bracket holds exactly `2**d` nodes, so
    a position outside that is a bracket that cannot be walked — detected at
    construction rather than when advancement runs into it.
    """


__all__ = [
    "InvalidBracketPosition",
    "InvalidCapacity",
    "InvalidRoundNumber",
    "InvalidTournamentTransition",
    "PublishedRoundIsImmutable",
    "UnsupportedTournamentFormat",
]
