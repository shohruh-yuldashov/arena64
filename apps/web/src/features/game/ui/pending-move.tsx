import { useTranslation } from "@/shared/i18n";
import { Button } from "@/shared/ui";

/**
 * The move a player has chosen, and the two answers to it — A64-025.14 §38.
 *
 * ## Why it sits under the board rather than in the panel
 *
 * It is about the squares directly above it. A control that answers "play
 * this?" belongs where the "this" is, and the panel is where the *match*
 * is discussed — the status line, the result, the rating. On a phone the
 * panel is below the fold while a player is choosing.
 *
 * ## Play first, cancel second
 *
 * A player who staged a move meant to make it; confirming is the expected
 * answer and gets the filled button. Cancel is the correction, and an
 * outline. Reversing them would put the destructive answer under the thumb
 * that just chose a move.
 *
 * ## `role="status"`, not `alert`
 *
 * Nothing failed. A screen-reader user needs to hear that the move is
 * waiting, at the next pause rather than mid-sentence — and the board has
 * already announced the squares as they were chosen.
 */
export function PendingMove({
  onConfirm,
  onCancel,
  disabled,
}: {
  onConfirm: () => void;
  onCancel: () => void;
  /** True while a previous move is still in flight. */
  disabled: boolean;
}) {
  const { t } = useTranslation();

  return (
    <div
      role="status"
      className="border-primary/40 bg-primary/5 flex flex-wrap items-center justify-between gap-3 rounded-xl border px-4 py-3"
    >
      <p className="text-sm font-medium">{t("game.confirmMove.prompt")}</p>
      {/* Play is **first in the DOM and second on screen**, and
          `flex-row-reverse` is what separates the two orders. A keyboard
          therefore reaches the expected answer first — the player staged
          this move on purpose — while a pointer finds it on the right,
          where a primary action belongs beside its cancel. */}
      <div className="flex flex-row-reverse gap-2">
        <Button onClick={onConfirm} disabled={disabled}>
          {t("game.confirmMove.play")}
        </Button>
        <Button variant="outline" onClick={onCancel}>
          {t("game.confirmMove.cancel")}
        </Button>
      </div>
    </div>
  );
}
