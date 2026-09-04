import { useCallback, useEffect, useState } from "react";

import type { Square } from "@/entities/board";

/**
 * The move a player has chosen but not yet played — A64-025.14 §38.
 *
 * ## Why the preference needs a step rather than a flag
 *
 * `confirm_move` was the last of the five gameplay preferences that nothing
 * read. It is also the only one that could not be closed by writing an
 * attribute on the document: the other four change how something *looks*,
 * and this one changes when a move leaves the browser.
 *
 * So `useMoveSelection` is untouched. It still builds a path and still hands
 * back a completed one; what changes is who receives it. Without the
 * preference the page submits it. With the preference this holds it, the
 * board keeps showing it, and a button sends it.
 *
 * ## It clears itself, and that is the part worth being careful about
 *
 * A staged move is a claim about a position. The moment the position moves
 * under it — the opponent played, the game ended, a resync replaced the
 * board — the staged path is a move that may no longer be legal, and
 * submitting it would be rejected at best.
 *
 * `sequence` is what says the position changed — `GameState` calls it "the
 * authoritative ply, never advanced without a server frame", which is
 * exactly the signal wanted: it moves for the opponent's move as well as
 * this player's, and a resync carries the server's. So the staged move is
 * dropped whenever it changes, rather than when this hook guesses that
 * something happened.
 *
 * ## Confirming is not a second chance to change the move
 *
 * There is no editing here. A player who wants a different move cancels and
 * picks again, which puts them back in the selection they already
 * understand. A staged move that could be adjusted would be a second
 * selection model with its own rules about multi-capture prefixes, and
 * `useMoveSelection`'s docstring is the argument for why there is only one.
 */
export interface MoveConfirmation {
  /** The path awaiting confirmation, or `null` when nothing is staged. */
  staged: readonly Square[] | null;
  /**
   * Takes a completed path.
   *
   * Returns `true` when it was staged and the caller should not submit, and
   * `false` when confirmation is off and the caller owns it as before.
   */
  stage: (path: readonly Square[]) => boolean;
  /** Plays the staged move. Does nothing when none is staged. */
  confirm: () => void;
  /** Drops it, leaving the player where they were before they chose. */
  cancel: () => void;
}

export function useMoveConfirmation({
  enabled,
  sequence,
  onSubmit,
}: {
  /** The player's `confirm_move` preference. */
  enabled: boolean;
  /** `GameState.sequence`. A change drops anything staged against the old position. */
  sequence: number;
  onSubmit: (path: readonly Square[]) => void;
}): MoveConfirmation {
  const [staged, setStaged] = useState<readonly Square[] | null>(null);

  useEffect(() => {
    // Not a dependency on `staged`: this runs when the position changes,
    // and setting state to `null` when it already is null is a no-op React
    // bails out of. Including it would re-run the effect on every stage.
    setStaged(null);
  }, [sequence]);

  // Switching the preference off mid-game must not strand a staged move on
  // screen with no control left to answer it.
  useEffect(() => {
    if (!enabled) setStaged(null);
  }, [enabled]);

  const stage = useCallback(
    (path: readonly Square[]): boolean => {
      if (!enabled) return false;
      setStaged(path);
      return true;
    },
    [enabled],
  );

  // Read from the closure, not from a state updater. React may invoke an
  // updater twice — StrictMode does, deliberately — and a submit inside one
  // would send the move twice.
  const confirm = useCallback(() => {
    if (staged === null) return;
    onSubmit(staged);
    setStaged(null);
  }, [staged, onSubmit]);

  const cancel = useCallback(() => setStaged(null), []);

  return { staged, stage, confirm, cancel };
}
