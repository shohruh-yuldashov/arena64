import { useCallback, useMemo, useRef } from "react";

import {
  type Board,
  BOARD_SIZE,
  isPlayable,
  type Side,
  type Square,
  toCoordinate,
  toSquare,
} from "@/entities/board";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import { cn } from "@/shared/lib/cn";

/**
 * The board — A64-020.5B §13, §14, §26.
 *
 * ## Orientation is rendering, never state
 *
 * The model is always in the engine's frame (`a1` is LIGHT's near-left
 * corner). This component decides only the **order it walks the squares**,
 * so a DARK player sees their own pieces at the bottom without a single
 * coordinate being mirrored. §13 forbids mirroring paths, and nothing here
 * could: the squares it renders carry the server's own names.
 *
 * A future orientation toggle changes one boolean here and nothing else.
 *
 * ## Keyboard, and why the grid is a grid
 *
 * `role="grid"` with `role="gridcell"` buttons: arrow keys move, Enter and
 * Space choose. That is the native pattern for a two-dimensional widget,
 * and it is why every playable square is a real `<button>` rather than a
 * `div` with a handler — a button is focusable, activatable and announced
 * without any of it being re-implemented.
 *
 * **Roving tab index**: exactly one square is in the tab order, so a
 * keyboard user tabs *past* the board rather than through thirty-two
 * squares. Arrow keys move focus within it.
 *
 * ## Nothing is communicated by colour alone
 *
 * Every state a square can be in — selected, a legal destination, part of
 * the last move, holding a piece to be captured — is in its accessible
 * name as words, and marked with a ring or a dot as well as a tint. WCAG
 * 1.4.1, and the reason it matters on a board is that the two piece colours
 * are already the load-bearing distinction.
 */
export interface BoardProps {
  board: Board;
  /** Which way up. The viewer's own side sits at the bottom. */
  orientation: Side;
  /** Squares holding a piece this player may pick up. */
  movable: readonly Square[];
  /** The path chosen so far. `[0]` is the selected piece. */
  selected: readonly Square[];
  /** Squares that may be chosen next. */
  destinations: readonly Square[];
  /** Pieces this selection would take. */
  captured: readonly Square[];
  /** The move just played, for a persistent highlight. */
  lastMove: { path: string[]; captured: string[] } | null;
  interactive: boolean;
  onSelect: (square: Square) => void;
}

