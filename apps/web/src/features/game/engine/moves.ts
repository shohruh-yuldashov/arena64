import {
  type Board,
  type Coordinate,
  forwardStep,
  isPlayable,
  opponentOf,
  type PlacedPiece,
  promotionRank,
  type Side,
  type Square,
  toCoordinate,
  toSquare,
} from "@/entities/board";

/**
 * The client rules kernel — A64-020.5B §12.
 *
 * ## What this is for, and what it is emphatically not
 *
 * Highlighting. A player needs to see which squares a selected piece may
 * reach *before* the server has been asked, and a round trip per hover is
 * not a UI. That is the entire mandate: selection, legal destinations,
 * mandatory capture, and building a complete multi-capture path.
 *
 * **The server remains authoritative** (§11). Nothing here decides whether
 * a move was legal — `game.move.rejected` does. If this kernel and the
 * engine ever disagree, the engine is right and the client resyncs; the
 * cost of a disagreement is a rejected move and a confused half-second,
 * not a corrupt game.
 *
 * ## The rules are not written from memory
 *
 * §12 forbids that, and this file obeys it: every rule below is the one
 * `specs/game-engine/corpus/` asserts, and `game.test.tsx` runs this
 * against the corpus files themselves. Where a rule was not obvious from
 * the cases it is not implemented — there is no draw detection, no terminal
 * evaluation and no move ordering guarantee, because none of those is
 * needed to highlight a square and all three would be a second opinion the
 * server never asked for.
 *
 * The specific Russian rules the corpus pins, each traceable to a case:
 *
 *   - a man steps one square diagonally **forward** when not capturing
 *   - a man captures in **any** direction, jumping an adjacent enemy to the
 *     empty square immediately beyond
 *   - a king slides any distance along a diagonal (`king-quiet-moves-along-
 *     open-diagonals` — eleven from c3 on an empty board)
 *   - a king captures from a distance and may land on **any** empty square
 *     beyond the victim, each landing being a distinct move
 *     (`a-flying-king-capture-offers-every-square-beyond-the-victim`)
 *   - capture is **mandatory** and binds the *player*, not the piece
 *     (`a-king-capture-suppresses-every-quiet-move`)
 *   - only **complete** sequences are moves; a prefix is never offered
 *     (`incomplete-prefix-is-not-offered`)
 *   - a captured piece stays on the board for the rest of the sequence and
 *     may not be taken twice — the Turkish strike
 *     (`a-taken-piece-blocks-and-is-never-taken-again`, whose path returns
 *     to its own origin)
 *   - Russian rules impose **no maximum-capture filter**: a one-piece
 *     sequence stands beside a two-piece one
 *     (`two-alternative-complete-sequences`)
 *   - a man that lands on the crownhead mid-sequence is crowned **at once**
 *     and continues as a flying king
 *     (`russian-man-crowns-mid-sequence-and-continues`)
 *
 * Only `russian_8x8` is implemented, which is the only `ProductVariant` the
 * platform offers. The corpus's international and English cases are
 * filtered out by the test rather than half-supported here.
 */

/** A complete, submittable move. `path` is what goes on the wire. */
export interface CandidateMove {
  /** Every square the piece occupies, in order. Two or more. */
  path: Square[];
  /** The squares whose pieces this move takes. Server re-derives them. */
  captured: Square[];
  /** The rank the piece ends as, when the move crowns it. */
  promotesTo: "king" | null;
}

const DIAGONALS: readonly Coordinate[] = [
  { file: 1, rank: 1 },
  { file: 1, rank: -1 },
  { file: -1, rank: 1 },
  { file: -1, rank: -1 },
];

/**
 * Every complete move `side` may play.
 *
 * Captures suppress quiet moves entirely when any capture exists anywhere —
 * mandatory capture binds the player, not the piece.
 */
export function legalMoves(board: Board, side: Side): CandidateMove[] {
  const captures: CandidateMove[] = [];
  for (const piece of board.values()) {
    if (piece.side !== side) continue;
    captures.push(...capturesFrom(board, piece));
  }
  if (captures.length > 0) return captures;

  const quiet: CandidateMove[] = [];
  for (const piece of board.values()) {
    if (piece.side !== side) continue;
    quiet.push(...quietFrom(board, piece));
  }
  return quiet;
}

/** The moves that begin on one square. Used to light up a selection. */
export function movesFrom(board: Board, side: Side, from: Square): CandidateMove[] {
  return legalMoves(board, side).filter((move) => move.path[0] === from);
}

// --- quiet moves ---------------------------------------------------------

