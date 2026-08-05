import { useCallback, useMemo, useState } from "react";

import type { Square } from "@/entities/board";
import type { CandidateMove } from "@/features/game/engine/moves";
import { legalMoves } from "@/features/game/engine/moves";
import { canInteract, type GameState } from "@/features/game/model/state";

/**
 * Picking a piece and building a path — A64-020.5B §15.
 *
 * ## A selection is a prefix, not a from/to pair
 *
 * §15 forbids reducing a move to origin and destination, and draughts is
 * why: the same two squares can be reached by two capture sequences taking
 * different pieces, so the wire carries the whole path and so does this.
 * The selection state is therefore the path *so far*, and the available
 * destinations are the squares that extend it.
 *
 * ## Multi-capture continues by narrowing, not by asking
 *
 * After each step the candidate list is filtered to the moves whose path
 * still begins with what has been chosen. When exactly one remains and it
 * is fully consumed, the move is complete. When several remain — a king
 * that may stop on three squares beyond its victim — the player chooses
 * again. Nothing here decides for them, and nothing submits a prefix: the
 * kernel only ever produced complete sequences.
 *
 * ## Mandatory capture needs no code here
 *
 * `legalMoves` already suppresses quiet moves when a capture exists, so a
 * player whose only legal moves are captures simply finds that their other
 * pieces offer nothing. The rule is enforced once, in the kernel, against
 * the corpus.
 */
export interface MoveSelection {
  /** The squares chosen so far. Empty when nothing is selected. */
  path: Square[];
  /** Squares that may be chosen next. */
  destinations: Square[];
  /** Squares holding a piece this player may pick up. */
  movable: Square[];
  /** The pieces this selection would take, for highlighting. */
  captured: Square[];
  /** Whether choosing a destination would complete a move. */
  select: (square: Square) => Square[] | null;
  clear: () => void;
}

export function useMoveSelection(state: GameState): MoveSelection {
  const [path, setPath] = useState<Square[]>([]);

  const candidates = useMemo(
    () =>
      canInteract(state) && state.side !== null ? legalMoves(state.board, state.side) : [],
    [state],
  );

  // Only the moves this selection could still become.
  const matching = useMemo(
    () => candidates.filter((move) => startsWith(move.path, path)),
    [candidates, path],
  );

  const movable = useMemo(
    () => unique(candidates.map((move) => move.path[0]).filter(isSquare)),
    [candidates],
  );

  const destinations = useMemo(() => {
    if (path.length === 0) return [];
    return unique(matching.map((move) => move.path[path.length]).filter(isSquare));
  }, [matching, path]);

  const captured = useMemo(() => {
    if (path.length < 2) return [];
    // What every remaining candidate agrees has been taken so far. Shown
    // rather than the union, because a square only one branch takes is not
    // yet a fact about this move.
    return matching.length === 0 ? [] : capturedSoFar(matching, path);
  }, [matching, path]);

  const clear = useCallback(() => setPath([]), []);

  /**
   * Chooses a square. Returns the completed path when the move is finished,
   * and `null` while it is still being built or the square was not legal.
   *
   * The caller submits what it is handed; this never submits, so a
   * component that forgets to is a component that does nothing rather than
   * one that silently plays a move.
   */
  const select = useCallback(
    (square: Square): Square[] | null => {
      if (!canInteract(state)) return null;

      // Nothing selected: pick a piece that has moves.
      if (path.length === 0) {
        if (!movable.includes(square)) return null;
        setPath([square]);
        return null;
      }

      // Re-clicking the selected piece clears; clicking another movable
      // piece switches to it. Both are what a player expects and neither is
      // a move.
      if (square === path[0] && path.length === 1) {
        setPath([]);
        return null;
      }
      if (path.length === 1 && movable.includes(square)) {
        setPath([square]);
        return null;
      }

      if (!destinations.includes(square)) return null;

      const next = [...path, square];
      const complete = matching.find(
        (move) => move.path.length === next.length && startsWith(move.path, next),
      );
      if (complete !== undefined) {
        setPath([]);
        return complete.path;
      }

      // A longer sequence continues from here.
      setPath(next);
      return null;
    },
    [state, path, movable, destinations, matching],
  );

  return { path, destinations, movable, captured, select, clear };
}

function startsWith(candidate: readonly Square[], prefix: readonly Square[]): boolean {
  return prefix.every((square, index) => candidate[index] === square);
}

/**
 * The captures every remaining branch has already made.
 *
 * One jump per step taken, and the branches agree on those by construction
 * — they share the prefix. Taking the first candidate's captures up to the
 * number of steps chosen is therefore exact rather than an approximation.
 */
function capturedSoFar(matching: readonly CandidateMove[], path: readonly Square[]): Square[] {
  const first = matching[0];
  if (first === undefined) return [];
  return first.captured.slice(0, path.length - 1);
}

function unique(values: Square[]): Square[] {
  return [...new Set(values)];
}

function isSquare(value: Square | undefined): value is Square {
  return value !== undefined;
}
