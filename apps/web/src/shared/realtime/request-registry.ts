import type { GatewayErrorCode, InboundFrame } from "@/shared/realtime/protocol";

/**
 * Correlating a sent frame with its answer — A64-020.5B §8.
 *
 * The gateway echoes `request_id` on the frame that answers a request, so
 * that field **is** the correlation token. §8 forbids inventing a second
 * one, and there is nothing a move-correlation identifier would need that
 * this does not already do.
 *
 * ## Every pending request has a deadline
 *
 * A move that stays pending forever is a board that never becomes
 * interactive again, and the player has no way to tell that from a slow
 * server. So every entry carries a timer, and a timeout **rejects** rather
 * than silently resolving — the caller then reconciles rather than assuming.
 *
 * §8 and §16 both forbid an automatic retry after an ambiguous timeout, and
 * this registry could not perform one: it does not hold the frame it sent.
 * Resubmitting is a decision only the layer that knows what the move was can
 * make, and the answer there is to resync instead.
 *
 * ## Everything is cleaned up on close
 *
 * A socket that drops takes every in-flight answer with it. Leaving the
 * entries would mean their timers firing minutes later against a game that
 * has moved on, so `rejectAll` is called from the transport's close path and
 * from sign-out.
 */
export class RealtimeError extends Error {
  readonly code: GatewayErrorCode | "timeout" | "disconnected";

  constructor(code: GatewayErrorCode | "timeout" | "disconnected", message: string) {
    super(message);
    this.name = "RealtimeError";
    this.code = code;
  }
}

interface Pending {
  resolve: (frame: InboundFrame) => void;
  reject: (error: RealtimeError) => void;
  timer: ReturnType<typeof setTimeout>;
}

/**
 * How long a request may stay unanswered.
 *
 * Ten seconds. A move is one database write and a fan-out on a healthy
 * server, so anything near this means the connection is gone rather than
 * busy — and the reconnect path is a better answer than a longer wait.
 */
export const REQUEST_TIMEOUT_MS = 10_000;

export class RequestRegistry {
  private readonly pending = new Map<string, Pending>();
  private counter = 0;

  /**
   * A fresh identifier.
   *
   * A counter and a random suffix rather than a UUID: the gateway bounds
   * the field at 64 characters and reflects it, the value has no meaning
   * beyond this connection, and two tabs sharing a random seed is not a
   * failure mode anybody has to reason about because the registry is
   * per-socket.
   */
  nextId(): string {
    this.counter += 1;
    return `r${this.counter}-${Math.random().toString(36).slice(2, 10)}`;
  }

  /** Registers `id` and resolves when a frame echoes it. */
  await(id: string, timeoutMs = REQUEST_TIMEOUT_MS): Promise<InboundFrame> {
    return new Promise<InboundFrame>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new RealtimeError("timeout", "The server did not answer in time."));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
    });
  }

  /**
   * Delivers a frame to whoever is waiting for it.
   *
   * Returns whether anybody was — the caller uses that to decide if the
   * frame is *also* a broadcast worth handling. `game.move.applied` carries
   * no `request_id` and always returns `false`; `game.move.accepted` always
   * returns `true` for the submitter and never reaches the opponent.
   */
  settle(frame: InboundFrame): boolean {
    const id = frame.request_id;
    if (id === null) return false;
    const entry = this.pending.get(id);
    if (entry === undefined) return false;

    this.pending.delete(id);
    clearTimeout(entry.timer);

    // An `error` frame carrying our `request_id` is this request failing,
    // not a connection-level problem. Rejecting here is what lets a caller
    // `await` a request and handle its refusal in one place.
    if (
      frame.type === "error" ||
      frame.type === "game.move.rejected" ||
      // A64-020.5C §12. A refused participant command must reject the
      // promise its caller is awaiting, exactly as a refused move does —
      // otherwise `game.command.rejected` would resolve as a success and a
      // resign button would go back to idle looking like it worked.
      frame.type === "game.command.rejected"
    ) {
      const code = frame.payload.code;
      entry.reject(
        new RealtimeError(
          typeof code === "string" ? (code as GatewayErrorCode) : "internal_error",
          "The server refused the request.",
        ),
      );
      return true;
    }

    entry.resolve(frame);
    return true;
  }

  /** Fails everything in flight. Called on close and on sign-out. */
  rejectAll(reason: RealtimeError): void {
    for (const entry of this.pending.values()) {
      clearTimeout(entry.timer);
      entry.reject(reason);
    }
    this.pending.clear();
  }

  get size(): number {
    return this.pending.size;
  }
}
