import { useEffect, useState } from "react";

/**
 * How long a challenge has left, coarsely — A64-022.5 §3, §10, §16.
 *
 * ## Not `useCountdown`, and the difference is the timescale
 *
 * `matchmaking`'s countdown renders whole seconds against a **thirty-second**
 * acceptance window, where every second is a decision. A challenge lives for
 * **twenty-four hours**, and a per-second re-render of a number nobody is
 * watching is eighty-six thousand renders to say "tomorrow".
 *
 * So this returns a *bucket* — hours, or minutes under an hour — and
 * re-renders only when the bucket can have changed. A row showing "23h"
 * updates once an hour; one showing "4m" updates once a minute.
 *
 * ## The server decides; this only renders — §10
 *
 * Every number here is arithmetic against the **local** clock, so a device
 * two minutes fast reaches zero early. Nothing happens when it does: the
 * row renders "expired" and stops offering an action, and the list is
 * corrected by the next read. There is no local timer that removes a row,
 * cancels a challenge, or decides anything — a client clock that could
 * expire an invitation would be a second authority over a rule the backend
 * re-checks inside the transaction anyway.
 *
 * `expired` is therefore a *display* state. The Accept button is disabled
 * on it as a courtesy; the server is what refuses the answer.
 */
export interface Expiry {
  /** Whole minutes remaining, floored at zero. */
  minutesLeft: number;
  /** Whether the local clock has passed the deadline. Display only. */
  isExpired: boolean;
}

const MINUTE_MS = 60_000;

export function useExpiry(deadline: string): Expiry {
  const target = Date.parse(deadline);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (Number.isNaN(target)) return;

    let timer: ReturnType<typeof setTimeout>;

    // Each tick is scheduled for the instant the displayed minute should
    // next change, computed from the deadline itself, so error does not
    // accumulate and a backgrounded tab resumes on the correct number
    // rather than however many ticks behind it missed.
    const schedule = () => {
      const current = Date.now();
      setNow(current);
      if (current >= target) return;
      const untilNextMinuteBoundary = (target - current) % MINUTE_MS || MINUTE_MS;
      timer = setTimeout(schedule, untilNextMinuteBoundary);
    };

    schedule();
    return () => clearTimeout(timer);
  }, [target]);

  if (Number.isNaN(target)) return { minutesLeft: 0, isExpired: false };

  const remaining = target - now;
  return {
    minutesLeft: Math.max(0, Math.floor(remaining / MINUTE_MS)),
    isExpired: remaining <= 0,
  };
}
