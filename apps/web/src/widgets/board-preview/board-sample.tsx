import { PIECE_FINISH_CLASS } from "@/entities/board";
import { cn } from "@/shared/lib/cn";

/**
 * A four-by-four board, drawn the way the real one is — A64-025.5B §22.
 *
 * `BoardMotif` is an SVG and stays where it is: it is decoration on the
 * home page and its pieces are circles. This is a **preview**, which is a
 * different job — it has to be true. A piece set changes the disc's radius,
 * its rim and its relief as well as its colour, and none of those survive a
 * `<circle>`; so the squares and the men here are the same elements with
 * the same classes the game room uses, and they read the same tokens.
 *
 * That is also what stops the two drifting: a piece set added later shows
 * up here without anybody remembering to update a second drawing.
 */

/** Where the three men sit — an opening exchange rather than random. */
const PIECES: Record<string, "light" | "dark"> = {
  "1:1": "dark",
  "2:2": "light",
  "2:0": "dark",
};

export function BoardSample({ className }: { className?: string }) {
  return (
    <div
      aria-hidden="true"
      className={cn(
        "border-piece-dark-edge/40 grid aspect-square grid-cols-4 overflow-hidden rounded-md border-2",
        className,
      )}
    >
      {Array.from({ length: 16 }, (_, index) => {
        const row = Math.floor(index / 4);
        const column = index % 4;
        const piece = PIECES[`${row.toString()}:${column.toString()}`];

        return (
          <div
            key={index}
            className={cn(
              "flex items-center justify-center",
              (row + column) % 2 === 1 ? "bg-board-dark" : "bg-board-light",
            )}
          >
            {piece !== undefined && (
              <span
                className={cn(
                  "size-[72%]",
                  PIECE_FINISH_CLASS,
                  piece === "light"
                    ? "border-piece-light-edge bg-piece-light"
                    : "border-piece-dark-edge bg-piece-dark",
                )}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}
