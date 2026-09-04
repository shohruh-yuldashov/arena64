/**
 * A fragment of a draughts board, drawn in the board's own tokens.
 *
 * The product had no artwork at all, and the first screen a player saw was
 * a heading and four cards. This is the smallest honest answer to that: not
 * a stock illustration of something Arena64 is not, but the surface the
 * whole product is about, at an angle, with three pieces on it.
 *
 * ## Why it is drawn rather than fetched
 *
 * An SVG in the bundle costs no request, scales to any density, and — the
 * reason it is worth writing rather than exporting — reads `--board-light`,
 * `--board-dark`, `--piece-light` and `--piece-dark` at paint time, so it
 * follows the theme and any future change to the board's palette without a
 * second asset to keep in step. A PNG would be two files that drift.
 *
 * ## Decorative, and declared so
 *
 * `aria-hidden` with no title: it carries no information a player needs,
 * and everything the section says is said in text beside it. A screen
 * reader that announced "board illustration" here would be announcing
 * noise.
 */
export function BoardMotif({ className }: { className?: string }) {
  const squares: { x: number; y: number }[] = [];
  for (let row = 0; row < 4; row += 1) {
    for (let column = 0; column < 4; column += 1) {
      if ((row + column) % 2 === 1) squares.push({ x: column * 30, y: row * 30 });
    }
  }

  return (
    <svg
      aria-hidden="true"
      focusable="false"
      viewBox="0 0 120 120"
      className={className}
      style={{ overflow: "visible" }}
    >
      <rect width="120" height="120" rx="6" fill="var(--board-light)" />
      {squares.map((square) => (
        <rect
          key={`${square.x}:${square.y}`}
          x={square.x}
          y={square.y}
          width="30"
          height="30"
          fill="var(--board-dark)"
        />
      ))}

      {/* Three men, placed as an opening exchange rather than at random —
          a dark piece just past the middle, a light one facing it, and a
          second dark one covering the diagonal behind. */}
      <Piece cx={45} cy={45} tone="dark" />
      <Piece cx={75} cy={75} tone="light" />
      <Piece cx={15} cy={75} tone="dark" />
    </svg>
  );
}

function Piece({ cx, cy, tone }: { cx: number; cy: number; tone: "light" | "dark" }) {
  const fill = tone === "light" ? "var(--piece-light)" : "var(--piece-dark)";
  const edge = tone === "light" ? "var(--piece-light-edge)" : "var(--piece-dark-edge)";

  return (
    <g>
      {/* The seated shadow, then the disc, then the inner ring — the same
          three-part relief `board.tsx` gives a piece on the real board, so
          the motif and the game do not look like two products. */}
      <circle cx={cx} cy={cy + 1.5} r="10.5" fill={edge} opacity="0.55" />
      <circle cx={cx} cy={cy} r="10.5" fill={fill} stroke={edge} strokeWidth="1" />
      <circle cx={cx} cy={cy} r="6" fill="none" stroke={edge} strokeWidth="0.9" opacity="0.7" />
    </g>
  );
}
