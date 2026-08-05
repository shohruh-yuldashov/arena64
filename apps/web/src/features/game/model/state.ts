import { type Board, boardFrom } from "@/entities/board";
import type { CandidateMove } from "@/features/game/engine/moves";
import type {
  ClockPayload,
  DrawDeclinedPayload,
  DrawOffer,
  DrawOfferedPayload,
  DrawStatePayload,
  GameCompletedPayload,
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

/**
 * Which participant command is in flight — A64-020.5C §3.
 *
 * One at a time, and a single value rather than a set: the four are mutually
 * exclusive in the UI (a player answering an offer is not simultaneously
 * resigning), so a set would model a state the buttons cannot produce and
 * every reader would have to decide what it means.
 */
export type ActiveCommand = "resign" | "offer" | "accept" | "decline";

/**
 * The draw agreement, as this client holds it — §3.
 *
 * **Server-owned, without exception.** The three booleans arrive resolved
 * for this viewer and are never recomputed: §2 forbids inferring permission
 * from the ply or from local move history, and the spam rule that decides
 * `mayOffer` lives in `game.domain.draw_agreement` where it can be enforced.
 *
 * `null` for `offer` means nothing stands. The booleans still matter then —
 * `mayOffer` is false for a player whose last offer was resolved and whose
 * opponent has not moved since.
 */
export interface DrawAgreementState {
  offer: DrawOffer | null;
  mayOffer: boolean;
  mayAccept: boolean;
  mayDecline: boolean;
}

const NO_DRAW_AGREEMENT: DrawAgreementState = {
  offer: null,
  mayOffer: false,
  mayAccept: false,
  mayDecline: false,
};

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

  /**
   * The draw agreement — §3. Never a second store, and never duplicated into
   * component state: a control that held its own copy would render the
   * previous answer for one frame after every authoritative update.
   */
  draw: DrawAgreementState;

  /** The command awaiting an authoritative answer, or `null`. */
  activeCommand: ActiveCommand | null;

  /**
   * The code of the last refused command.
   *
   * Separate from `lastRejection`, which is the *move* path's. Collapsing
   * them would let a refused draw offer overwrite the reason a move was
   * rejected, and the two are shown in different places.
   */
  commandError: string | null;
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
    draw: NO_DRAW_AGREEMENT,
    activeCommand: null,
    commandError: null,
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
  | { type: "fatal"; code: string }
  // --- participant commands — A64-020.5C §3 ---------------------------
  | { type: "command_sent"; command: ActiveCommand }
  | { type: "command_rejected"; code: string }
  | { type: "draw_offered"; payload: DrawOfferedPayload; viewerSide: Side | null }
  | { type: "draw_declined"; payload: DrawDeclinedPayload }
  | { type: "completed"; payload: GameCompletedPayload }
  | { type: "draw_state"; payload: DrawStatePayload };

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
        // §11: a full snapshot **replaces** the control state too, so a
        // reload restores an offer the server still holds and drops one it
        // does not. `draw` is absent from a spectator's snapshot, which
        // reads as "no agreement" rather than as a parse failure.
        draw: drawFrom(snapshot),
        activeCommand: null,
        commandError: null,
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
        // §10: the backend clears an offer when its **recipient** applies a
        // legal move, inside that move's own transaction. Mirrored here so
        // the indicator disappears with the move rather than one round trip
        // later — and only for the recipient's move, because the offerer
        // playing on leaves their own offer standing.
        //
        // This is a *reflection* of an authoritative change, not a local
        // rule: `game.move.applied` only arrives for a move the server
        // applied, so there is no ply arithmetic here and none is wanted.
        draw: clearedByMove(state.draw, move.side_to_move),
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

    // --- participant commands — A64-020.5C §3, §5, §6, §8, §9 ----------

    case "command_sent":
      // The command is in flight and nothing else changes. §2 and §5: the
      // match is **not** marked completed locally, not even for a
      // resignation this player just confirmed — the result is whatever
      // `game.completed` says, and a resignation can race another terminal
      // event and lose.
      return { ...state, activeCommand: action.command, commandError: null };

    case "command_rejected":
      return { ...state, activeCommand: null, commandError: action.code };

    case "draw_offered": {
      // The authoritative offer, from the event payload rather than from
      // what this client sent (§6). The offerer and the recipient both
      // receive this frame, and the permissions follow from who made it —
      // the offerer may do none of the three until it resolves.
      const offer: DrawOffer = {
        offered_by: action.payload.offered_by,
        offered_at_ply: action.payload.offered_at_ply,
        offered_at: action.payload.offered_at,
      };
      const isRecipient =
        action.viewerSide !== null && action.payload.offered_by !== action.viewerSide;

      return {
        ...state,
        activeCommand: null,
        commandError: null,
        draw: {
          offer,
          // Nobody may open a new offer while one stands — the server
          // refuses it as `draw_offer_already_pending`, and showing the
          // button would be showing one that cannot work.
          mayOffer: false,
          mayAccept: isRecipient,
          mayDecline: isRecipient,
        },
      };
    }

    case "draw_declined":
      // §9: a decline changes no board, no clock, no turn and no ply.
      // Nothing here touches any of them.
      //
      // `mayOffer` stays **false** for both sides rather than being guessed.
      // The declining player could offer, and the declined one could not
      // until their opponent moves — but which of those this client is
      // depends on a rule the server owns (§2), so the honest answer is to
      // enable nothing until the next authoritative statement says
      // otherwise. `game.move.applied` triggers a snapshot-free re-read on
      // the next resume; until then a disabled button is wrong-but-safe and
      // an enabled one is a request the server refuses.
      return {
        ...state,
        activeCommand: null,
        commandError: null,
        draw: NO_DRAW_AGREEMENT,
      };

    case "draw_state":
      // A64-020.5D §15. **Replaces** the agreement authoritatively and
      // recomputes nothing: the three booleans arrive resolved for this
      // viewer, and §2 forbids deriving them.
      //
      // Touches neither the board, the clock, the turn, the sequence nor
      // the move in flight — which is what makes it safe to apply whenever
      // it arrives relative to the move that caused it (§12).
      return {
        ...state,
        draw: {
          offer: action.payload.offer,
          mayOffer: action.payload.may_offer,
          mayAccept: action.payload.may_accept,
          mayDecline: action.payload.may_decline,
        },
      };

    case "completed":
      // §5, §8, §15. The authoritative result wins, whatever command this
      // client happened to send. A resignation racing an accepted draw ends
      // as whichever the server settled, and this is the only place the
      // phase becomes `completed` for a command.
      return {
        ...state,
        phase: "completed",
        result: action.payload.result,
        activeCommand: null,
        commandError: null,
        pending: null,
        draw: NO_DRAW_AGREEMENT,
      };
  }
}

/**
 * The draw agreement a snapshot carries — §11.
 *
 * Absent for a spectator, and absent from every snapshot a server built
 * before A64-020.5C-pre. Both read as "no agreement", which is correct in
 * each case and needs no version negotiation.
 */
function drawFrom(snapshot: SnapshotPayload): DrawAgreementState {
  const draw = snapshot.draw;
  if (draw === undefined) return NO_DRAW_AGREEMENT;
  return {
    offer: draw.offer,
    mayOffer: draw.may_offer,
    mayAccept: draw.may_accept,
    mayDecline: draw.may_decline,
  };
}

/**
 * The agreement after a move the server applied — §10.
 *
 * `sideToMove` on an applied frame is whose turn it is **now**, so the side
 * that just moved is its opposite. An offer survives its own offerer's move
 * and dies on its recipient's.
 */
function clearedByMove(draw: DrawAgreementState, sideToMove: Side): DrawAgreementState {
  if (draw.offer === null) return draw;
  const moved: Side = sideToMove === "light" ? "dark" : "light";
  return draw.offer.offered_by === moved ? draw : NO_DRAW_AGREEMENT;
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
