import { useCallback, useMemo, useState } from "react";

import { type Board, boardFrom } from "@/entities/board";
import type { MatchReplay, PlacedPiece as WirePiece } from "@/features/replay/api";
import type { PlacedPiece, Side } from "@/shared/realtime";

/**
 * Where a replay is, and what that position looks like — A64-020.5E §6.
 *
 * ## Two pieces of state and nothing else
 *
 * The index and the orientation. Everything a screen renders is **derived**
 * from those two plus the immutable document TanStack Query holds — there
 * is no copy of the board, no cached position list and no second source of
 * truth. §6 forbids a store and this needs none: the authoritative data is
 * already cached, and what is local is genuinely local.
 *
 * ## The index is a *position*, not a ply
 *
 * `0` is the opening, before anybody moved. `n` is the board after ply `n`.
 * That off-by-one is the whole reason this is a named concept rather than
 * an integer passed around: "ply 3" and "the position after ply 3" are
 * different things, and a move list highlighting one while the board shows
 * the other is the bug this arrangement makes unrepresentable.
 *
 * So `positionCount === plies.length + 1`, and a game nobody moved in has
 * exactly one position — which is why zero plies is a valid replay and not
 * an empty state (§18).
 */
export interface ReplayPosition {
  /** `0` is the opening; `n` is the board after ply `n`. */
  index: number;
  /** The board at `index`, straight from the server. */
  board: Board;
  /** The ply that produced this position, or `null` at the opening. */
  playedPath: string[] | null;
  captured: string[];
  isAtStart: boolean;
  isAtEnd: boolean;
}

export interface ReplayView {
  position: ReplayPosition;
  positionCount: number;
  orientation: Side;
  goTo: (index: number) => void;
  first: () => void;
  previous: () => void;
  next: () => void;
  last: () => void;
  flip: () => void;
}

export function useReplayNavigation(replay: MatchReplay, viewerId: string | null): ReplayView {
  const positionCount = replay.plies.length + 1;

  const [index, setIndex] = useState(0);
  const [flipped, setFlipped] = useState(false);

  // §12: the viewer's own side at the bottom, and light for anybody else —
  // a spectator, a signed-out visitor, or a participant of a different
  // match. Derived from the authoritative seats rather than from a route
  // parameter, so a hand-typed URL cannot choose an orientation that
  // claims a seat.
  const seated: Side =
    viewerId !== null && replay.dark.player_id === viewerId ? "dark" : "light";
  const orientation: Side = flipped ? opposite(seated) : seated;

  const position = useMemo<ReplayPosition>(() => {
    // Clamped rather than trusted. `index` is only ever set by this hook,
    // but a replay whose length changed under a remount would otherwise
    // index past the end — and the clamp costs one comparison.
    const at = Math.min(Math.max(index, 0), positionCount - 1);
    const ply = at === 0 ? null : replay.plies[at - 1];

    return {
      index: at,
      // **The server's board**, never a locally applied move. Every ply
      // carries the position it produced, so there is no engine here and
      // no possibility of the client and the archive disagreeing (§8).
      board: boardFrom(
        placement(ply === undefined || ply === null ? replay.opening : ply.pieces),
      ),
      playedPath: ply?.path ?? null,
      captured: ply?.captured ?? [],
      isAtStart: at === 0,
      isAtEnd: at === positionCount - 1,
    };
  }, [index, positionCount, replay]);

  const goTo = useCallback(
    (next: number) => setIndex(Math.min(Math.max(next, 0), positionCount - 1)),
    [positionCount],
  );

  return {
    position,
    positionCount,
    orientation,
    goTo,
    // Each a no-op at its boundary (§6), by the same clamp — so a control
    // that stayed enabled by mistake still cannot move past the end.
    first: useCallback(() => goTo(0), [goTo]),
    previous: useCallback(() => setIndex((at) => Math.max(at - 1, 0)), []),
    next: useCallback(
      () => setIndex((at) => Math.min(at + 1, positionCount - 1)),
      [positionCount],
    ),
    last: useCallback(() => goTo(positionCount - 1), [goTo, positionCount]),
    flip: useCallback(() => setFlipped((was) => !was), []),
  };
}

function opposite(side: Side): Side {
  return side === "light" ? "dark" : "light";
}

/**
 * The wire's placement list as the board entity's — CLAUDE.md §2.4.
 *
 * The generated schema types `side` and `rank` as `string`, because the
 * OpenAPI document describes them that way; the board entity needs the
 * closed unions. This is the boundary, so this is where the narrowing
 * happens — and a square whose side is neither `light` nor `dark` is
 * **dropped** rather than rendered as something, because a piece of no
 * colour on an archive board would be a silent corruption of a permanent
 * record.
 */
function placement(pieces: readonly WirePiece[]): PlacedPiece[] {
  const placed: PlacedPiece[] = [];
  for (const piece of pieces) {
    if (piece.side !== "light" && piece.side !== "dark") continue;
    if (piece.rank !== "man" && piece.rank !== "king") continue;
    placed.push({ square: piece.square, side: piece.side, rank: piece.rank });
  }
  return placed;
}
