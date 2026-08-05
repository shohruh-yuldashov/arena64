import type { PlacedPiece, Rank, Side } from "@/shared/realtime";

/**
 * The board, as this client holds it — A64-020.5B §13.
 *
 * ## Coordinates are the server's, always
 *
 * A square is the algebraic string the gateway sends: `"c3"`. Nothing here
 * invents a numbering, and nothing mirrors a path — §13 forbids both, and
 * the reason is that a move is submitted as a list of these strings. A
 * client that renumbered internally would have to renumber back at exactly
 * one place, and the day it forgot, the server would receive a legal move
 * for a different set of squares.
 *
 * Orientation is a **rendering** concern and lives in the board component.
 * The model is always in the engine's frame: `a1` is LIGHT's near-left
 * corner, `row` increases away from LIGHT
 * (`app/modules/engine/coordinate.py`).
 *
 * ## Playable squares
 *
 * `(file + rank)` even. `a1 = (0,0)`, `b2 = (1,1)`, `c3 = (2,2)` — every
 * square the initial position occupies. Derived rather than listed, and
 * asserted against the corpus's opening position.
 */
export const BOARD_SIZE = 8;

/** A square, in the engine's algebraic notation. `"a1"` … `"h8"`. */
export type Square = string;

/** Zero-based, in the engine's frame. `a1` is `{ file: 0, rank: 0 }`. */
export interface Coordinate {
  file: number;
  rank: number;
}

const FILES = "abcdefgh";

/** `"c3"` → `{ file: 2, rank: 2 }`, or `null` if it is not a square. */
export function toCoordinate(square: Square): Coordinate | null {
  if (square.length !== 2) return null;
  const file = FILES.indexOf(square[0] ?? "");
  const rank = Number(square[1]) - 1;
  if (file < 0 || !Number.isInteger(rank) || rank < 0 || rank >= BOARD_SIZE) return null;
  return { file, rank };
}

/** `{ file: 2, rank: 2 }` → `"c3"`, or `null` if it is off the board. */
export function toSquare(coordinate: Coordinate): Square | null {
  const { file, rank } = coordinate;
  if (file < 0 || file >= BOARD_SIZE || rank < 0 || rank >= BOARD_SIZE) return null;
  return `${FILES[file]}${rank + 1}`;
}

/** Whether a piece may ever stand here. Dark squares only. */
export function isPlayable(coordinate: Coordinate): boolean {
  return (coordinate.file + coordinate.rank) % 2 === 0;
}

/** Every square, in a stable order: rank 1 first, file a first. */
export function allSquares(): Square[] {
  const squares: Square[] = [];
  for (let rank = 0; rank < BOARD_SIZE; rank += 1) {
    for (let file = 0; file < BOARD_SIZE; file += 1) {
      const square = toSquare({ file, rank });
      if (square !== null) squares.push(square);
    }
  }
  return squares;
}

/**
 * A position: which piece stands on which square.
 *
 * A `Map` keyed by square rather than an array, because the wire sends a
 * sparse list of occupied squares and every question this client asks is
 * "what is on `c3`". Empty squares are absent, not `null` — a distinction
 * that costs nothing and makes `has` the occupancy test.
 */
export type Board = ReadonlyMap<Square, PlacedPiece>;

export function boardFrom(pieces: readonly PlacedPiece[]): Board {
  return new Map(pieces.map((piece) => [piece.square, piece]));
}

/** The row a man of `side` promotes on. LIGHT crowns far, DARK crowns near. */
export function promotionRank(side: Side): number {
  return side === "light" ? BOARD_SIZE - 1 : 0;
}

/** The rank a man of `side` moves towards when it is not capturing. */
export function forwardStep(side: Side): number {
  return side === "light" ? 1 : -1;
}

export function opponentOf(side: Side): Side {
  return side === "light" ? "dark" : "light";
}

export type { PlacedPiece, Rank, Side };
