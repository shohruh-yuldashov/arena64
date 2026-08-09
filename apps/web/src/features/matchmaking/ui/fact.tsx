import type { ReactNode } from "react";

/**
 * One labelled fact about a match, as a chip — A64-025.5 §9, §11.
 *
 * Shared because two surfaces show the same three things and a player
 * should recognise the second from the first: the queue says what it is
 * searching for, and the offer says what it found. Two differently-shaped
 * tables for one configuration is how a person stops being sure they are
 * looking at the same game.
 *
 * A `dt`/`dd` pair rather than two spans — the label is not decoration, and
 * "Time control, 3+2" is what a screen reader should read.
 */
export function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="border-border bg-muted/40 flex items-baseline gap-1.5 rounded-md border px-2.5 py-1.5 text-sm">
      <dt className="text-muted-foreground text-xs">{label}</dt>
      <dd className="font-medium">{children}</dd>
    </div>
  );
}
