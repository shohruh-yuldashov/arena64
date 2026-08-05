import { useEffect, useRef, useState } from "react";

/**
 * Seconds left until an absolute instant — A64-020.5A §15.
 *
 * ## The server decides; this only renders
 *
 * The deadline is an ISO instant the API sent, and everything here is
 * arithmetic against the local clock. That distinction is load-bearing: a
 * client whose clock is two minutes fast will see this reach zero early,
 * and **nothing happens** when it does except a refetch. The server refuses
 * a late answer and expires the offer itself; this number is a courtesy.
 *
 * Which is why `onExpire` re-reads rather than transitioning. Fabricating
 * an expiry locally would mean a player with a skewed clock losing a match
 * they still had ten seconds to accept.
 *
 * ## Why the interval is not one second
 *
 * `setInterval(fn, 1000)` drifts — every tick is *at least* a second and
 * usually more, so a thirty-second countdown reliably renders thirty-one
 * ticks and ends late. Each tick is instead scheduled for the instant the
 * displayed number should next change, computed from the deadline itself,
 * so error does not accumulate and a backgrounded tab that missed ten
 * ticks resumes on the correct number rather than ten seconds behind.
 *
 * ## Announcements are not ticks
 *
 * `announceAt` is the small set of remaining-second values worth telling a
 * screen reader about. §15 and §23 both forbid announcing every second:
 * `aria-live` on a per-second counter is a reader that says a number thirty
 * times and drowns out everything else on the page.
 */
const ANNOUNCE_AT_SECONDS: readonly number[] = [30, 20, 10, 5];

export interface Countdown {
  /** Whole seconds remaining, floored at zero. */
  secondsLeft: number;
  /** The value to put in an `aria-live` region, or `null` for silence. */
  announcement: number | null;
}

export function useCountdown(deadline: string | null, onExpire?: () => void): Countdown {
  const [now, setNow] = useState(() => Date.now());
  const target = deadline === null ? null : Date.parse(deadline);
  const expired = useRef(false);

  useEffect(() => {
    expired.current = false;
  }, [deadline]);

  useEffect(() => {
    if (target === null) return;

    let timer: ReturnType<typeof setTimeout>;
    const tick = () => {
      const at = Date.now();
      setNow(at);

      const remaining = target - at;
      if (remaining <= 0) {
        // Once, not on every tick: `onExpire` refetches, and a refetch
        // every second past a deadline the server has not yet acted on is
        // a poll disguised as an expiry handler.
        if (!expired.current) {
          expired.current = true;
          onExpire?.();
        }
        return;
      }

      // Fire when the *displayed* number next changes, not a second from
      // now. `remaining % 1000` is however much of the current second is
      // left; scheduling for exactly that keeps the display in step with
      // the deadline however long a tick takes to run.
      timer = setTimeout(tick, remaining % 1000 || 1000);
    };

    tick();
    return () => clearTimeout(timer);
    // `onExpire` is deliberately absent: it is recreated on every render by
    // every caller, and depending on it would tear down and rebuild the
    // schedule constantly. The ref guard is what makes the stale closure
    // harmless — it fires at most once per deadline either way.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target]);

  if (target === null) return { secondsLeft: 0, announcement: null };

  const secondsLeft = Math.max(0, Math.ceil((target - now) / 1000));
  return {
    secondsLeft,
    announcement: ANNOUNCE_AT_SECONDS.includes(secondsLeft) ? secondsLeft : null,
  };
}
