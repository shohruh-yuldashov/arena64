/**
 * Arena64's mark — A64-027A.2 §7.
 *
 * The artwork in `apps/web/public/icons/favicon.svg`, which is itself one
 * of two forms of the description `apps/web/scripts/generate-icons.mjs`
 * rasterises: a two-by-two board with a piece on a dark square.
 *
 * ## Why it is drawn here rather than fetched
 *
 * `apps/admin` must not reach into `apps/web` — AD-04, the rule that keeps
 * the console deployable without the player client. Copying eleven lines of
 * geometry is the alternative to a build-time dependency between two
 * applications, and the values are stated below so a reader can compare
 * them against the source rather than trust that they match.
 *
 * ## Why the colours are literals
 *
 * They are the mark's own, and the mark does not change with the console's
 * theme: a logo that restyles itself in dark mode is two logos. The light
 * square is `#494fcc`, which is `oklch(0.499 0.19 275)` — `--primary` to
 * three decimals, which is why the console can be built out of the mark's
 * palette without inventing a second one.
 *
 * A64-027A.1 drew an "A64" monogram in the brand gradient instead. That was
 * a third brand treatment beside the platform's wordmark and its favicon,
 * and the only one invented in this repository.
 */
export function BrandMark({ size = 34, className }: { size?: number; className?: string }) {
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 100 100"
      role="img"
      aria-label="Arena64"
    >
      <defs>
        {/* Unique per instance is unnecessary — the console renders one
            mark — but a stable id keeps the clip from colliding with any
            other `#board` a page might grow. */}
        <clipPath id="arena64-mark-board">
          <rect width="100" height="100" rx="14" />
        </clipPath>
      </defs>
      <g clipPath="url(#arena64-mark-board)">
        <rect width="100" height="100" fill="#202268" />
        <rect width="50" height="50" fill="#494fcc" />
        <rect x="50" y="50" width="50" height="50" fill="#494fcc" />
        <circle cx="25" cy="75" r="16.5" fill="#fafafa" />
      </g>
    </svg>
  );
}
