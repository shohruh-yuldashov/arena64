import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from "react";

import {
  QUICK_MESSAGE_ORDER,
  QUICK_MESSAGE_PRESENTATION,
} from "@/features/game/model/quick-messages";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import { cn } from "@/shared/lib/cn";
import type { GatewayErrorCode, QuickMessage } from "@/shared/realtime";
import { Button } from "@/shared/ui";

/**
 * The quick-message picker — A64-023.2 §2, §15, §16.
 *
 * A compact non-modal menu: a trigger, six items, and nothing else. There is
 * **no text input, no textarea and no editable element anywhere in this
 * file** — the only thing a player can do is choose one of six values the
 * server already published, which is what makes "arbitrary text cannot be
 * sent" a property of the UI as well as of the protocol.
 *
 * ## Why this is hand-written rather than a library primitive
 *
 * This repository has no popover, menu or dropdown primitive — only
 * `@radix-ui/react-dialog`, and only for two confirmation dialogs. The two
 * options were adding a dependency or writing the interaction, and this is
 * the smaller liability (CLAUDE.md §2.6): the whole behaviour is a listbox
 * of six static items with arrow keys and Escape.
 *
 * **A dialog would have been wrong even though one exists.** `ResignDialog`
 * is modal — it traps focus and dims the page — which is right for a
 * destructive confirmation and wrong for saying "nice move" during a blitz
 * game with a clock running. This closes on Escape, on outside click and on
 * choosing, and never takes focus away from the board unless the player
 * opened it.
 *
 * ## Accessibility — §15
 *
 *     trigger      a real button, named, `aria-haspopup="menu"` and
 *                  `aria-expanded` so the state is available to assistive
 *                  technology rather than implied by appearance
 *     menu         `role="menu"`, items `role="menuitem"`
 *     keyboard     Up/Down move, Home/End jump, Enter/Space choose,
 *                  Escape closes and **returns focus to the trigger**
 *     glyphs       `aria-hidden` — the localised text is the content, and
 *                  "person with folded hands" read aloud mid-game is noise
 *
 * ## Responsive — §16, and the desktop bug it missed — A64-031.B
 *
 * This opened **upward and only upward** (`bottom-full`), reasoning that
 * "the control sits under the panel and a menu opening downward on a phone
 * would leave the viewport". That is true on a phone, where the panel is
 * stacked below the board and the trigger is far down the page.
 *
 * On desktop the same panel is the right-hand column, so the trigger sits
 * near the **top** of the document — and a menu with no height bound and no
 * collision handling grew upward past the top of the page, where nothing can
 * scroll to it. The upper items were not merely off-screen; they were
 * unreachable. Six rows is roughly 260px, and the trigger is routinely
 * closer to the top than that.
 *
 * So placement is now measured rather than assumed: the menu opens into
 * whichever side of the trigger has more room, and is bounded to that room
 * with `overflow-y-auto` so a list that cannot fit scrolls instead of
 * escaping. Both halves are needed — flipping alone still overflows a short
 * viewport, and bounding alone can leave a 40px menu when the other side had
 * 400px going spare.
 */

/** A refused send as a sentence — §9. */
function errorKey(code: GatewayErrorCode): TranslationKey {
  const known: Partial<Record<GatewayErrorCode, TranslationKey>> = {
    rate_limited: "game.quickMessages.errors.rateLimited",
    match_not_active: "game.quickMessages.errors.matchNotActive",
    unknown_quick_message: "game.quickMessages.errors.unavailable",
  };
  // Anything else is an ordinary transport problem, and §9 forbids showing
  // frightening generic websocket text for what is usually a slow down.
  return known[code] ?? "game.quickMessages.errors.unknown";
}

/**
 * Where the menu opens, and how much room it has there — A64-031.B.
 *
 * `null` until the trigger has been measured, which is one frame: rendering
 * the menu before its placement is known would put it in the wrong place and
 * move it, and a menu that jumps is worse than one that appears a frame late.
 */
interface Placement {
  side: "above" | "below";
  maxHeight: number;
}

/** The gap the menu keeps from the trigger and from the viewport edge. */
const MENU_GAP_PX = 8;

