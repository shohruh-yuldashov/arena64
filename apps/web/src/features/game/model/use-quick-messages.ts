import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  type GatewayErrorCode,
  type InboundFrame,
  isQuickMessage,
  isReady,
  type QuickMessage,
  type Side,
  useConnectionStatus,
  useFrames,
  useRealtime,
} from "@/shared/realtime";

/**
 * Quick messages for one live match — A64-023.2 §5 through §12, §17, §18.
 *
 * ## Deliberately not in the game reducer — §17
 *
 * `GameState` is the authoritative board, clock, ply and result. A quick
 * message touches none of them, and putting it there would mean every
 * arriving bubble and every expiry timer produces a reducer action on the
 * same state a move is applied to. This is separate state in a separate
 * hook, so a malformed frame or a render failure here **cannot** corrupt the
 * board, reset a clock, move the ply or trigger a resync — there is no code
 * path from this file to any of them.
 *
 * ## One bubble per participant, newest wins — §7
 *
 * Keyed by `Side`, so the map holds at most two entries: one for each seat.
 * A second message from the same participant **replaces** the first and
 * restarts its timer, which is the deterministic policy §7 asks for and the
 * reason there is no array — an array is how ten stacked bubbles happen.
 *
 * ## No optimistic render — §5
 *
 * Selecting an item sends a frame and shows nothing. The sender's own bubble
 * appears when the **server's** fan-out arrives, exactly as the opponent's
 * does, so one code path renders both and there is no window in which an
 * optimistic bubble and a server echo are both on screen. The server is
 * authoritative about whether the message was sent at all — a rate-limited
 * or terminal-match send produces no bubble, which is correct and which an
 * optimistic render would have got wrong.
 *
 * ## Nothing is replayed — §18
 *
 * Quick messages are ephemeral and the gateway never buffers them, so a
 * reconnect brings none back. This hook holds no history to restore and
 * builds none: bubbles live in component state that a remount starts empty.
 */

/** How long one bubble stays on screen — §7. */
export const QUICK_MESSAGE_TTL_MS = 4000;

/**
 * A local guard against a double-press producing two frames — §9.
 *
 * **UX only, never security.** The gateway's Redis limiter is the authority
 * and this cannot weaken it; what it prevents is a player's own double-click
 * spending two of their six-a-minute allowance on one intent.
 */
export const QUICK_MESSAGE_SEND_GUARD_MS = 600;

/** One bubble on screen. */
export interface VisibleQuickMessage {
  message: QuickMessage;
  /**
   * Distinguishes a *replacement* from the same message arriving twice.
   *
   * Without it, sending `nice_move` twice in a row would re-render an
   * identical object and React would keep the old element — so the timer
   * would not visibly restart. This is what makes "newest wins" observable.
   */
  key: number;
}

export interface QuickMessages {
  /** At most one per seat — §7. */
  visible: ReadonlyMap<Side, VisibleQuickMessage>;
  /** Whether the opponent's messages are currently suppressed — §11. */
  muted: boolean;
  toggleMute: () => void;
  /** Sends one catalogue member. Never throws. */
  send: (message: QuickMessage) => void;
  /** Whether a send is possible right now — §10, and the socket's state. */
  canSend: boolean;
  /**
   * The code of the last refused send, or `null`.
   *
   * Separate from the game reducer's `commandError` deliberately: a refused
   * quick message must not overwrite the reason a *draw offer* was refused,
   * and the two are rendered in different places.
   */
  error: GatewayErrorCode | null;
}

/**
 * The refusal codes this feature owns — §9.
 *
 * `game.command.rejected` is shared with resign and the draw commands, so a
 * refusal has to be attributed before it is rendered. These are the codes
 * only a quick message can produce; anything else on that frame belongs to
 * the command path and is left alone.
 *
 * `rate_limited` is deliberately **not** here: both paths can produce it,
 * and claiming it would let a rate-limited draw offer surface as a
 * quick-message error. It is attributed by `pending` instead — see `send`.
 */
const QUICK_MESSAGE_CODES = new Set<string>(["unknown_quick_message"]);