export function GameBoard({
  board,
  orientation,
  movable,
  selected,
  destinations,
  captured,
  lastMove,
  interactive,
  onSelect,
}: BoardProps) {
  const { t } = useTranslation();
  const gridRef = useRef<HTMLDivElement>(null);

  // Rank 8 at the top for LIGHT; rank 1 at the top for DARK.
  const ranks = useMemo(() => {
    const ascending = Array.from({ length: BOARD_SIZE }, (_, index) => index);
    return orientation === "light" ? [...ascending].reverse() : ascending;
  }, [orientation]);
  const files = useMemo(() => {
    const ascending = Array.from({ length: BOARD_SIZE }, (_, index) => index);
    return orientation === "light" ? ascending : [...ascending].reverse();
  }, [orientation]);

  // The one square in the tab order — the selection, else the first piece
  // this player can move, else the near-left corner.
  const tabStop = selected[0] ?? movable[0] ?? toSquare({ file: 0, rank: 0 }) ?? "a1";

  const onKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      const deltas: Record<string, { file: number; rank: number }> = {
        ArrowUp: { file: 0, rank: 1 },
        ArrowDown: { file: 0, rank: -1 },
        ArrowLeft: { file: -1, rank: 0 },
        ArrowRight: { file: 1, rank: 0 },
      };
      const delta = deltas[event.key];
      if (delta === undefined) return;

      const active = document.activeElement;
      const from = toCoordinate(active?.getAttribute("data-square") ?? "");
      if (from === null) return;
      event.preventDefault();

      // Flipped for a DARK viewer, so "up" is always up *on screen*. A
      // keyboard user whose arrow keys ran backwards would be the clearest
      // possible case of the model leaking into the view.
      const sign = orientation === "light" ? 1 : -1;
      // Two squares diagonally is one playable square in that direction;
      // stepping one would land on a light square, which holds nothing.
      let next = {
        file: from.file + delta.file * sign,
        rank: from.rank + delta.rank * sign,
      };
      if (!isPlayable(next)) {
        next = { file: next.file + (delta.file === 0 ? 1 : 0), rank: next.rank };
        if (!isPlayable(next)) return;
      }

      const square = toSquare(next);
      if (square === null) return;
      gridRef.current?.querySelector<HTMLButtonElement>(`[data-square="${square}"]`)?.focus();
    },
    [orientation],
  );

  return (
    <div
      ref={gridRef}
      role="grid"
      aria-label={t("game.board.label")}
      onKeyDown={onKeyDown}
      // `aspect-square` and a percentage width: the board is whatever the
      // column gives it and stays square at every size — §27's "board uses
      // available width" without a resize observer.
      className="border-border grid aspect-square w-full grid-cols-8 overflow-hidden rounded-xl border-2 shadow-sm"
    >
      {ranks.map((rank) => (
        <div key={rank} role="row" className="contents">
          {files.map((file) => {
            const square = toSquare({ file, rank });
            if (square === null) return null;
            const playable = isPlayable({ file, rank });
            const piece = board.get(square);

            if (!playable) {
              return (
                <div
                  key={square}
                  role="gridcell"
                  aria-hidden="true"
                  className="bg-muted/40 aspect-square"
                />
              );
            }

            const isSelected = selected[0] === square;
            const inPath = selected.includes(square);
            const isDestination = destinations.includes(square);
            const willBeCaptured = captured.includes(square);
            const inLastMove = lastMove?.path.includes(square) ?? false;
            const canPickUp = interactive && movable.includes(square);

            return (
              <button
                key={square}
                type="button"
                role="gridcell"
                data-square={square}
                tabIndex={square === tabStop ? 0 : -1}
                disabled={!interactive || (!canPickUp && !isDestination && !inPath)}
                aria-pressed={isSelected}
                aria-label={squareLabel(square, piece?.side, piece?.rank, t, {
                  isSelected,
                  isDestination,
                  willBeCaptured,
                  inLastMove,
                })}
                onClick={() => onSelect(square)}
                className={cn(
                  "relative flex aspect-square items-center justify-center transition-colors",
                  "bg-[color-mix(in_oklab,var(--color-foreground)_22%,transparent)]",
                  "focus-visible:ring-ring focus-visible:z-10 focus-visible:ring-2 focus-visible:outline-none",
                  inLastMove && "bg-[color-mix(in_oklab,var(--color-primary)_18%,transparent)]",
                  isSelected && "ring-primary z-10 ring-2 ring-inset",
                  canPickUp && !isSelected && "cursor-pointer",
                  !interactive && "cursor-default",
                )}
              >
                {piece !== undefined && (
                  <span
                    aria-hidden="true"
                    className={cn(
                      "flex size-[72%] items-center justify-center rounded-full border-2 text-[0.6rem] font-bold",
                      piece.side === "light"
                        ? "border-neutral-400 bg-neutral-100 text-neutral-700"
                        : "border-neutral-900 bg-neutral-800 text-neutral-100",
                      willBeCaptured && "opacity-50 ring-2 ring-red-500 ring-offset-1",
                    )}
                  >
                    {/* The king mark is a glyph as well as a ring, so the
                        two ranks differ by shape and not only by colour. */}
                    {piece.rank === "king" ? "♛" : ""}
                  </span>
                )}

                {/* A legal destination is a dot, which is a shape — the
                    tint beside it is decoration. */}
                {isDestination && (
                  <span
                    aria-hidden="true"
                    className="bg-primary absolute size-3 rounded-full opacity-80"
                  />
                )}
              </button>
            );
          })}
        </div>
      ))}
    </div>
  );
}

/**
 * What a screen reader hears on a square.
 *
 * Coordinate first, then what stands there, then what this square *is* in
 * the current interaction. Every one of those is a fact the sighted player
 * gets from colour and position, which is exactly the set §26 requires be
 * available another way.
 */
function squareLabel(
  square: Square,
  side: Side | undefined,
  rank: "man" | "king" | undefined,
  t: (key: TranslationKey, values?: Record<string, string>) => string,
  flags: {
    isSelected: boolean;
    isDestination: boolean;
    willBeCaptured: boolean;
    inLastMove: boolean;
  },
): string {
  const parts: string[] = [square];

  if (side === undefined) {
    parts.push(t("game.board.empty"));
  } else {
    parts.push(
      t(side === "light" ? "game.side.light" : "game.side.dark"),
      t(rank === "king" ? "game.piece.king" : "game.piece.man"),
    );
  }

  if (flags.isSelected) parts.push(t("game.board.selected"));
  if (flags.isDestination) parts.push(t("game.board.destination"));
  if (flags.willBeCaptured) parts.push(t("game.board.willBeCaptured"));
  if (flags.inLastMove) parts.push(t("game.board.lastMove"));

  return parts.join(", ");
}