function quietFrom(board: Board, piece: PlacedPiece): CandidateMove[] {
  const origin = toCoordinate(piece.square);
  if (origin === null) return [];
  const moves: CandidateMove[] = [];

  for (const step of DIAGONALS) {
    // A man may only step towards its crownhead. A king may go anywhere.
    if (piece.rank === "man" && step.rank !== forwardStep(piece.side)) continue;

    // A man takes one step; a king slides until something stops it.
    const reach = piece.rank === "man" ? 1 : Infinity;
    for (let distance = 1; distance <= reach; distance += 1) {
      const square = squareAt(origin, step, distance);
      if (square === null || board.has(square)) break;
      moves.push({
        path: [piece.square, square],
        captured: [],
        promotesTo: crowns(piece, square) ? "king" : null,
      });
    }
  }
  return moves;
}

// --- captures ------------------------------------------------------------

/**
 * Every **complete** capture sequence starting from one piece.
 *
 * Depth-first, and a branch is only emitted when it can go no further —
 * which is what makes a prefix impossible to offer. Taken pieces are
 * remembered rather than removed, because the Turkish strike says a
 * captured piece keeps blocking the board until the ply ends.
 */
function capturesFrom(board: Board, piece: PlacedPiece): CandidateMove[] {
  const complete: CandidateMove[] = [];

  const walk = (
    at: Square,
    rank: PlacedPiece["rank"],
    path: Square[],
    taken: Square[],
  ): void => {
    const continuations = captureStepsFrom(board, at, piece.side, rank, taken, piece.square);

    if (continuations.length === 0) {
      // Nothing further: this branch is a complete move. A single square
      // means no capture was ever found, which is not a move at all.
      if (path.length > 1) {
        complete.push({
          path: [...path],
          captured: [...taken],
          // Crowning is decided by where the piece *ends*, and separately
          // by whether it crowned mid-sequence — both produce a king, and
          // `rank` already carries the latter.
          promotesTo: rank === "king" && piece.rank === "man" ? "king" : null,
        });
      }
      return;
    }

    for (const step of continuations) {
      // Russian: crowning happens the moment the piece lands on the
      // crownhead, and it continues the sequence as a flying king.
      const crowned = rank === "man" && landsOnCrownhead(piece.side, step.landing);
      walk(
        step.landing,
        crowned ? "king" : rank,
        [...path, step.landing],
        [...taken, step.victim],
      );
    }
  };

  walk(piece.square, piece.rank, [piece.square], []);
  return complete;
}

interface CaptureStep {
  victim: Square;
  landing: Square;
}

/**
 * One jump from `at`, in every direction it is available.
 *
 * `origin` is the square the sequence began on: it is empty for the rest of
 * the ply — the piece left it — so a king sliding back over it must not be
 * blocked. `a-taken-piece-blocks-and-is-never-taken-again` ends by landing
 * there.
 */
function captureStepsFrom(
  board: Board,
  at: Square,
  side: Side,
  rank: PlacedPiece["rank"],
  taken: readonly Square[],
  origin: Square,
): CaptureStep[] {
  const from = toCoordinate(at);
  if (from === null) return [];

  const occupied = (square: Square): PlacedPiece | undefined =>
    square === origin ? undefined : board.get(square);

  const steps: CaptureStep[] = [];
  const enemy = opponentOf(side);
  // A man jumps an adjacent piece; a king may approach from any distance.
  const reach = rank === "man" ? 1 : Infinity;

  for (const direction of DIAGONALS) {
    let victim: Square | null = null;

    for (let distance = 1; distance <= reach + 1; distance += 1) {
      const square = squareAt(from, direction, distance);
      if (square === null) break;
      const piece = occupied(square);

      if (victim === null) {
        if (piece === undefined) {
          // Empty: a king keeps looking down the diagonal, a man has used
          // its one square of reach.
          if (rank === "man") break;
          continue;
        }
        // A friendly piece, or one already taken, ends this diagonal: both
        // block, and an already-taken piece may not be taken again.
        if (piece.side !== enemy || taken.includes(square)) break;
        victim = square;
        continue;
      }

      // Past the victim: every empty square is a landing, and the first
      // occupied one ends the run.
      if (piece !== undefined) break;
      steps.push({ victim, landing: square });
      if (rank === "man") break;
    }
  }
  return steps;
}

// --- helpers -------------------------------------------------------------

function squareAt(from: Coordinate, step: Coordinate, distance: number): Square | null {
  const target = {
    file: from.file + step.file * distance,
    rank: from.rank + step.rank * distance,
  };
  if (!isPlayable(target)) return null;
  return toSquare(target);
}

function landsOnCrownhead(side: Side, square: Square): boolean {
  const coordinate = toCoordinate(square);
  return coordinate !== null && coordinate.rank === promotionRank(side);
}

function crowns(piece: PlacedPiece, destination: Square): boolean {
  return piece.rank === "man" && landsOnCrownhead(piece.side, destination);
}