export function useQuickMessages({
  matchId,
  viewerSide,
  playable,
}: {
  matchId: string;
  /** Which seat this client holds. `null` for a spectator or before the snapshot. */
  viewerSide: Side | null;
  /** Whether the match is in a state that accepts messages — §10. */
  playable: boolean;
}): QuickMessages {
  const realtime = useRealtime();
  const status = useConnectionStatus();

  const [visible, setVisible] = useState<ReadonlyMap<Side, VisibleQuickMessage>>(new Map());
  const [muted, setMuted] = useState(false);
  const [error, setError] = useState<GatewayErrorCode | null>(null);

  // One expiry timer per seat. A ref rather than state: firing one must not
  // re-render, and clearing a replaced message's timer is bookkeeping the
  // view has no opinion about.
  const timers = useRef(new Map<Side, ReturnType<typeof setTimeout>>());
  // Whether a send is outstanding, so a shared `rate_limited` refusal can be
  // attributed to this feature rather than to the draw commands.
  const pending = useRef(false);
  const lastSentAt = useRef(0);
  // Read inside the frame handler, which is installed once — see
  // `use-game-room.ts` on why a value the handler needs fresh is a ref.
  const mutedRef = useRef(muted);
  mutedRef.current = muted;
  const sideRef = useRef(viewerSide);
  sideRef.current = viewerSide;

  const clearTimers = useCallback(() => {
    for (const timer of timers.current.values()) clearTimeout(timer);
    timers.current.clear();
  }, []);

  // Every timer dies with the component. Without this a bubble expiring
  // after unmount would set state on a surface that no longer exists —
  // and on a fast navigation away from a finished game, that is the
  // ordinary case rather than a rare one.
  useEffect(() => clearTimers, [clearTimers]);

  const show = useCallback((from: Side, message: QuickMessage) => {
    const existing = timers.current.get(from);
    // §7: the newer message replaces the older and the timer **restarts**.
    // Clearing first is what makes that true — otherwise the first
    // message's timer would expire and remove the second.
    if (existing !== undefined) clearTimeout(existing);

    timers.current.set(
      from,
      setTimeout(() => {
        timers.current.delete(from);
        setVisible((current) => {
          const next = new Map(current);
          next.delete(from);
          return next;
        });
      }, QUICK_MESSAGE_TTL_MS),
    );

    setVisible((current) => {
      const next = new Map(current);
      // `Date.now()` as the key: monotonic enough to force a remount, and
      // never compared against a server instant.
      next.set(from, { message, key: Date.now() });
      return next;
    });
  }, []);

  useFrames(
    useCallback(
      (frame: InboundFrame) => {
        if (frame.channel !== "game") return;

        if (frame.type === "game.quick_message.received") {
          const payload = frame.payload;
          // Validated before it is trusted. The transport hands over
          // `Record<string, unknown>`, and a `message` the catalogue does
          // not contain is **dropped rather than rendered** — §3's "unknown
          // values must never silently render arbitrary server text". This
          // is the last line of defence if a newer server gains an entry
          // this build does not know.
          if (payload.match_id !== matchId) return;
          if (!isQuickMessage(payload.message)) return;
          const from = payload.from;
          if (from !== "light" && from !== "dark") return;

          // §12: mute suppresses the **opponent's** presentation only. Own
          // messages still render through the server echo, and nothing is
          // queued — a muted message is dropped here and never replayed on
          // unmute, which is what "applies prospectively" means.
          if (mutedRef.current && from !== sideRef.current) return;

          show(from, payload.message);
          return;
        }

        if (frame.type === "game.command.rejected") {
          const code = rejectionCode(frame);
          if (code === null) return;
          // Ours if it is a quick-message-only code, or if a send of ours
          // is outstanding — see `QUICK_MESSAGE_CODES`.
          if (!QUICK_MESSAGE_CODES.has(code) && !pending.current) return;
          pending.current = false;
          setError(code as GatewayErrorCode);
        }
      },
      [matchId, show],
    ),
  );

  const canSend = playable && viewerSide !== null && isReady(status);

  const send = useCallback(
    (message: QuickMessage) => {
      if (!canSend) return;

      // §9: a double-press guard, for the player's own allowance. Not a
      // reimplementation of the server's window — it has no memory beyond
      // the last press and cannot refuse anything the server would allow.
      const now = Date.now();
      if (now - lastSentAt.current < QUICK_MESSAGE_SEND_GUARD_MS) return;
      lastSentAt.current = now;

      setError(null);
      pending.current = true;
      // `send`, not `request`: the gateway answers a successful quick
      // message with **nothing** — the sender learns it went out by
      // receiving the fan-out (A64-023.1 §4). Awaiting a correlated reply
      // would wait for a frame that never comes.
      realtime.send("game.quick_message.send", { match_id: matchId, message }, "game");
    },
    [canSend, matchId, realtime],
  );

  const toggleMute = useCallback(() => {
    setMuted((current) => {
      const next = !current;
      if (next) {
        // Clear the opponent's bubble on mute so the suppression is
        // immediate rather than beginning with the next message. Our own
        // stays: §12 says mute affects the opponent's presentation only.
        const ours = sideRef.current;
        for (const [side, timer] of timers.current) {
          if (side === ours) continue;
          clearTimeout(timer);
          timers.current.delete(side);
        }
        setVisible((visibleNow) => {
          const kept = new Map<Side, VisibleQuickMessage>();
          const own = ours === null ? undefined : visibleNow.get(ours);
          if (ours !== null && own !== undefined) kept.set(ours, own);
          return kept;
        });
      }
      return next;
    });
  }, []);

  // A finished match keeps whatever bubble is on screen until its own timer
  // runs out — §10 — but must not accept a new one. That is `canSend`'s job
  // above; nothing is cleared here deliberately.
  return useMemo(
    () => ({ visible, muted, toggleMute, send, canSend, error }),
    [visible, muted, toggleMute, send, canSend, error],
  );
}

/** The refusal code on a `game.command.rejected` frame, if it carries one. */
function rejectionCode(frame: InboundFrame): string | null {
  const code = frame.payload.code;
  return typeof code === "string" ? code : null;
}
