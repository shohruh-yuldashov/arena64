import { type Board, boardFrom } from "@/entities/board";
import type { CandidateMove } from "@/features/game/engine/moves";
import type {
  ClockPayload,
  MovePayload,
  ResultPayload,
  Side,
  SnapshotPayload,
} from "@/shared/realtime";

/**
 * The one authoritative game state — A64-020.5B §10, §11.
 *
 * ## One owner
 *
 * §10 forbids duplicating this across TanStack Query, context, component
 * state and a store. The owner is this reducer: the socket feeds it frames,
 * the page renders it, and nothing else holds a copy. Query is not involved
 * at all — a live game is not a cache, it is a stream with an authoritative
 * sequence, and `staleTime` has no meaning for it.
 *
 * ## What the server owns
 *
 * Everything that decides the game: legality, the board, the turn, capture
 * continuation, promotion, the clock, the result and the ply. This state
 * holds what the server last said plus **one** clearly-marked optimistic
 * value — `pending`, the move this client sent and has not heard back
 * about. Nothing else is guessed, and `sequence` never advances without a
 * server frame.
 *
 * ## Why `accepted` and `applied` are separate
 *
 * They are separate frames and they mean different things (§11). `accepted`
 * is correlated to a `request_id` and says *your* submission was the one
 * that landed; `applied` is the fan-out and carries the state change. The
 * submitter receives both. Treating them as one would leave a client unable
 * to tell its own move from its opponent's — which is exactly what the
 * board must know to decide whether to clear its pending state.
 */

/** A move this client has sent and not yet heard back about. */
export interface PendingMove {
  path: string[];
  /** For the diagnostic log only; never rendered. */
  requestId: string;
}

export type GamePhase =
  /** The route mounted; nothing has been asked for yet. */
  | "loading"
  /** `room.join` is in flight. */
  | "joining"
  /** A snapshot has been applied. Playable if it is our turn. */
  | "active"
  /** Our move is in flight. The board is not interactive. */
  | "submitting_move"
  /** The socket dropped. The board is frozen but still shown. */
  | "reconnecting"
  /** A gap was detected; a full snapshot has been asked for. */
  | "resyncing"
  /** The server reported a result. */
  | "completed"
  /** We could not join — not a participant, wrong match, not active. */
  | "unavailable"
  /** Something retrying cannot fix. */
  | "fatal";

export interface GameState {
  phase: GamePhase;
  matchId: string;
  /** Which side this client plays. `null` until the snapshot names us. */
  side: Side | null;
  board: Board;
  /** The authoritative ply. Never advanced without a server frame. */
  sequence: number;
  sideToMove: Side | null;
  fingerprint: string | null;
  participants: { light: string; dark: string } | null;
  clock: ClockPayload | null;
  result: ResultPayload | null;
  /** The squares of the move just played, for highlighting. */
  lastMove: { path: string[]; captured: string[] } | null;
  /** Every move so far, oldest first. Bounded by the game's own length. */
  history: { ply: number; path: string[]; captured: string[] }[];
  pending: PendingMove | null;
  /** A stable code for the last refusal, for the status line. */
  lastRejection: string | null;
  engineVersion: number | null;
}

export function initialState(matchId: string): GameState {
  return {
    phase: "loading",
    matchId,
    side: null,
    board: new Map(),
    sequence: -1,
    sideToMove: null,
    fingerprint: null,
    participants: null,
    clock: null,
    result: null,
    lastMove: null,
    history: [],
    pending: null,
    lastRejection: null,
    engineVersion: null,
  };
}

export type GameAction =
  | { type: "joining" }
  | { type: "snapshot"; payload: SnapshotPayload; viewerId: string }
  | { type: "applied"; payload: MovePayload }
  | { type: "rejected"; code: string }
  | { type: "submitting"; move: PendingMove }
  | { type: "resuming" }
  | { type: "resyncing" }
  | { type: "disconnected" }
  | { type: "unavailable"; code: string }
  | { type: "fatal"; code: string };

/**
 * The transitions, stated once.
 *
 * A reducer rather than scattered setters, because the interesting rules
 * here are *which* transitions exist: an `applied` frame from the past must
 * not roll the board backward, a duplicate must be idempotent, and a gap
 * must not be silently accepted. Those are three lines in one place, or
 * three bugs in three components.
 */
