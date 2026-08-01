"""Loader for the Game Engine conformance corpus.

The corpus itself lives in `specs/game-engine/corpus/`, outside both
implementations on purpose (AD-14: "the corpus is the contract"). This
module is the Python side's reader — the part a future TypeScript engine
writes for itself.

**It is here rather than in `app.modules.engine` because reading a file is
I/O**, and AD-13 gives the engine none. A loader inside the kernel would be
the first crack in the guarantee that makes the kernel worth having, and
`.importlinter`'s `engine-is-a-dependency-free-kernel` contract would fail
on the import.
"""

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from app.modules.engine import (
    Board,
    BoardCoordinate,
    BoardVariant,
    Move,
    Piece,
    PieceRank,
    PlayerSide,
    Position,
    TerminalReason,
)

CORPUS_ROOT = Path(__file__).resolve().parents[3] / "specs" / "game-engine" / "corpus"

LATEST_VERSION = 2


@dataclass(frozen=True, slots=True)
class CorpusCase:
    """One position and the complete move list it must produce."""

    id: str
    description: str
    position: Position
    expected_moves: tuple[Move, ...]
    source: str
    """`<file>#<id>`, so a failure names the case a reader has to open."""

    def __str__(self) -> str:
        return self.source


class RejectionCategory(StrEnum):
    """Where a refused move is refused, and by what.

    Part of the expectation rather than a hint: the three are raised at
    different moments by different code for different reasons, and a case
    that mixed them up would satisfy any reader that only checked
    "something was refused".
    """

    ILLEGAL_MOVE = "illegal_move"
    """Well-formed, and not among the moves the position offers."""

    MALFORMED_MOVE = "malformed_move"
    """The move's shape is broken; it cannot be constructed at all."""

    UNSUPPORTED_PIECE = "unsupported_piece"
    """**Historical.** The engine could not evaluate the position at all.

    v1 used it for a king belonging to the side to move, which A64-014.3
    refused because kings did not move yet. A64-014.5 implements them, so
    v2 supersedes that case and no active case carries this category — but
    the member stays, because v1's files are kept verbatim and a reader
    that could not parse them would make the history unreadable, which is
    the whole point of keeping it.
    """


@dataclass(frozen=True, slots=True)
class RejectionCase:
    """One position, one move, and the refusal it must produce."""

    id: str
    description: str
    position: Position
    rejection: RejectionCategory
    source: str

    raw_move: Mapping[str, Any]
    """The move as written in the corpus, **unbuilt**.

    A `Move` and not raw data would be the obvious shape, and it cannot be:
    a `malformed_move` case is precisely one that fails to construct, so
    building it in the loader would raise while *reading the corpus* rather
    than while running the case. `build_move` is where construction
    happens, and for that category raising is the expectation.
    """

    def build_move(self) -> Move:
        """The case's move, or `InvalidMove` for a malformed one."""
        return _move(self.raw_move)


@dataclass(frozen=True, slots=True)
class DrawSequenceCase:
    """An opening position, an ordered list of moves, and what the match
    looks like once they have all been played.

    The **fourth** expectation shape. A draw is a property of a game
    rather than of a board, so it cannot be stated as a position the way
    `terminal_positions` states a loss — this is the first case kind that
    describes a sequence.
    """

    id: str
    description: str
    engine_version: int
    """Which rules build the expectation was written for. Draws arrived in
    2, so a reader on 1 would disagree about the last ply of half of
    these."""

    variant: BoardVariant
    side_to_move: PlayerSide
    pieces: Mapping[BoardCoordinate, Piece]
    moves: tuple[Move, ...]
    expected_status: str
    expected_outcome: str | None
    expected_reason: str | None
    expected_winner: PlayerSide | None
    expected_position_occurrences: int
    expected_plies_since_progress: int
    source: str

    def __str__(self) -> str:
        return self.source


@dataclass(frozen=True, slots=True)
class TerminalCase:
    """One position and the terminal verdict it must produce.

    `winner` and `reason` are `None` together, for a position that is not
    terminal — the same shape `TerminalStateEvaluator.evaluate` answers
    with, so a reader compares one value rather than three flags.
    """

    id: str
    description: str
    position: Position
    terminal: bool
    winner: PlayerSide | None
    reason: TerminalReason | None
    source: str

    def __str__(self) -> str:
        return self.source


def load_cases(through: int = LATEST_VERSION) -> tuple[CorpusCase, ...]:
    """Every legal-move case still in force, v1 through `through`.

    Files are read version by version and then in sorted name order, and
    cases keep the order they are written in, so a parametrised test
    reports them the same way twice.
    """
    return tuple(_case(entry, name) for entry, name in _entries(through, "cases"))


def load_rejections(through: int = LATEST_VERSION) -> tuple[RejectionCase, ...]:
    """Every rejection case still in force, in the same order."""
    return tuple(_rejection(entry, name) for entry, name in _entries(through, "rejections"))


