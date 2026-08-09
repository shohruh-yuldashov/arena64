import { QUICK_MESSAGE_PRESENTATION } from "@/features/game/model/quick-messages";
import type { VisibleQuickMessage } from "@/features/game/model/use-quick-messages";
import { useTranslation } from "@/shared/i18n";
import { cn } from "@/shared/lib/cn";

/**
 * One transient quick message, beside the participant who sent it —
 * A64-023.2 §6, §7, §15.
 *
 * ## Why it lives in the panel and not over the board
 *
 * §6 asks for something close to the relevant participant and forbids
 * covering board squares or clocks. `GamePanel` already renders the two
 * seats in a fixed order — opponent above, viewer below — so a bubble
 * rendered *between* the seat's label and its clock is adjacent to the right
 * person by construction, with no positioning maths and nothing overlapping
 * the board.
 *
 * That also means it cannot obscure anything on a phone, which is the case a
 * floating overlay would have had to be tested against at every width.
 *
 * ## Announcement — §15
 *
 * `role="status"` (polite), so a message is read when the reader pauses
 * rather than interrupting a move. Deliberately **not** `alert`: a courtesy
 * is not an interruption, and §15 warns against aggressively repetitive
 * announcements. The glyph is `aria-hidden`; the localised sentence is the
 * content.
 *
 * The `key` the parent passes is what makes a replacement announce: React
 * remounts the node, so the live region sees new content even when the same
 * message arrives twice (§7).
 */
export function QuickMessageBubble({
  bubble,
  align,
}: {
  bubble: VisibleQuickMessage;
  /** Which seat this belongs to, so it sits on that side of the panel. */
  align: "near" | "far";
}) {
  const { t } = useTranslation();
  const { label, glyph } = QUICK_MESSAGE_PRESENTATION[bubble.message];

  return (
    <p
      role="status"
      className={cn(
        "bg-muted text-foreground w-fit max-w-full rounded-lg px-3 py-1.5 text-sm",
        align === "far" ? "self-start" : "self-end",
      )}
    >
      <span aria-hidden="true" className="mr-1.5">
        {glyph}
      </span>
      {t(label)}
    </p>
  );
}