/**
 * Measured on the **positioned ancestor**, not on the trigger.
 *
 * `top-full` and `bottom-full` resolve against the offset parent, and that
 * parent holds the trigger *and* the mute button under `flex-wrap`. In the
 * 320px desktop panel the two wrap onto separate rows, so the box the menu is
 * actually anchored to ends lower than the trigger does — and a bound
 * computed from the trigger let the menu hang past the bottom of the
 * viewport. Thirteen pixels, caught by the end-to-end measurement and by
 * nothing else.
 */
function placementFor(anchor: HTMLElement): Placement {
  const rect = anchor.getBoundingClientRect();
  const above = rect.top - MENU_GAP_PX;
  const below = window.innerHeight - rect.bottom - MENU_GAP_PX;
  // Ties go **above**, which keeps the established behaviour on every layout
  // that was already correct — a phone, where the trigger is near the bottom
  // and `above` wins by a wide margin anyway.
  const side = above >= below ? "above" : "below";
  // **No floor.** A minimum height was the obvious kindness and is the one
  // thing that can put the menu back outside the viewport: forcing 96px into
  // 83px of room overflows by thirteen, which is exactly what the end-to-end
  // measurement caught. The larger side is already chosen, so a small number
  // here means the viewport is genuinely small — and a short menu that
  // scrolls is usable, where one that hangs off the edge is not.
  return { side, maxHeight: Math.max(0, Math.floor(side === "above" ? above : below)) };
}

