import type { ReactNode } from "react";

/**
 * A scrollable, sticky-headed table — A64-027A §5, §30.
 *
 * The wrapper is the whole component. Every table in the console had been
 * repeating the same three decisions and getting one of them wrong
 * somewhere: the border-and-radius frame, the horizontal scroll on narrow
 * screens, and `position: relative` on that scroll container.
 *
 * The third is not cosmetic. A `.sr-only` caption inside a scrolled table
 * is absolutely positioned; with no positioned ancestor it resolves against
 * the initial containing block at a static position *beyond the viewport*,
 * which grows `html`'s scroll width while `body` measures clean — a page
 * that scrolls sideways for no visible reason. A64-027.6 found it at 360px
 * and this wrapper is where the fix now lives for every table at once.
 *
 * §30 permits the internal scroll deliberately: a dense operational table
 * folded into cards loses the column alignment that makes a hundred rows
 * scannable, which is the entire reason it is a table.
 *
 * ## A table with a card twin steps aside on a phone — A64-031.G
 *
 * Five listings render their rows twice, as this table and as a card list
 * below 48rem, and only the old `.users-table` class knew to hide itself
 * there. When those pages moved onto this wrapper the rule stopped
 * matching, and a phone showed every row twice: a scrolling table, then the
 * same rows as cards. `narrow="cards"` says so at the call site rather than
 * in a selector that has to guess which sibling follows.
 */
export function DataTable({
  caption,
  children,
  minWidth,
  narrow = "scroll",
}: {
  /** Names the table for a screen reader. Visually hidden. */
  caption: string;
  children: ReactNode;
  /** Where the table starts scrolling instead of compressing. */
  minWidth?: string;
  /** Below 48rem: scroll, or give way to the card list the page renders. */
  narrow?: "scroll" | "cards";
}) {
  return (
    <div className={narrow === "cards" ? "table-scroll table-scroll--wide" : "table-scroll"}>
      <table
        className="data-table"
        style={minWidth === undefined ? undefined : { minInlineSize: minWidth }}
      >
        <caption className="sr-only">{caption}</caption>
        {children}
      </table>
    </div>
  );
}
