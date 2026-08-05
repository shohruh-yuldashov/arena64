/**
 * The gateway wire protocol, as this client speaks it — A64-020.5B §7.
 *
 * ## Why this file is hand-written, and what keeps it honest
 *
 * Everything else this app sends is generated from the OpenAPI document.
 * The WebSocket protocol has no machine-readable schema — it is Python
 * `StrEnum`s and `dict[str, Any]` payloads in `app/gateway/protocol.py` —
 * so this is a **hand-maintained contract**, and it is isolated in one file
 * for exactly that reason: when the gateway changes, there is one place to
 * change and a reviewer can diff it against the Python.
 *
 * The rule that keeps it from rotting is that **nothing outside this
 * directory constructs a frame**. Components call the client's methods;
 * the client is the only thing that knows what a frame looks like.
 *
 * ## Every field here exists on the server
 *
 * Cross-checked against `app/gateway/protocol.py` at A64-020.5B. Where the
 * server sends a field this client does not read, it is omitted rather than
 * typed as `unknown` — a type that claims to describe a payload it has not
 * verified is worse than one that describes a subset and says so.
 */

/** `app/gateway/protocol.py` — bumped only on an incompatible reshape. */
export const PROTOCOL_VERSION = 1;

/**
 * Which logical stream a frame belongs to — AD-11.
 *
 * One socket, multiplexed. Not one socket per match: the channel is a
 * *kind* of traffic and the match is in the payload, which is what keeps
 * the set bounded.
 */
export type Channel = "system" | "matchmaking" | "game";

/** Everything the gateway can send us. */
export type InboundType =
  | "connection.ready"
  | "pong"
  | "room.joined"
  | "room.left"
  | "game.move.accepted"
  | "game.move.rejected"
  | "game.move.applied"
  | "game.snapshot"
  | "game.events"
  | "game.resumed"
  | "game.resync_required"
  | "error";

/** Everything this client sends. Spectator frames are deferred. */
export type OutboundType =
  "ping" | "room.join" | "room.leave" | "game.move.submit" | "game.resume";

/**
 * The stable refusal codes — `GatewayErrorCode`.
 *
 * A closed union rather than `string`, so a branch cannot misspell one and
 * a code the gateway removes becomes a compile error rather than a branch
 * that silently stops matching. The two spectator codes are included even
 * though this phase never joins as a spectator: they are part of the
 * contract, and omitting them would make an exhaustive `switch` lie.
 */
export type GatewayErrorCode =
  | "invalid_ticket"
  | "malformed_message"
  | "not_a_participant"
  | "room_unavailable"
  | "not_in_room"
  | "not_your_turn"
  | "illegal_move"
  | "stale_state"
  | "match_not_active"
  | "not_spectatable"
  | "spectating_forbidden"
  | "clock_expired"
  | "rate_limited"
  | "internal_error";

/** `light` moves first. The engine's `PlayerSide`. */
export type Side = "light" | "dark";

/** `man` or `king`. The engine's rank vocabulary. */
export type Rank = "man" | "king";

/** One piece on one square — `game.public.PlacedPiece`. Three strings. */
export interface PlacedPiece {
  /** Algebraic, e.g. `"c3"`. The server's coordinate, never re-derived. */
  square: string;
  side: Side;
  rank: Rank;
}

/**
 * The authoritative clock — **absolute instants, never durations**.
 *
 * `deadline` is when the active side flags and `server_time` is when the
 * server built the reading. A client corrects its own offset from the
 * second and counts down to the first; a duration re-based on receipt would
 * drift by exactly the amount it was meant to describe.
 */
export interface ClockPayload {
  light_ms: number;
  dark_ms: number;
  active_side: Side;
  /** ISO-8601. */
  deadline: string;
  /** ISO-8601. */
  server_time: string;
}

export interface ResultPayload {
  outcome: string;
  termination_reason: string;
  winner: Side | null;
}

/**
 * The synchronisation baseline — `game.snapshot`.
 *
 * A client that applies this and then every later frame in order is exactly
 * in step. **Replacement is authoritative**: a snapshot is never merged
 * into what the client already had.
 *
 * Carries no player handles and no ratings — those are `users`' and
 * `rating`'s, and the gateway deliberately does not reach for them.
 */