def load_terminal_positions(through: int = LATEST_VERSION) -> tuple[TerminalCase, ...]:
    """Every terminal-state case still in force.

    A third top-level key beside `cases` and `rejections`, added by v2:
    "these are the legal moves" and "this position has ended" are
    different claims, and bending one shape into the other would make a
    reader guess which it was looking at.
    """
    return tuple(_terminal(entry, name) for entry, name in _entries(through, "terminal_positions"))


def load_draw_sequences(through: int = LATEST_VERSION) -> tuple[DrawSequenceCase, ...]:
    """Every draw-sequence case still in force."""
    return tuple(_draw_sequence(entry, name) for entry, name in _entries(through, "draw_sequences"))


def superseded_ids(through: int = LATEST_VERSION) -> frozenset[str]:
    """The case ids a later version has retired."""
    return frozenset(entry["id"] for entry, _ in _supersessions(through))


def _entries(through: int, key: str) -> Iterator[tuple[Mapping[str, Any], str]]:
    """Every entry under `key` that has not been superseded.

    A file carries one of the top-level case keys and is skipped by the
    loader for the other — which is what lets a new kind of case be an
    append rather than a migration of every reader.

    **Supersession is data, not prose.** A version that changes a rule
    lists the ids it retires in its own files, and this filters them out
    of the earlier versions rather than editing them. That is what keeps
    the append-only promise honest: v1 on disk is byte-for-byte what it
    was, and the reason a case stopped applying is recorded beside the
    rule that replaced it.
    """
    retired = superseded_ids(through)
    for document, name in _documents(through):
        for entry in document.get(key, ()):
            if entry["id"] not in retired:
                yield entry, name


def _supersessions(through: int) -> Iterator[tuple[Mapping[str, Any], str]]:
    for document, name in _documents(through):
        for entry in document.get("supersedes", ()):
            yield entry, name


def _documents(through: int) -> Iterator[tuple[Mapping[str, Any], str]]:
    for version in range(1, through + 1):
        directory = CORPUS_ROOT / f"v{version}"
        for path in sorted(directory.glob("*.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            if document["corpus_version"] != version:
                raise ValueError(
                    f"{path.name} declares corpus_version {document['corpus_version']} "
                    f"but sits in v{version}."
                )
            yield document, f"v{version}/{path.name}"


def _case(entry: Mapping[str, Any], file_name: str) -> CorpusCase:
    return CorpusCase(
        id=entry["id"],
        description=entry["description"],
        position=_position(entry),
        expected_moves=tuple(_move(move) for move in entry["expected_moves"]),
        source=f"{file_name}#{entry['id']}",
    )


def _rejection(entry: Mapping[str, Any], file_name: str) -> RejectionCase:
    return RejectionCase(
        id=entry["id"],
        description=entry["description"],
        position=_position(entry),
        rejection=RejectionCategory(entry["rejection"]),
        source=f"{file_name}#{entry['id']}",
        raw_move=entry["move"],
    )


def _terminal(entry: Mapping[str, Any], file_name: str) -> TerminalCase:
    winner, reason = entry["winner"], entry["reason"]
    return TerminalCase(
        id=entry["id"],
        description=entry["description"],
        position=_position(entry),
        terminal=entry["terminal"],
        winner=None if winner is None else PlayerSide(winner),
        reason=None if reason is None else TerminalReason(reason),
        source=f"{file_name}#{entry['id']}",
    )


def _draw_sequence(entry: Mapping[str, Any], file_name: str) -> DrawSequenceCase:
    winner = entry["expected_winner"]
    return DrawSequenceCase(
        id=entry["id"],
        description=entry["description"],
        engine_version=entry["engine_version"],
        variant=BoardVariant(entry["variant"]),
        side_to_move=PlayerSide(entry["side_to_move"]),
        pieces=_pieces(entry),
        moves=tuple(_move(played) for played in entry["moves"]),
        expected_status=entry["expected_status"],
        expected_outcome=entry["expected_outcome"],
        expected_reason=entry["expected_reason"],
        expected_winner=None if winner is None else PlayerSide(winner),
        expected_position_occurrences=entry["expected_position_occurrences"],
        expected_plies_since_progress=entry["expected_plies_since_progress"],
        source=f"{file_name}#{entry['id']}",
    )


def _pieces(entry: Mapping[str, Any]) -> Mapping[BoardCoordinate, Piece]:
    return {
        BoardCoordinate.parse(piece["square"]): Piece(
            side=PlayerSide(piece["side"]), rank=PieceRank(piece["rank"])
        )
        for piece in entry["pieces"]
    }


def _position(entry: Mapping[str, Any]) -> Position:
    board = Board(BoardVariant(entry["variant"]), _pieces(entry))
    return Position(board=board, side_to_move=PlayerSide(entry["side_to_move"]))


def _move(entry: Mapping[str, Any]) -> Move:
    promotes_to = entry["promotes_to"]
    return Move(
        path=tuple(BoardCoordinate.parse(square) for square in entry["path"]),
        captured=tuple(BoardCoordinate.parse(square) for square in entry["captured"]),
        promotes_to=None if promotes_to is None else PieceRank(promotes_to),
    )
