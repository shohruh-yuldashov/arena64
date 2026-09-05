/**
 * The behavioural event tracker — A64-027.2 §34, §45, §61.
 *
 * **Fire-and-forget, and that is a contract rather than a shortcut.**
 * Nothing here is awaited by a component, nothing throws into a render, and
 * no navigation waits for a request. A lost `landing_viewed` undercounts M1
 * by one; a delayed navigation is a product defect. A64-027.1 §36 states
 * the cost so a dashboard reader never discovers it from a number.
 *
 * ## Batched, flushed on a timer and on page hide
 *
 * A click and the navigation it causes happen in the same instant, so a
 * request started on the click is a request the browser may cancel. The
 * queue is flushed with `sendBeacon` on `visibilitychange`, which is the
 * one delivery mechanism that survives a page going away — and the only
 * reason a CTA click is measurable at all.
 *
 * `pagehide` is not used: it does not fire reliably on mobile, where the
 * tab is frozen rather than unloaded. `visibilitychange` does.
 *
 * ## Typed, and only the four names
 *
 * `track` takes a name from `ClientEvents` and exactly that event's
 * properties. There is no `track(name: string, properties: unknown)`,
 * because the server refuses anything else and a `422` nobody sees is the
 * wrong place to learn about a typo.
 */

import type { ClientEventName, ClientEvents } from "@/shared/analytics/events";
import { anonymousId, sessionId } from "@/shared/analytics/identity";
import { env } from "@/shared/config/env";

interface QueuedEvent {
  event_name: ClientEventName;
  idempotency_key: string;
  anonymous_id: string;
  session_id: string;
  properties: Record<string, unknown>;
}

/** The server's own bound (§39). Flushing at it keeps a batch acceptable. */
const MAX_BATCH = 10;

/** Long enough to batch a page's events, short enough to lose few on a crash. */
const FLUSH_INTERVAL_MS = 5_000;

const ENDPOINT = `${env.VITE_API_URL}/analytics/events`;

let queue: QueuedEvent[] = [];
let timer: ReturnType<typeof setTimeout> | null = null;
let listening = false;

/**
 * Records one event. Returns immediately, always.
 *
 * Never `await`ed by a caller and never throwing: every statement is inside
 * the try, because an analytics failure that reached a click handler would
 * stop the navigation the click was for.
 */
export function track<Name extends ClientEventName>(
  name: Name,
  properties: ClientEvents[Name],
): void {
  try {
    queue.push({
      event_name: name,
      // The retry identity — §27. A dedup key and nothing more: the stored
      // id mixes in the identity the server resolved, so this confers no
      // ability to affect anybody else's events.
      idempotency_key: crypto.randomUUID(),
      anonymous_id: anonymousId(),
      session_id: sessionId(),
      properties: properties,
    });

    listen();
    if (queue.length >= MAX_BATCH) {
      flush();
      return;
    }
    timer ??= setTimeout(flush, FLUSH_INTERVAL_MS);
  } catch {
    /* Measurement must never break the thing being measured. */
  }
}

/**
 * Sends what is queued. Safe to call with an empty queue.
 *
 * `sendBeacon` first: it is the only transport that survives the page going
 * away, which is exactly when a CTA click needs to be sent. It is also
 * *queued by the browser* rather than awaited, so it cannot delay anything.
 *
 * `fetch` with `keepalive` is the fallback for a browser without it and for
 * a payload the beacon queue refuses — `sendBeacon` returns `false` rather
 * than throwing when its buffer is full, which is the one case worth
 * retrying through another transport.
 */
export function flush(): void {
  try {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
    if (queue.length === 0) return;

    const body = JSON.stringify({ events: queue.slice(0, MAX_BATCH) });
    const remaining = queue.slice(MAX_BATCH);
    queue = remaining;

    const blob = new Blob([body], { type: "application/json" });
    if (navigator.sendBeacon?.(ENDPOINT, blob)) return;

    void fetch(ENDPOINT, {
      method: "POST",
      body,
      headers: { "Content-Type": "application/json" },
      // The session cookie, so a signed-in player's events are attributed.
      // The server takes the identity from it; the body has no field for
      // one (§29).
      credentials: "include",
      keepalive: true,
      // A rejected promise here is a lost event, which is the stated
      // contract. Swallowed rather than reported: a network failure on a
      // best-effort beacon is not an incident, and reporting each one
      // would fill a log with them.
    }).catch(() => undefined);
  } catch {
    /* See `track`. */
  }
}

/** Flush on page hide — the delivery that makes a CTA click measurable. */
function listen(): void {
  if (listening || typeof document === "undefined") return;
  listening = true;
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") flush();
  });
}

/** Drops anything queued. For tests, and for sign-out. */
export function resetTracker(): void {
  queue = [];
  if (timer !== null) {
    clearTimeout(timer);
    timer = null;
  }
}
