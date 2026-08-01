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
)

CORPUS_ROOT = Path(__file__).resolve().parents[3] / "specs" / "game-engine" / "corpus"

LATEST_VERSION = 1


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
    """The engine cannot evaluate the position. Temporary — today it means
    a king belonging to the side to move (A64-014.5)."""


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


def load_cases(version: int = LATEST_VERSION) -> tuple[CorpusCase, ...]:
    """Every legal-move case in a corpus version.

    Files are read in sorted name order and cases keep the order they are
    written in, so a parametrised test reports them the same way twice.
    """
    return tuple(_case(entry, name) for entry, name in _entries(version, "cases"))


def load_rejections(version: int = LATEST_VERSION) -> tuple[RejectionCase, ...]:
    """Every rejection case in a corpus version, in the same order."""
    return tuple(_rejection(entry, name) for entry, name in _entries(version, "rejections"))


def _entries(version: int, key: str) -> Iterator[tuple[Mapping[str, Any], str]]:
    """Every entry under `key`, across the version's files.

    A file carries one of the top-level keys and is skipped by the loader
    for the other — which is what lets a third kind of case be an append
    rather than a migration of every reader.
    """
    directory = CORPUS_ROOT / f"v{version}"
    for path in sorted(directory.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        if document["corpus_version"] != version:
            raise ValueError(
                f"{path.name} declares corpus_version {document['corpus_version']} "
                f"but sits in v{version}."
            )
        for entry in document.get(key, ()):
            yield entry, path.name


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


def _position(entry: Mapping[str, Any]) -> Position:
    variant = BoardVariant(entry["variant"])
    board = Board(
        variant,
        {
            BoardCoordinate.parse(piece["square"]): Piece(
                side=PlayerSide(piece["side"]), rank=PieceRank(piece["rank"])
            )
            for piece in entry["pieces"]
        },
    )
    return Position(board=board, side_to_move=PlayerSide(entry["side_to_move"]))


def _move(entry: Mapping[str, Any]) -> Move:
    promotes_to = entry["promotes_to"]
    return Move(
        path=tuple(BoardCoordinate.parse(square) for square in entry["path"]),
        captured=tuple(BoardCoordinate.parse(square) for square in entry["captured"]),
        promotes_to=None if promotes_to is None else PieceRank(promotes_to),
    )
