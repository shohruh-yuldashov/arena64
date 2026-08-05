import { api } from "@/shared/api";
import { reportError } from "@/shared/lib/report-error";
import type { ConnectionStatus } from "@/shared/realtime/connection-state";
import {
  type Channel,
  encodeFrame,
  type InboundFrame,
  type OutboundType,
  parseFrame,
  PROTOCOL_VERSION,
} from "@/shared/realtime/protocol";
import {
  RECONNECT_CEILING_MS,
  reconnectDelay,
  STABLE_AFTER_MS,
} from "@/shared/realtime/reconnect-policy";
import { RealtimeError, RequestRegistry } from "@/shared/realtime/request-registry";

/**
 * The one socket — AD-11, A64-020.5B §3, §4.
 *
 * One authenticated connection per tab, multiplexed by channel. Not one per
 * match, not a second one for presence: browsers cap concurrent connections
 * per origin, mobile clients pay a battery cost per socket, and — the
 * reason that actually decides it — separate sockets make cross-stream
 * ordering undefined. A resignation and a chat message sent in that order
 * must arrive in that order.
 *
 * ## Deliberately not a React thing
 *
 * A plain class with a subscription API, owned by a provider that mounts
 * once. §3 forbids putting socket state in TanStack Query and forbids
 * putting the instance in a context value that re-renders broadly, and both
 * for the same reason: the transport's callbacks fire on the network's
 * schedule, and anything that turns each one into a React state update
 * re-renders a chess board sixty times a game for no reason.
 *
 * Subscribers get frames. What to do with them is the game feature's.
 *
 * ## The ticket is never stored
 *
 * `POST /auth/ws-ticket` mints a single-use, seconds-lived credential; it
 * goes straight into the URL of one connection attempt and is never written
 * to a variable that outlives that attempt, never logged, and never put in
 * storage. **Every reconnect mints a fresh one** — a spent ticket is not
 * refreshed, it is replaced, and reusing one would fail in exactly the way
 * that looks like a server bug.
 */

/** What a subscriber receives. */
export type FrameListener = (frame: InboundFrame) => void;
export type StatusListener = (status: ConnectionStatus) => void;

interface TicketResponse {
  ticket: string;
  expires_at: string;
}

/**
 * Where `/ws` lives.
 *
 * Same origin, always — §4. The page and the API share an origin
 * (`specs/frontend.md` §11) and the gateway is mounted at the application
 * root rather than under `/api/v1`, so this is `/ws` on the current host
 * with the scheme upgraded. Nothing here names a host: a build pointed at a
 * separate API would not have a working session anyway.
 */
