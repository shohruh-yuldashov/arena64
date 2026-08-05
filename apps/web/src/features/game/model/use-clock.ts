import { useEffect, useRef, useState } from "react";

import type { ClockPayload, Side } from "@/shared/realtime";

/**
 * The clock, interpolated between server frames — A64-020.5B §19.
 *
 * ## The server adjudicates; this only counts
 *
 * §19 is explicit and the rule matters more here than anywhere else in the
 * client: **when the visual clock reaches zero nothing happens**. No
 * winner, no state change, no submission blocked beyond what the server
 * already said. The flag is `game.move.applied` carrying a result, or a
 * `clock_expired` rejection — both of which arrive as frames.
 *
 * A client that adjudicated locally would decide games on the accuracy of
 * `Date.now()` on somebody's laptop, which is the one input on this
 * platform nobody controls.
 *
 * ## Drift is corrected against `server_time`, not accumulated
 *
 * The payload carries **absolute instants** — `deadline` and `server_time`
 * — precisely so a client can compute its own offset rather than trusting
 * its clock: `offset = server_time - received_at`. Every authoritative
 * update recomputes it, so a machine whose clock is a minute fast shows the
 * right countdown from the first frame rather than drifting into it.
 *
 * ## One scheduler, not one per clock
 *
 * §19 and §28 both require it. Two `setInterval`s — one per side — would be
 * two timers firing at different moments, re-rendering the panel twice a
 * second and showing two numbers computed from two instants. This hook runs
 * one timer and returns both numbers.
 *
 * The tick is 250 ms rather than 1000: a second-resolution timer drifts
 * visibly against a deadline it does not divide evenly, and the displayed
 * value is floored to whole seconds anyway, so four ticks a second is what
 * makes the seconds change *on time* without the display changing four
 * times.
 */
export interface ClockReading {
  lightMs: number;
  darkMs: number;
  activeSide: Side | null;
  /** The local clock has passed the deadline. The server has not spoken. */
  awaitingServer: boolean;
}

/** How often the countdown recomputes. See this module's docstring. */
export const CLOCK_TICK_MS = 250;

export function useClock(clock: ClockPayload | null, running: boolean): ClockReading {
  const [now, setNow] = useState(() => Date.now());

  // The difference between this machine's clock and the server's, measured
  // when the payload arrived. A ref rather than state: it changes with the
  // payload, not with the tick, and putting it in state would re-render.
  const offset = useRef(0);
  const receivedAt = useRef(0);

  useEffect(() => {
    if (clock === null) return;
    const serverTime = Date.parse(clock.server_time);
    if (Number.isNaN(serverTime)) return;
    const arrived = Date.now();
    offset.current = serverTime - arrived;
    receivedAt.current = arrived;
    setNow(arrived);
  }, [clock]);

  useEffect(() => {
    if (!running || clock === null) return;
    const timer = setInterval(() => setNow(Date.now()), CLOCK_TICK_MS);
    return () => clearInterval(timer);
  }, [running, clock]);

  if (clock === null) {
    return { lightMs: 0, darkMs: 0, activeSide: null, awaitingServer: false };
  }

  const deadline = Date.parse(clock.deadline);
  // The server's idea of "now", derived from ours plus the measured offset.
  const serverNow = now + offset.current;
  const elapsed = running
    ? Math.max(0, serverNow - (Date.parse(clock.server_time) || serverNow))
    : 0;

  const remaining = (base: number, isActive: boolean): number =>
    Math.max(0, isActive ? base - elapsed : base);

  return {
    lightMs: remaining(clock.light_ms, clock.active_side === "light"),
    darkMs: remaining(clock.dark_ms, clock.active_side === "dark"),
    activeSide: clock.active_side,
    // Rendered as "waiting for the server", never as a result.
    awaitingServer: running && !Number.isNaN(deadline) && serverNow > deadline,
  };
}

/** `m:ss`, or `h:mm:ss` past an hour. Tabular digits are the caller's. */
export function formatClock(ms: number, locale: string): string {
  const total = Math.ceil(ms / 1000);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;

  const pad = new Intl.NumberFormat(locale, { minimumIntegerDigits: 2, useGrouping: false });
  const plain = new Intl.NumberFormat(locale, { useGrouping: false });

  return hours > 0
    ? `${plain.format(hours)}:${pad.format(minutes)}:${pad.format(seconds)}`
    : `${plain.format(minutes)}:${pad.format(seconds)}`;
}
