import { useTranslation } from "@/shared/i18n";
import { cn } from "@/shared/lib/cn";

/**
 * A four-player bracket, drawn the way the real one is — A64-026.1 §40.6.
 *
 * ## Why it is redrawn rather than reused
 *
 * `features/tournament`'s bracket takes a `BracketNode[]` from
 * `/tournaments/{id}/bracket`, and §16's whole argument is that its
 * connector lines come from `BracketSlot.parent()` — an authoritative
 * relationship, not a CSS approximation. A landing page has no tournament
 * to read, so reusing that component would mean **inventing a payload**,
 * which is the fake data §30 forbids in the one place it would be easiest
 * to justify.
 *
 * So this draws the *shape*: four seats, two pairings, a final, and the
 * lines between them. No player names, no scores, no tournament title.
 *
 * ## The seats are anonymous without looking unfinished
 *
 * The first version used plain grey bars and read as a loading skeleton —
 * which is the failure mode of "honest but empty". Each seat now carries
 * the piece disc the product uses for a side and a neutral name bar, so it
 * reads as a bracket with the names withheld rather than as content that
 * failed to arrive. Nothing here claims a player exists.
 *
 * The connectors are borders on a flex column rather than an SVG: four
 * lines do not earn a second drawing system, and they inherit `--border`
 * so they follow the theme.
 */
export function BracketShowcase({ className }: { className?: string }) {
  const { t } = useTranslation();

  return (
    <div aria-hidden="true" className={cn("flex items-stretch gap-2 sm:gap-3", className)}>
      {/* Round one: two pairings, four seats. */}
      <div className="flex flex-1 flex-col justify-between gap-6 py-2">
        <Pairing eliminated="dark" />
        <Pairing eliminated="light" />
      </div>

      <Connector />

      {/* The final, marked as the live round the way the product marks one. */}
      <div className="flex flex-1 flex-col justify-center">
        <div className="border-primary/40 bg-primary/5 flex flex-col gap-2 rounded-xl border p-3 shadow-sm">
          <span className="text-primary text-[11px] font-semibold tracking-wide uppercase">
            {t("landing.showcase.final")}
          </span>
          <Seat tone="light" />
          <Seat tone="dark" />
        </div>
      </div>
    </div>
  );
}

/**
 * One pairing. The side that did not advance is dimmed, which is the only
 * thing this picture says about an outcome — and it says it about a seat
 * with no name in it.
 */
function Pairing({ eliminated }: { eliminated: "light" | "dark" }) {
  return (
    <div className="border-border bg-card flex flex-col gap-2 rounded-xl border p-3">
      <Seat tone="light" dimmed={eliminated === "light"} />
      <Seat tone="dark" dimmed={eliminated === "dark"} />
    </div>
  );
}

/**
 * A seat: the piece disc that names a side, and a bar where a name goes.
 *
 * A bar rather than a name, because a name here would be a player who does
 * not exist. The disc is what stops the bar reading as a skeleton.
 */
function Seat({ tone, dimmed = false }: { tone: "light" | "dark"; dimmed?: boolean }) {
  return (
    <div className={cn("flex items-center gap-2", dimmed && "opacity-45")}>
      <span
        className={cn(
          "size-4 shrink-0 rounded-full border",
          tone === "dark"
            ? "bg-piece-dark border-piece-dark-edge"
            : "bg-piece-light border-piece-light-edge",
        )}
      />
      <span className="bg-muted h-3 flex-1 rounded-full" />
    </div>
  );
}

/**
 * The two lines from the pairings' mid-points into the final's.
 *
 * A flex column of four equal cells: the middle two carry the borders, so
 * the corners land exactly halfway down each pairing box whatever the
 * height. That is the same geometry the real bracket derives from its
 * parent relationship, expressed in the cheapest way that cannot drift.
 */
function Connector() {
  return (
    <div className="flex w-5 shrink-0 flex-col sm:w-9">
      <div className="flex-1" />
      <div className="border-border flex-1 rounded-tr-lg border-t-2 border-r-2" />
      <div className="border-border flex-1 rounded-br-lg border-r-2 border-b-2" />
      <div className="flex-1" />
    </div>
  );
}