export function QuickMessagePicker({
  disabled,
  muted,
  error,
  onSelect,
  onToggleMute,
}: {
  /** `true` once the match is terminal or the socket is not ready — §10. */
  disabled: boolean;
  muted: boolean;
  error: GatewayErrorCode | null;
  onSelect: (message: QuickMessage) => void;
  onToggleMute: () => void;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [placement, setPlacement] = useState<Placement | null>(null);
  const menuId = useId();

  const anchor = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const muteButton = useRef<HTMLButtonElement>(null);
  const items = useRef<(HTMLButtonElement | null)[]>([]);

  const close = useCallback((returnFocus: boolean) => {
    setOpen(false);
    if (returnFocus) trigger.current?.focus();
  }, []);

  // A terminal match closes an open picker rather than leaving a menu whose
  // items all refuse — §10.
  //
  // Focus moves **only if it is inside the menu** — A64-023.4 §7. A game
  // ending while the picker is open would otherwise unmount the focused
  // item and drop focus to `<body>`, stranding a keyboard user at the top
  // of the document at the exact moment the result appears. The condition
  // matters as much as the move: a player who opened the picker and then
  // clicked the board must not have focus yanked back.
  //
  // It goes to the **mute** button rather than the trigger, and that is not
  // a preference. The trigger is `disabled` by the time this runs, and a
  // disabled button cannot hold focus — focusing it silently does nothing
  // and leaves the player exactly where this exists to rescue them from.
  // Mute stays enabled on a finished match (§10), so it is the nearest
  // control that can actually receive focus.
  useEffect(() => {
    if (!disabled) return;
    const inside = menu.current?.contains(document.activeElement) === true;
    setOpen(false);
    if (inside) muteButton.current?.focus();
  }, [disabled]);

  // Focus the first item when the menu opens, which is what makes the
  // keyboard path work: a menu nobody can reach with Tab is not accessible
  // merely because it has the right roles.
  useEffect(() => {
    if (open) items.current[0]?.focus();
  }, [open]);

  // Measured on open, and again while the viewport or the page moves under
  // it — A64-031.B. `useLayoutEffect` so the menu is placed before the
  // browser paints it; with `useEffect` it would render in the default
  // position for a frame and then jump.
  //
  // Scroll as well as resize: this is anchored to a trigger inside a page
  // that scrolls, so the room above and below it changes without the window
  // changing size at all. `capture` because the scroll may be an ancestor's
  // rather than the document's.
  useLayoutEffect(() => {
    if (!open) {
      setPlacement(null);
      return;
    }
    const measure = () => {
      if (anchor.current === null) return;
      const next = placementFor(anchor.current);
      // Compared before it is set: a scroll that moves nothing produces an
      // identical decision, and a new object per  event would re-render the menu
      // under the player's cursor for no reason.
      setPlacement((current) =>
        current !== null && current.side === next.side && current.maxHeight === next.maxHeight
          ? current
          : next,
      );
    };
    measure();
    window.addEventListener("resize", measure);
    window.addEventListener("scroll", measure, true);
    return () => {
      window.removeEventListener("resize", measure);
      window.removeEventListener("scroll", measure, true);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;

    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (menu.current?.contains(target) === true) return;
      if (trigger.current?.contains(target) === true) return;
      // No focus return on an outside click: the player is reaching for
      // something else, and pulling focus back would fight them.
      close(false);
    };

    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [open, close]);

  const onKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    const count = QUICK_MESSAGE_ORDER.length;
    const active = items.current.indexOf(document.activeElement as HTMLButtonElement);

    switch (event.key) {
      case "Escape":
        event.preventDefault();
        close(true);
        return;
      case "ArrowDown":
        event.preventDefault();
        items.current[(active + 1) % count]?.focus();
        return;
      case "ArrowUp":
        event.preventDefault();
        items.current[(active - 1 + count) % count]?.focus();
        return;
      case "Home":
        event.preventDefault();
        items.current[0]?.focus();
        return;
      case "End":
        event.preventDefault();
        items.current[count - 1]?.focus();
        return;
      case "Tab":
        // Tabbing away closes it, so focus never lands behind an open menu.
        close(false);
        return;
      default:
        return;
    }
  };

  return (
    <div className="flex flex-col gap-3">
      {/* The group says what it is, exactly as `GameControls` does. Without
          it this was two loose buttons between two labelled groups, which is
          what made the panel read as a list rather than as sections. */}
      <h2 className="text-muted-foreground text-xs font-medium">
        {t("game.quickMessages.heading")}
      </h2>

      <div ref={anchor} className="relative flex flex-wrap items-center gap-2">
        <Button
          ref={trigger}
          type="button"
          variant="outline"
          className="min-h-11"
          disabled={disabled}
          aria-haspopup="menu"
          aria-expanded={open}
          aria-controls={open ? menuId : undefined}
          onClick={() => setOpen((current) => !current)}
        >
          {t("game.quickMessages.open")}
        </Button>

        {/* §11, §12: small, secondary, and never visually dominant. The
            pressed state is what carries the mute to assistive technology —
            a label that only changed its text would leave a screen reader
            without the state itself. */}
        <Button
          ref={muteButton}
          type="button"
          variant="ghost"
          className="min-h-11"
          aria-pressed={muted}
          onClick={onToggleMute}
        >
          {t(muted ? "game.quickMessages.unmute" : "game.quickMessages.mute")}
        </Button>

        {open && (
          <div
            ref={menu}
            id={menuId}
            role="menu"
            aria-label={t("game.quickMessages.open")}
            // The measured decision, as state a test can read — the layout
            // it produces is a browser's to compute and jsdom has none.
            data-placement={placement?.side}
            onKeyDown={onKeyDown}
            // Bounded to the room actually measured, and scrolling inside
            // it. `overscroll-contain` so reaching the end of a scrolled
            // menu does not start scrolling the page behind it.
            style={{ maxHeight: placement?.maxHeight }}
            className={cn(
              "bg-popover absolute left-0 z-40 flex w-56 max-w-[calc(100vw-2rem)]",
              "border-border flex-col gap-1 overflow-y-auto overscroll-contain",
              "rounded-md border p-1 shadow-md",
              // Hidden until measured — one frame, and it prevents the jump
              // that placing after paint would cause.
              placement === null && "invisible",
              placement?.side === "below" ? "top-full mt-2" : "bottom-full mb-2",
            )}
          >
            {QUICK_MESSAGE_ORDER.map((message, index) => {
              const { label, glyph } = QUICK_MESSAGE_PRESENTATION[message];
              return (
                <button
                  key={message}
                  ref={(node) => {
                    items.current[index] = node;
                  }}
                  type="button"
                  role="menuitem"
                  className="hover:bg-accent focus-visible:bg-accent flex min-h-11 items-center gap-2 rounded-sm px-3 text-left text-sm outline-none"
                  onClick={() => {
                    onSelect(message);
                    // §5: the picker closes on selection and shows nothing.
                    // The bubble appears when the server's fan-out arrives.
                    close(true);
                  }}
                >
                  <span aria-hidden="true">{glyph}</span>
                  <span>{t(label)}</span>
                </button>
              );
            })}
          </div>
        )}
      </div>

      {error !== null && (
        <p role="alert" className="text-muted-foreground text-xs">
          {t(errorKey(error))}
        </p>
      )}
    </div>
  );
}
