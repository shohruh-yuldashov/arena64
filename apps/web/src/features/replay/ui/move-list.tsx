import { useEffect, useRef } from "react";

import type { ReplayPly } from "@/features/replay/api";
import type { ReplayView } from "@/features/replay/model/state";
import { useTranslation } from "@/shared/i18n";
import { cn } from "@/shared/lib/cn";

/**
 * The moves, in order — A64-020.5E §9, §20.
 *
 * ## The path is shown whole, and no notation is invented
 *
 * The backend publishes a **coordinate path** and no notation, so this
 * renders `c3–d4` and `f6–d4–b2` rather than claiming a PDN dialect the
 * repository has never chosen (§9). A multi-capture reduced to its
 * endpoints would also be wrong twice over: two different capture
 * sequences can share an origin and a destination, and which pieces came
 * off is the thing a reader is looking at the move list to find out.
 *
 * ## Rows are pairs, entries are plies
 *
 * Light and dark are grouped into a numbered row because that is how a
 * draughts score reads — but the **index is still the ply**, and clicking
 * an entry jumps to the position after it. §6 makes that distinction
 * structural; this only presents it.
 *
 * ## Every entry is a real button
 *
 * §20. A `div` with a click handler is invisible to a keyboard and to a
 * screen reader's controls list, and `aria-current` on the active one is
 * what says *which* without relying on the highlight — colour is never the
 * only indicator.
 */
export function MoveList({ plies, view }: { plies: readonly ReplayPly[]; view: ReplayView }) {
  const { t } = useTranslation();
  const active = useRef<HTMLButtonElement>(null);

  // Scrolled into view when the index changes, including when a keyboard
  // shortcut moved it — otherwise stepping past the visible window would
  // leave the current move off screen with nothing to indicate where.
  //
  // `nearest` rather than `center`: it scrolls only when it has to, so a
  // move already visible does not shift the list under the reader.
  useEffect(() => {
    // Guarded because `scrollIntoView` is not implemented in jsdom, and a
    // missing browser affordance must not take the page down — the move
    // list is still correct without it, only less convenient. The same
    // reason `parseFrame` never throws: a nicety is not worth a crash.
    active.current?.scrollIntoView?.({ block: "nearest" });
  }, [view.position.index]);

  if (plies.length === 0) {
    // §18: a completed match with no moves is valid, not empty. The board
    // and the result are still rendered; this says why the list is not.
    return <p className="text-muted-foreground text-sm">{t("replay.moves.none")}</p>;
  }

  const rows: { number: number; light?: ReplayPly; dark?: ReplayPly }[] = [];
  for (const ply of plies) {
    const number = Math.ceil(ply.ply_number / 2);
    const row = rows.at(-1)?.number === number ? rows.at(-1) : undefined;
    const target = row ?? { number };
    if (row === undefined) rows.push(target);
    if (ply.side === "light") target.light = ply;
    else target.dark = ply;
  }

  return (
    <ol
      aria-label={t("replay.moves.label")}
      className="max-h-[22rem] overflow-y-auto text-sm lg:max-h-[32rem]"
    >
      {rows.map((row) => (
        <li key={row.number} className="grid grid-cols-[2.5rem_1fr_1fr] items-stretch gap-1">
          <span className="text-muted-foreground py-1 text-right tabular-nums">
            {row.number}.
          </span>
          {[row.light, row.dark].map((ply, column) =>
            ply === undefined ? (
              <span key={column} aria-hidden="true" />
            ) : (
              <button
                key={column}
                ref={view.position.index === ply.ply_number ? active : null}
                type="button"
                // `page` is the member `aria-current` offers for "the one
                // being viewed" in an ordered set; `true` would be vaguer.
                aria-current={view.position.index === ply.ply_number ? "step" : undefined}
                onClick={() => view.goTo(ply.ply_number)}
                className={cn(
                  "hover:bg-muted rounded px-2 py-1 text-left font-medium tabular-nums",
                  "focus-visible:ring-ring focus-visible:ring-2 focus-visible:outline-none",
                  view.position.index === ply.ply_number && "bg-primary/10 text-primary",
                )}
              >
                {formatPath(ply.path)}
                {ply.promoted_to === "king" && (
                  <span className="text-muted-foreground ml-1" aria-hidden="true">
                    ♛
                  </span>
                )}
                <span className="sr-only">
                  {t(ply.side === "light" ? "game.side.light" : "game.side.dark")}
                  {ply.captured.length > 0 &&
                    `, ${t("replay.moves.captured", { count: String(ply.captured.length) })}`}
                  {ply.promoted_to === "king" && `, ${t("replay.moves.promoted")}`}
                </span>
              </button>
            ),
          )}
        </li>
      ))}
    </ol>
  );
}

/**
 * `["f6","d4","b2"]` → `f6–d4–b2`.
 *
 * An en dash rather than a hyphen, and every square rather than the ends —
 * see this module's docstring on why the middle of a capture sequence is
 * the part that matters.
 */
function formatPath(path: readonly string[]): string {
  return path.join("–");
}
