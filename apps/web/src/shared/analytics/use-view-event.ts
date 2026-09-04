/**
 * A view event that fires **once per mount** — A64-027.2 §36.
 *
 * React makes this non-trivial in three separate ways, and a naive
 * `useEffect(() => track(...), [])` gets all three wrong:
 *
 *     StrictMode      mounts, unmounts and remounts every component in
 *                     development, so every view is counted twice
 *     rerenders       a parent's state change re-runs the effect if its
 *                     dependencies are objects
 *     navigation      returning to a route the router kept mounted fires
 *                     nothing at all, or fires again, depending on the
 *                     router's cache
 *
 * The guard is a ref keyed by the event's own identity: the first effect
 * for a given key fires, and nothing else does until the key changes. So
 * navigating between two tournaments counts two views — which is right —
 * and a rerender of one counts none.
 *
 * ## Why not deduplicate on the server
 *
 * It could, and it would be the wrong place: the server cannot tell a
 * genuine second view from a remount, and a rule that collapsed them would
 * silently undercount somebody who really did open a page twice.
 */

import { useEffect, useRef } from "react";

import type { ClientEventName, ClientEvents } from "@/shared/analytics/events";
import { track } from "@/shared/analytics/tracker";

export function useViewEvent<Name extends ClientEventName>(
  name: Name,
  properties: ClientEvents[Name],
  /**
   * What makes this view distinct — a tournament id, or nothing for a page
   * with one identity. A changed key is a new view; an unchanged one is a
   * rerender.
   */
  key: string = name,
): void {
  const fired = useRef<string | null>(null);
  // Read inside the effect rather than listed as a dependency: the
  // properties object is rebuilt on every render, so depending on it would
  // reintroduce exactly the rerender loop this hook exists to prevent.
  const latest = useRef(properties);
  latest.current = properties;

  useEffect(() => {
    if (fired.current === key) return;
    fired.current = key;
    track(name, latest.current);
  }, [name, key]);
}