export interface SnapshotPayload {
  match_id: string;
  engine_version: number;
  variant: string;
  /** `MatchRecordStatus` — `active`, `completed`, … */
  status: string;
  /** The ply. The one monotonic counter; there is no second sequence. */
  sequence: number;
  side_to_move: Side;
  fingerprint: string;
  pieces: PlacedPiece[];
  participants: { light: string; dark: string };
  clock: ClockPayload | null;
  result: ResultPayload | null;
  /** ISO-8601. When the server built this. */
  server_time: string;
}

/** What the engine determined the move actually was. */
export interface AppliedMove {
  /** Every square the piece occupied, in order. Two or more. */
  path: string[];
  /** The squares whose pieces were taken. Server-derived. */
  captured: string[];
  promoted_to: Rank | null;
}

/**
 * `game.move.accepted` and `game.move.applied` share this shape.
 *
 * They are **not** merged, and the distinction is load-bearing: `accepted`
 * is correlated to a `request_id` and answers "your submission was the one
 * that produced this"; `applied` is the fan-out to both players and carries
 * no correlation. A client that read only the broadcast could not tell
 * whose move it was.
 *
 * Carries a **fingerprint rather than a board**: the client already has the
 * position and needs confirmation plus a way to detect divergence.
 */
export interface MovePayload {
  match_id: string;
  ply: number;
  side_to_move: Side;
  fingerprint: string;
  applied: AppliedMove;
  /** Present only when this move ended the game. */
  result?: ResultPayload;
}

/** `game.move.rejected` — about the *move*, not the frame. */
export interface MoveRejectedPayload {
  code: GatewayErrorCode;
  /** Server prose. Logged, never rendered — §24 forbids branching on it. */
  reason: string;
}

export interface RoomJoinedPayload {
  match_id: string;
  participants: string[];
  both_connected: boolean;
}

export interface EventsPayload {
  match_id: string;
  /** Encoded frames, in order. Each is a JSON string of an inbound frame. */
  frames: string[];
}

export interface ResumedPayload {
  match_id: string;
  sequence: number;
  both_connected: boolean;
}

export interface ErrorPayload {
  code: GatewayErrorCode;
}

/**
 * A frame off the wire.
 *
 * The payload is deliberately typed per `type` at the point of use rather
 * than as a discriminated union here. The gateway's payloads are
 * `dict[str, Any]` and this client validates the fields it reads — a union
 * would claim a guarantee the transport does not give, and one unexpected
 * field would then be a type error rather than an ignorable frame.
 */
export interface InboundFrame {
  v: number;
  type: InboundType;
  request_id: string | null;
  channel: Channel;
  payload: Record<string, unknown>;
}

export interface OutboundFrame {
  v: number;
  type: OutboundType;
  request_id?: string;
  channel: Channel;
  payload: Record<string, unknown>;
}

const INBOUND_TYPES = new Set<string>([
  "connection.ready",
  "pong",
  "room.joined",
  "room.left",
  "game.move.accepted",
  "game.move.rejected",
  "game.move.applied",
  "game.snapshot",
  "game.events",
  "game.resumed",
  "game.resync_required",
  "error",
]);

/**
 * One received string as a frame, or `null`.
 *
 * **Never throws.** §7: one unsupported event must not take the app down,
 * and a transport callback is the worst place to discover an exception —
 * there is no boundary above it. A frame this build does not understand is
 * indistinguishable from garbage as far as behaviour goes: both are
 * ignored, and both are counted.
 *
 * A frame with a version this client does not speak is refused rather than
 * best-guessed. The gateway bumps `v` only when a shape changes
 * incompatibly, so "I do not speak v2" is exactly the right answer.
 */
export function parseFrame(raw: string): InboundFrame | null {
  let candidate: unknown;
  try {
    candidate = JSON.parse(raw);
  } catch {
    return null;
  }

  if (typeof candidate !== "object" || candidate === null) return null;
  const frame = candidate as Record<string, unknown>;

  if (frame.v !== PROTOCOL_VERSION) return null;
  if (typeof frame.type !== "string" || !INBOUND_TYPES.has(frame.type)) return null;
  if (typeof frame.payload !== "object" || frame.payload === null) return null;

  const channel = frame.channel;
  return {
    v: PROTOCOL_VERSION,
    type: frame.type as InboundType,
    request_id: typeof frame.request_id === "string" ? frame.request_id : null,
    // The gateway defaults an absent channel to `system`, and so does this.
    channel: channel === "game" || channel === "matchmaking" ? channel : "system",
    payload: frame.payload as Record<string, unknown>,
  };
}

/** A frame to send, as the gateway's decoder expects it. */
export function encodeFrame(frame: OutboundFrame): string {
  return JSON.stringify(frame);
}
