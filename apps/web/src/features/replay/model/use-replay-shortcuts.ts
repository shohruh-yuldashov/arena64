import { useEffect } from "react";

import type { ReplayView } from "@/features/replay/model/state";

/**
 * Arrow keys, Home and End — A64-020.5E §11.
 *
 * One document-level listener rather than a focus trap on the board,
 * because a player stepping through a game should not have to keep a
 * specific element focused to press an arrow key. The buttons remain the
 * primary accessible controls (§11); this is an accelerator over them.
 *
 * ## What it deliberately does not capture
 *
 * A key pressed while the caret is in a field. Without that check, typing
 * a display name in a dialog over this page would step the board, and the
 * player would see the game move under a form they were filling in — so
 * the guard covers `input`, `textarea`, `select` and anything
 * `contenteditable`.
 *
 * Modified presses are ignored too. `Ctrl+Left` and `Cmd+Left` are word
 * navigation and history navigation on the platforms that have them, and
 * a page that swallowed them would break a browser affordance to save a
 * keystroke this page already offers unmodified.
 */
export function useReplayShortcuts(view: ReplayView, enabled: boolean): void {
  const { first, previous, next, last } = view;

  useEffect(() => {
    if (!enabled) return;

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.altKey || event.ctrlKey || event.metaKey) return;
      if (isTyping(event.target)) return;

      const action = {
        ArrowLeft: previous,
        ArrowRight: next,
        Home: first,
        End: last,
      }[event.key];
      if (action === undefined) return;

      // Only for keys this page genuinely handles — `preventDefault` on
      // everything would stop `Home` scrolling a page that has nothing to
      // step through.
      event.preventDefault();
      action();
    };

    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [enabled, first, previous, next, last]);
}

/** Whether the caret is somewhere a key press belongs to. */
function isTyping(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable) return true;
  return ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName);
}