export function reduce(state: GameState, action: GameAction): GameState {
  switch (action.type) {
    case "joining":
      return { ...state, phase: "joining" };

    case "snapshot": {
      // §18: snapshot replacement is **authoritative**, never merged. A
      // client that reconciled a snapshot against what it already had would
      // be inventing a third state neither side agreed to.
      const snapshot = action.payload;
      const side =
        snapshot.participants.light === action.viewerId
          ? "light"
          : snapshot.participants.dark === action.viewerId
            ? "dark"
            : null;

      return {
        ...state,
        // A snapshot of a finished game resumes as finished — a client
        // reconnecting to a match that ended while it was away is a normal
        // outcome, not an error.
        phase: snapshot.result === null ? "active" : "completed",
        side,
        board: boardFrom(snapshot.pieces),
        sequence: snapshot.sequence,
        sideToMove: snapshot.side_to_move,
        fingerprint: snapshot.fingerprint,
        participants: snapshot.participants,
        clock: snapshot.clock,
        result: snapshot.result,
        engineVersion: snapshot.engine_version,
        // The pending move is discarded: the snapshot either contains it or
        // it never happened, and either way this client's copy is not
        // evidence (§18).
        pending: null,
        lastRejection: null,
        // History cannot be recovered from a snapshot — it carries a
        // position, not a past. Cleared rather than kept, because keeping a
        // list that no longer connects to the board would be a lie.
        history: [],
        lastMove: null,
      };
    }

    case "applied": {
      const move = action.payload;

      // §17: a duplicate is ignored idempotently and a stale frame never
      // rolls state backward. Both are ordinary on a reconnect, when the
      // buffer replays frames this client already has.
      if (move.ply <= state.sequence) return state;

      // A gap. The client is missing at least one move, so the board it
      // would render is not the board the server has. `resyncing` is the
      // honest state; the page asks for a snapshot.
      if (move.ply > state.sequence + 1) return { ...state, phase: "resyncing" };

      const board = applyToBoard(state.board, move);
      const completed = move.result != null;

      return {
        ...state,
        phase: completed ? "completed" : "active",
        board,
        sequence: move.ply,
        sideToMove: move.side_to_move,
        fingerprint: move.fingerprint,
        result: move.result ?? state.result,
        lastMove: { path: move.applied.path, captured: move.applied.captured },
        history: [
          ...state.history,
          { ply: move.ply, path: move.applied.path, captured: move.applied.captured },
        ],
        // Cleared whoever moved: if it was us the move landed, and if it was
        // the opponent then ours cannot still be outstanding on this ply.
        pending: null,
        lastRejection: null,
      };
    }

    case "rejected":
      // The board is untouched — it was never optimistically advanced, so
      // there is nothing to roll back. That is the whole benefit of not
      // advancing `sequence` locally.
      return { ...state, phase: "active", pending: null, lastRejection: action.code };

    case "submitting":
      return { ...state, phase: "submitting_move", pending: action.move, lastRejection: null };

    case "resuming":
      return { ...state, phase: "joining" };

    case "resyncing":
      return { ...state, phase: "resyncing", pending: null };

    case "disconnected":
      // The board stays on screen. A player watching their game vanish
      // because a proxy recycled a connection would reload, which costs a
      // snapshot; showing the last known position and a status line is both
      // truer and cheaper.
      return state.phase === "completed"
        ? state
        : { ...state, phase: "reconnecting", pending: null };

    case "unavailable":
      return { ...state, phase: "unavailable", lastRejection: action.code, pending: null };

    case "fatal":
      return { ...state, phase: "fatal", lastRejection: action.code, pending: null };
  }
}

/**
 * The board after a move the server confirmed.
 *
 * Applied from the **server's** `applied` payload — its path, its captures,
 * its promotion — rather than recomputed by the kernel. The kernel exists
 * to highlight squares; letting it decide what a confirmed move did would
 * make a client-side disagreement corrupt the board instead of merely
 * mis-highlighting it.
 */
function applyToBoard(board: Board, move: MovePayload): Board {
  const next = new Map(board);
  const from = move.applied.path[0];
  const to = move.applied.path[move.applied.path.length - 1];
  if (from === undefined || to === undefined) return board;

  const piece = next.get(from);
  if (piece === undefined) return board;

  next.delete(from);
  for (const square of move.applied.captured) next.delete(square);
  next.set(to, {
    square: to,
    side: piece.side,
    rank: move.applied.promoted_to ?? piece.rank,
  });
  return next;
}

/** Whether this client may pick up a piece right now. */
export function canInteract(state: GameState): boolean {
  return (
    state.phase === "active" &&
    state.side !== null &&
    state.sideToMove === state.side &&
    state.pending === null
  );
}

/** The moves available to this player, or none when it is not their turn. */
export function availableMoves(
  state: GameState,
  compute: (board: Board, side: Side) => CandidateMove[],
): CandidateMove[] {
  if (!canInteract(state) || state.side === null) return [];
  return compute(state.board, state.side);
}
