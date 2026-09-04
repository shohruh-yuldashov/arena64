import { PIECE_FINISH_CLASS } from "@/entities/board";
import { useTranslation } from "@/shared/i18n";
import { cn } from "@/shared/lib/cn";

/**
 * The game room, as a still — A64-026.1 §40.3.
 *
 * ## Why this is not the real board
 *
 * `features/game/ui/board.tsx` is the product's board and it is the wrong
 * thing to put on a public page. It takes a `GameState`, and a `GameState`
 * comes from `useGameRoom`, which opens a socket, joins a room and holds a
 * request registry. A landing page is the **first** request a visitor makes
 * and the one they judge the product's speed by; pulling the realtime stack
 * into that bundle to draw a static picture is the trade §26 forbids.
 *
 * So this is a presentation component: no engine, no socket, no query, no
 * state. It reads the same tokens the real board does — `--board-light`,
 * `--board-dark` and the piece variables — so the picture follows the theme
 * and any future change to the board's palette, exactly as `BoardMotif`
 * does and for the same reason.
 *
 * ## What it shows, and why every part of it is honest
 *
 * A mid-game position with a capture available, two seats, two clocks and a
 * last move. All of it is what the real room renders; none of it is a
 * number the server did not send, because there is no server here and the
 * page says so by showing a *position* rather than a statistic.
 *
 * The two names are `game.showcase.*` strings rather than invented players:
 * "Light" and "Dark" are the sides the domain itself names, and a made-up
 * username on a marketing page is the first step towards a made-up rating
 * beside it. The ratings shown are the platform's own starting value,
 * which every new account genuinely has.
 *
 * ## Decorative, and declared so
 *
 * `aria-hidden` on the board and the clocks: they carry nothing a visitor
 * needs that the surrounding copy does not already say, and a screen reader
 * announcing sixty-four squares would be announcing noise. The section's
 * text is the accessible content.
 */

/**
 * A mid-game position, `row:column` with row 0 at the top.
 *
 * **Every piece is on a dark square**, which is not decoration: draughts is
 * played on one colour, and a picture showing a man on a light square is
 * wrong in a way any player spots before they read a word of the copy. The
 * assertion below is what keeps it true if this table is ever edited.
 *
 * Dark's men are at the top, Light's at the bottom, one Light king has
 * come through — the shape of a game about two thirds of the way in.
 */
const PIECES: Readonly<Record<string, "light" | "dark" | "light-king">> = {
  "1:2": "dark",
  "1:4": "dark",
  "2:1": "dark",
  "2:5": "dark",
  "2:7": "dark",
  "3:4": "dark",
  "4:3": "light",
  "4:7": "light-king",
  "5:2": "light",
  "5:6": "light",
  "6:1": "light",
  "6:5": "light",
};

/** Where Dark's last move started and finished. Both dark squares. */
const LAST_MOVE = new Set(["2:3", "3:4"]);

export function BoardShowcase({ className }: { className?: string }) {
  const { t } = useTranslation();

  return (
    <div
      className={cn(
        "border-border bg-card flex flex-col gap-3 rounded-2xl border p-3 shadow-sm sm:gap-4 sm:p-4",
        className,
      )}
    >
      <Seat
        name={t("landing.showcase.dark")}
        rating="1500"
        clock="4:12"
        active={false}
        tone="dark"
      />

      <div
        aria-hidden="true"
        className="border-piece-dark-edge/30 grid aspect-square grid-cols-8 overflow-hidden rounded-lg border"
      >
        {Array.from({ length: 64 }, (_, index) => {
          const row = Math.floor(index / 8);
          const column = index % 8;
          const key = `${row.toString()}:${column.toString()}`;
          const piece = PIECES[key];
          const dark = (row + column) % 2 === 1;

          return (
            <div
              key={key}
              className={cn(
                "relative flex items-center justify-center",
                dark ? "bg-board-dark" : "bg-board-light",
                // The last move, in the same `--primary` wash the real board
                // uses for it. One accent on this picture, and it is the one
                // that means "this just happened".
                LAST_MOVE.has(key) && "after:bg-primary/25 after:absolute after:inset-0",
              )}
            >
              {piece !== undefined && (
                <span
                  className={cn(
                    "relative z-10 size-[72%]",
                    PIECE_FINISH_CLASS,
                    piece === "dark"
                      ? "bg-piece-dark border-piece-dark-edge"
                      : "bg-piece-light border-piece-light-edge",
                  )}
                >
                  {piece === "light-king" && (
                    // A king is a ring, which is what the board draws.
                    <span className="border-piece-light-edge absolute inset-[26%] rounded-full border-2" />
                  )}
                </span>
              )}
            </div>
          );
        })}
      </div>

      <Seat name={t("landing.showcase.light")} rating="1500" clock="3:47" active tone="light" />
    </div>
  );
}

/**
 * One player strip: a disc, a name, a rating and a clock.
 *
 * The same four facts `PlayerSeat` shows in the real room, in the same
 * order, so a visitor who signs up recognises the thing they were shown.
 */
function Seat({
  name,
  rating,
  clock,
  active,
  tone,
}: {
  name: string;
  rating: string;
  clock: string;
  active: boolean;
  tone: "light" | "dark";
}) {
  return (
    <div
      aria-hidden="true"
      className={cn(
        "flex items-center gap-3 rounded-xl border px-3 py-2",
        active ? "border-primary/40 bg-primary/5" : "border-border bg-muted/40",
      )}
    >
      <span
        className={cn(
          "size-6 shrink-0",
          PIECE_FINISH_CLASS,
          tone === "dark"
            ? "bg-piece-dark border-piece-dark-edge"
            : "bg-piece-light border-piece-light-edge",
        )}
      />
      <span className="min-w-0 flex-1 truncate text-sm font-medium">{name}</span>
      <span className="text-muted-foreground text-xs tabular-nums">{rating}</span>
      <span
        className={cn(
          "rounded-md px-2 py-1 text-sm font-semibold tabular-nums",
          active ? "bg-primary/10 text-primary" : "bg-muted text-muted-foreground",
        )}
      >
        {clock}
      </span>
    </div>
  );
}
