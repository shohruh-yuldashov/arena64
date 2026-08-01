"""Primitive projections of the kernel's value objects.

Framework-free and dependency-free (AD-13). Pure functions from values to
JSON-shaped primitives and back — no I/O, no framework types, nothing that
would make the kernel unmirrorable in TypeScript (AD-14).

## One encoding, not two

These are **the same shapes the conformance corpus is written in**. A
square is `"c3"`, a piece is `{"square", "side", "rank"}`, a move is
`{"path", "captured", "promotes_to"}`. That is deliberate: a corpus in one
encoding and a stored game in another would be two definitions of what a
move is, and they would diverge on exactly the multi-jump the path
notation exists to disambiguate. `tests/corpus.py` reads through these
functions for that reason.

## Round-trip, and no hidden defaults

Every `*_from_primitive` requires every field. There is no "if absent,
assume". A record written by an older build that lacked a field must fail
loudly rather than be silently reinterpreted — a game whose promotion flag
quietly defaulted to `null` would replay into a different position, which
is the whole failure AD-15 exists to make visible.

## Why this is in `engine` rather than in `game`

`replay` and `fairplay` may import `engine` and not `game` (R-2). Both
read stored games, so the projection of a position has to be reachable
from the kernel. The `game` aggregate's own shapes — move records, results,
replay payloads — are serialized in `game.domain.serialization`, which is
where they belong.
"""

from collections.abc import Mapping
from typing import Any

from app.modules.engine.board import Board
from app.modules.engine.coordinate import BoardCoordinate
from app.modules.engine.exceptions import InvalidBoardState
from app.modules.engine.move import Move
from app.modules.engine.piece import Piece, PieceRank, PlayerSide
from app.modules.engine.position import Position
from app.modules.engine.variant import BoardVariant
from app.modules.engine.version import EngineVersion

Primitive = Mapping[str, Any]


def engine_version_to_primitive(version: EngineVersion) -> int:
    """The integer a stored match records — `EngineVersion.as_primitive`.

    Named here so a caller serializing a whole payload does not reach past
    this module for one field, but it is the existing method and not a
    second encoding.
    """
    return version.as_primitive()


def engine_version_from_primitive(value: object) -> EngineVersion:
    """An engine version from an explicit integer.

    **Never inferred.** Not from a timestamp, a schema version, a file
    format, a creation date, or the version this build happens to be. A
    replay whose version was guessed is a replay under rules the game was
    not played under, which is precisely what AD-15 says must be
    impossible — so an absent or non-integer value is an error rather than
    a default.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidBoardState(f"An engine version is an integer, not {value!r}.")
    return EngineVersion(number=value)


def coordinate_to_primitive(coordinate: BoardCoordinate) -> str:
    """Algebraic notation — `"a1"` at the near-left corner."""
    return str(coordinate)


def coordinate_from_primitive(value: str) -> BoardCoordinate:
    return BoardCoordinate.parse(value)


def piece_to_primitive(square: BoardCoordinate, piece: Piece) -> dict[str, str]:
    return {
        "square": coordinate_to_primitive(square),
        "side": piece.side.value,
        "rank": piece.rank.value,
    }


def piece_from_primitive(entry: Primitive) -> tuple[BoardCoordinate, Piece]:
    return (
        coordinate_from_primitive(entry["square"]),
        Piece(side=PlayerSide(entry["side"]), rank=PieceRank(entry["rank"])),
    )


def board_to_primitive(board: Board) -> dict[str, Any]:
    """A board as its variant and its occupied squares, **sorted**.

    Sorted because two identical boards built by different move orders
    must serialize to identical text — the same reason
    `Position.fingerprint` sorts. A reader comparing two stored games byte
    for byte is a reader this makes possible.
    """
    return {
        "variant": board.variant.value,
        "pieces": [
            piece_to_primitive(square, piece)
            for square, piece in sorted(board.occupied_squares.items(), key=lambda e: e[0])
        ],
    }


def board_from_primitive(entry: Primitive) -> Board:
    return Board(
        BoardVariant(entry["variant"]),
        dict(piece_from_primitive(piece) for piece in entry["pieces"]),
    )


def position_to_primitive(position: Position) -> dict[str, Any]:
    """A position as a structured record, not as its fingerprint.

    The fingerprint encodes the same facts and is what gets *hashed*, but
    parsing it back would be a second parser for one encoding — and the
    one place a typo in it would surface is a replay that reconstructed a
    different board. Structure in, structure out; the fingerprint stays a
    comparison key.
    """
    board = board_to_primitive(position.board)
    return {
        "variant": board["variant"],
        "side_to_move": position.side_to_move.value,
        "pieces": board["pieces"],
    }


def position_from_primitive(entry: Primitive) -> Position:
    return Position(
        board=board_from_primitive(entry),
        side_to_move=PlayerSide(entry["side_to_move"]),
    )


def move_to_primitive(move: Move) -> dict[str, Any]:
    """A move as its **complete path**, never as an origin and a
    destination.

    domain-model.md §2.1: "a multi-jump in draughts can reach the same
    destination square by different capture paths, capturing different
    pieces." A from/to record is an ambiguous description of several moves,
    and the ambiguity lands on the piece the paths disagree about — so a
    move log written that way cannot be replayed, which invalidates the
    result, the analysis and the fair-play record at once.
    """
    return {
        "path": [coordinate_to_primitive(square) for square in move.path],
        "captured": [coordinate_to_primitive(square) for square in move.captured],
        "promotes_to": None if move.promotes_to is None else move.promotes_to.value,
    }


def move_from_primitive(entry: Primitive) -> Move:
    promotes_to = entry["promotes_to"]
    return Move(
        path=tuple(coordinate_from_primitive(square) for square in entry["path"]),
        captured=tuple(coordinate_from_primitive(square) for square in entry["captured"]),
        promotes_to=None if promotes_to is None else PieceRank(promotes_to),
    )


__all__ = [
    "board_from_primitive",
    "board_to_primitive",
    "coordinate_from_primitive",
    "coordinate_to_primitive",
    "engine_version_from_primitive",
    "engine_version_to_primitive",
    "move_from_primitive",
    "move_to_primitive",
    "piece_from_primitive",
    "piece_to_primitive",
    "position_from_primitive",
    "position_to_primitive",
]
