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
from collections.abc import Mapping
from dataclasses import dataclass
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


def load_cases(version: int = LATEST_VERSION) -> tuple[CorpusCase, ...]:
    """Every case in a corpus version, in file-then-declaration order.

    Files are read in sorted name order and cases keep the order they are
    written in, so a parametrised test reports them the same way twice.
    """
    directory = CORPUS_ROOT / f"v{version}"
    cases: list[CorpusCase] = []
    for path in sorted(directory.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        if document["corpus_version"] != version:
            raise ValueError(
                f"{path.name} declares corpus_version {document['corpus_version']} "
                f"but sits in v{version}."
            )
        cases.extend(_case(entry, path.name) for entry in document["cases"])
    return tuple(cases)


def _case(entry: Mapping[str, Any], file_name: str) -> CorpusCase:
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
    return CorpusCase(
        id=entry["id"],
        description=entry["description"],
        position=Position(board=board, side_to_move=PlayerSide(entry["side_to_move"])),
        expected_moves=tuple(_move(move) for move in entry["expected_moves"]),
        source=f"{file_name}#{entry['id']}",
    )


def _move(entry: Mapping[str, Any]) -> Move:
    promotes_to = entry["promotes_to"]
    return Move(
        path=tuple(BoardCoordinate.parse(square) for square in entry["path"]),
        captured=tuple(BoardCoordinate.parse(square) for square in entry["captured"]),
        promotes_to=None if promotes_to is None else PieceRank(promotes_to),
    )
