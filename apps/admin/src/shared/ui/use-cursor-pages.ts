import { useCallback, useEffect, useRef, useState } from "react";

import type { Outcome } from "@/shared/api/client";

/**
 * Page-at-a-time navigation over a **keyset** listing — A64-024 hardening.
 *
 * Every admin listing pages by cursor: the server returns rows and a
 * `next_cursor`, and there is no `prev_cursor`, no `offset` and no total.
 * That is deliberate — `specs/admin.md` records why for each console — and
 * this hook is what turns it into something an operator can navigate
 * without changing any of it.
 *
 * ## Where the "previous page" comes from
 *
 * The client remembers the cursor that produced each page it has seen.
 * Page 1 was produced by `null`; page 2 by the cursor page 1 returned; and
 * so on. Going back is therefore re-fetching with a cursor already in hand
 * — one request, no backend change, and the same keyset the forward
 * direction uses.
 *
 * ## Why there is no "jump to page 57"
 *
 * There is no cursor for page 57 until page 56 has been fetched, and the
 * only way to invent one is `OFFSET` — which on a growing table shows a
 * different page 57 depending on what was written since, and scans every
 * row before it. So the control offers **Previous / page number / Next**
 * and no numbered jumps, because a number the operator can click and the
 * server cannot honour is worse than one that is absent.
 *
 * The page number is honest for the same reason: it is how many pages have
 * been walked, not a position in a total nobody counted.
 *
 * ## Filters reset the walk
 *
 * `key` identifies the query. When it changes the history is discarded and
 * the walk restarts at page 1 — a cursor from one filter names a row that
 * may not be in the other's result at all, and continuing with it would
 * silently show a page from the wrong query.
 *
 * ## A failed page says so — A64-031.G
 *
 * A navigation that fails keeps the rows on screen, and before this the
 * operator learned nothing else: the button re-enabled, the page number
 * stayed put, and a click that did nothing looked like a click that had not
 * landed. `failed` is that missing sentence, and it is cleared by the next
 * attempt rather than by a timer, so it is on screen exactly as long as it
 * is true.
 */
export interface CursorPage<T> {
  items: T[];
  next_cursor: string | null;
}

export interface CursorPages<T> {
  rows: T[];
  page: number;
  hasPrevious: boolean;
  hasNext: boolean;
  state: "loading" | "ready" | "error";
  /** True while a *navigation* is in flight, so the control can disable. */
  busy: boolean;
  /** The last navigation failed, and the rows shown are the page before it. */
  failed: boolean;
  next: () => void;
  previous: () => void;
  /**
   * Re-fetch the page currently shown — after a mutation, or as the retry
   * of a failed first load, which then shows its loading state again.
   */
  reload: () => void;
}

export function useCursorPages<T>(
  fetchPage: (cursor: string | null, signal?: AbortSignal) => Promise<Outcome<CursorPage<T>>>,
  key: string,
  /**
   * Whether to fetch at all — A64-027A §34.
   *
   * Defaulted to `true`, so every existing caller is unchanged. The one
   * caller that passes it is the Notifications workspace, whose delivery
   * listing lives on a tab: fetching a page nobody has opened is a request
   * per visit for a table that is not on screen.
   *
   * A parameter rather than a conditional hook call, because a hook cannot
   * be called conditionally — and rather than unmounting the component,
   * because the walk's position should survive a trip to another tab.
   */
  enabled = true,
): CursorPages<T> {
  const [rows, setRows] = useState<T[]>([]);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);

  /**
   * The cursor that produced each page, page 1 first.
   *
   * A ref rather than state: it is navigation bookkeeping, not something
   * the view renders, and putting it in state would re-run the effect that
   * writes it.
   */
  const history = useRef<(string | null)[]>([null]);
  const [index, setIndex] = useState(0);
  const [nextCursor, setNextCursor] = useState<string | null>(null);

  const controller = useRef<AbortController | null>(null);
  const fetcher = useRef(fetchPage);
  fetcher.current = fetchPage;

  const load = useCallback((at: number, { navigating }: { navigating: boolean }) => {
    controller.current?.abort();
    const next = new AbortController();
    controller.current = next;

    setFailed(false);
    if (navigating) setBusy(true);
    else setState("loading");

    return fetcher.current(history.current[at] ?? null, next.signal).then((outcome) => {
      if (next.signal.aborted) return;
      setBusy(false);

      if (outcome.status !== "ok") {
        // A failed navigation leaves the page where it was rather than
        // emptying it: the rows on screen were true when they were
        // fetched, and replacing them with nothing would read as "this
        // page is empty". It is reported instead.
        if (navigating) setFailed(true);
        else setState("error");
        return;
      }

      setRows(outcome.value.items);
      setNextCursor(outcome.value.next_cursor);
      setIndex(at);
      setState("ready");

      // Record the cursor that will produce the page after this one, so
      // `next` has it and `previous` can come back to this one.
      if (outcome.value.next_cursor !== null && history.current.length === at + 1) {
        history.current = [...history.current, outcome.value.next_cursor];
      }
    });
  }, []);

  useEffect(() => {
    if (!enabled) return;
    history.current = [null];
    setIndex(0);
    setNextCursor(null);
    void load(0, { navigating: false });
    return () => controller.current?.abort();
  }, [enabled, key, load]);

  return {
    rows,
    page: index + 1,
    hasPrevious: index > 0,
    hasNext: nextCursor !== null,
    state,
    busy,
    failed,
    next: () => {
      if (nextCursor === null || busy) return;
      void load(index + 1, { navigating: true });
    },
    previous: () => {
      if (index === 0 || busy) return;
      void load(index - 1, { navigating: true });
    },
    // Over rows, a reload keeps them and reports through `failed`. Over a
    // failed first load there are no rows to keep, so it is a load again —
    // otherwise "Try again" against a server that is still down changes
    // nothing on screen and reads as a button that does not work.
    reload: () => void load(index, { navigating: state === "ready" }),
  };
}