function socketUrl(ticket: string): string {
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${window.location.host}/ws?ticket=${encodeURIComponent(ticket)}`;
}

export class RealtimeClient {
  private socket: WebSocket | null = null;
  private status: ConnectionStatus = "idle";
  private attempt = 0;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private stableTimer: ReturnType<typeof setTimeout> | null = null;
  /** Set by `stop()`. Nothing reconnects while it stands. */
  private stopped = true;

  private readonly frameListeners = new Set<FrameListener>();
  private readonly statusListeners = new Set<StatusListener>();
  readonly requests = new RequestRegistry();

  /** Last time the socket reached `ready`. Diagnostic; §5. */
  lastReadyAt: number | null = null;

  // --- lifecycle ---------------------------------------------------------

  /**
   * Starts, or does nothing if already running.
   *
   * Idempotent on purpose: the provider calls it whenever the session
   * becomes authenticated, and React may run that effect more than once.
   */
  start(): void {
    if (!this.stopped) return;
    this.stopped = false;
    this.attempt = 0;
    void this.connect();
  }

  /**
   * Stops for good, until `start` is called again.
   *
   * Called from `onSessionEnded`, so signing out closes the socket rather
   * than leaving an authenticated connection open for a session that no
   * longer exists. `closed`, not `fatal`: nothing is wrong.
   */
  stop(): void {
    this.stopped = true;
    this.clearTimers();
    this.requests.rejectAll(new RealtimeError("disconnected", "The session ended."));
    this.teardownSocket();
    this.setStatus("closed");
  }

  // --- subscription ------------------------------------------------------

  onFrame(listener: FrameListener): () => void {
    this.frameListeners.add(listener);
    return () => this.frameListeners.delete(listener);
  }

  onStatus(listener: StatusListener): () => void {
    this.statusListeners.add(listener);
    // Immediately, so a late subscriber is not left guessing until the next
    // transition — which on a healthy connection may be never.
    listener(this.status);
    return () => this.statusListeners.delete(listener);
  }

  get currentStatus(): ConnectionStatus {
    return this.status;
  }

  // --- sending -----------------------------------------------------------

  /**
   * Sends a frame and does not wait.
   *
   * Silently drops when the socket is not ready, which is the honest
   * behaviour for a fire-and-forget frame: a `ping` that could not be sent
   * is not worth an error, and the heartbeat's absence is already visible
   * as a closed socket.
   */
  send(type: OutboundType, payload: Record<string, unknown>, channel: Channel): void {
    if (this.socket === null || this.status !== "ready") return;
    this.socket.send(encodeFrame({ v: PROTOCOL_VERSION, type, channel, payload }));
  }

  /**
   * Sends a frame and waits for the answer that echoes its `request_id`.
   *
   * Rejects with a `RealtimeError` on refusal, timeout or disconnection —
   * three outcomes a caller genuinely handles differently, which is why they
   * carry distinct codes rather than one "it failed".
   */
  async request(
    type: OutboundType,
    payload: Record<string, unknown>,
    channel: Channel,
  ): Promise<InboundFrame> {
    if (this.socket === null || this.status !== "ready") {
      throw new RealtimeError("disconnected", "The connection is not ready.");
    }
    const requestId = this.requests.nextId();
    const waiting = this.requests.await(requestId);
    this.socket.send(
      encodeFrame({ v: PROTOCOL_VERSION, type, request_id: requestId, channel, payload }),
    );
    return waiting;
  }

  // --- the connection itself ---------------------------------------------

  private async connect(): Promise<void> {
    if (this.stopped) return;

    if (typeof navigator !== "undefined" && navigator.onLine === false) {
      // §6: pause rather than back off against a machine with no network.
      // `online` restarts us — see `RealtimeProvider`.
      this.setStatus("offline");
      return;
    }

    this.setStatus(this.attempt === 0 ? "ticketing" : "reconnecting");

    let ticket: string;
    try {
      // Minted per attempt and never held: the value below is the only
      // reference, and it goes out of scope with this function.
      ticket = (await api.post<TicketResponse>("/auth/ws-ticket")).ticket;
    } catch (error) {
      // A `401` here means the access token is gone, which the HTTP client's
      // refresh interceptor has already tried to fix. Retrying is still
      // right — the session may come back — and `fatal` is reserved for the
      // gateway refusing a ticket it was given.
      this.scheduleRetry(error);
      return;
    }

    if (this.stopped) return;
    this.setStatus("connecting");
    this.openSocket(ticket);
  }

  private openSocket(ticket: string): void {
    let socket: WebSocket;
    try {
      socket = new WebSocket(socketUrl(ticket));
    } catch (error) {
      this.scheduleRetry(error);
      return;
    }
    this.socket = socket;

    socket.onmessage = (event) => {
      if (typeof event.data !== "string") return;
      const frame = parseFrame(event.data);
      // §7: an unknown or malformed frame is ignored, never thrown. There is
      // no boundary above a transport callback.
      if (frame === null) return;
      this.receive(frame);
    };

    socket.onclose = (event) => {
      this.teardownSocket();
      this.requests.rejectAll(new RealtimeError("disconnected", "The connection closed."));
      if (this.stopped) return;

      // 1008 is the gateway's refusal — a spent, expired or forged ticket.
      // Retrying with a *fresh* ticket is correct for expiry and pointless
      // for a session that is gone, and the two are indistinguishable here;
      // so this retries, and the ticket request is what eventually fails
      // permanently if the session really has ended.
      this.scheduleRetry(new RealtimeError("invalid_ticket", `closed (${event.code})`));
    };

    socket.onerror = () => {
      // Deliberately empty. `onerror` carries nothing actionable in browsers
      // and is always followed by `onclose`, which is where the decision is.
    };
  }

  private receive(frame: InboundFrame): void {
    if (frame.type === "connection.ready") {
      this.setStatus("ready");
      this.lastReadyAt = Date.now();
      // §6: forgiven only after the connection *holds*. A socket that
      // authenticates and dies a second later has not recovered.
      this.stableTimer = setTimeout(() => {
        this.attempt = 0;
      }, STABLE_AFTER_MS);
    }

    // Answer a waiting request first; a settled frame is still delivered to
    // subscribers, because `game.move.accepted` both resolves the submitter's
    // promise and is a state change the game feature applies.
    this.requests.settle(frame);

    for (const listener of this.frameListeners) {
      try {
        listener(frame);
      } catch (error) {
        // One subscriber must not stop the others, and must not kill the
        // socket — CLAUDE.md §9.2 applied to fan-out.
        reportError(error, { scope: "realtime-listener", frameType: frame.type });
      }
    }
  }

  private scheduleRetry(cause: unknown): void {
    if (this.stopped) return;
    this.attempt += 1;
    const delay = reconnectDelay(this.attempt);
    this.setStatus("reconnecting");
    reportError(cause, {
      scope: "realtime-reconnect",
      attempt: this.attempt,
      delayMs: delay,
    });
    this.retryTimer = setTimeout(
      () => void this.connect(),
      Math.min(delay, RECONNECT_CEILING_MS),
    );
  }

  /** Called by the provider when the browser comes back online. */
  resumeFromOffline(): void {
    if (this.stopped || this.status !== "offline") return;
    this.attempt = 0;
    void this.connect();
  }

  private teardownSocket(): void {
    const socket = this.socket;
    this.socket = null;
    if (socket === null) return;
    socket.onmessage = null;
    socket.onclose = null;
    socket.onerror = null;
    if (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING) {
      socket.close();
    }
  }

  private clearTimers(): void {
    if (this.retryTimer !== null) clearTimeout(this.retryTimer);
    if (this.stableTimer !== null) clearTimeout(this.stableTimer);
    this.retryTimer = null;
    this.stableTimer = null;
  }

  private setStatus(status: ConnectionStatus): void {
    if (this.status === status) return;
    this.status = status;
    for (const listener of this.statusListeners) listener(status);
  }
}
