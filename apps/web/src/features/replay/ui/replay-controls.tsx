import {
  ChevronLeft,
  ChevronRight,
  ChevronsLeft,
  ChevronsRight,
  FlipVertical2,
} from "lucide-react";

import type { ReplayView } from "@/features/replay/model/state";
import { useTranslation } from "@/shared/i18n";
import { Button } from "@/shared/ui";

/**
 * Stepping through a game — A64-020.5E §10, §11, §20.
 *
 * Four navigation controls and an orientation toggle. Every one is a real
 * `<button>` with an explicit accessible name: the icons are decorative
 * (`aria-hidden`) and the name comes from the label beside them, because an
 * icon-only control whose meaning lives in a tooltip is unusable with a
 * screen reader and ambiguous with one.
 *
 * **Boundaries are `disabled`, not silently inert** (§10). A control that
 * stayed clickable and did nothing would be indistinguishable from a broken
 * one — and `disabled` is what tells assistive technology, which no amount
 * of styling does.
 *
 * The current position is announced through a polite live region rather
 * than by moving focus: a player holding the right arrow would otherwise
 * have focus dragged on every press, and §20 asks for focus to remain
 * stable while stepping.
 */
export function ReplayControls({ view }: { view: ReplayView }) {
  const { t, locale } = useTranslation();
  const { position, positionCount } = view;

  const format = new Intl.NumberFormat(locale);

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <Button
          variant="outline"
          className="min-h-11 min-w-11"
          disabled={position.isAtStart}
          onClick={view.first}
        >
          <ChevronsLeft aria-hidden="true" />
          <span className="sr-only sm:not-sr-only">{t("replay.controls.first")}</span>
        </Button>
        <Button
          variant="outline"
          className="min-h-11 min-w-11"
          disabled={position.isAtStart}
          onClick={view.previous}
        >
          <ChevronLeft aria-hidden="true" />
          <span className="sr-only sm:not-sr-only">{t("replay.controls.previous")}</span>
        </Button>
        <Button
          variant="outline"
          className="min-h-11 min-w-11"
          disabled={position.isAtEnd}
          onClick={view.next}
        >
          <span className="sr-only sm:not-sr-only">{t("replay.controls.next")}</span>
          <ChevronRight aria-hidden="true" />
        </Button>
        <Button
          variant="outline"
          className="min-h-11 min-w-11"
          disabled={position.isAtEnd}
          onClick={view.last}
        >
          <span className="sr-only sm:not-sr-only">{t("replay.controls.last")}</span>
          <ChevronsRight aria-hidden="true" />
        </Button>
        <Button variant="outline" className="min-h-11 min-w-11 sm:ml-auto" onClick={view.flip}>
          <FlipVertical2 aria-hidden="true" />
          <span className="sr-only sm:not-sr-only">{t("replay.controls.flip")}</span>
        </Button>
      </div>

      {/* Polite: the position changing is worth announcing and never worth
          interrupting, and a player stepping quickly would otherwise be
          read every intermediate ply. */}
      <p role="status" className="text-muted-foreground text-sm tabular-nums">
        {position.isAtStart
          ? t("replay.position.opening")
          : t("replay.position.at", {
              ply: format.format(position.index),
              total: format.format(positionCount - 1),
            })}
      </p>

      <p className="text-muted-foreground text-xs">{t("replay.shortcuts")}</p>
    </div>
  );
}
