import { useCallback, useEffect, useReducer, useRef } from "react";

import { isAuthenticated } from "@/entities/session";
import { useSession } from "@/features/auth/model/session-provider";
import {
  type ActiveCommand,
  type GameState,
  initialState,
  reduce,
} from "@/features/game/model/state";
import { reportError } from "@/shared/lib/report-error";
import {
  type DrawDeclinedPayload,
  type DrawOfferedPayload,
  type DrawStatePayload,
  type GameCommandType,
  type GameCompletedPayload,
  type GatewayErrorCode,
  type InboundFrame,
  isReady,
  type MovePayload,
  RealtimeError,
  type SnapshotPayload,
  useConnectionStatus,
  useFrames,
  useRealtime,
} from "@/shared/realtime";

/**
 * One live match, from joining to leaving — A64-020.5B §9, §16, §18.
 *
 * The page mounts this and renders what it returns. Everything about the
 * protocol is here; the components below know about a board and a clock.
 *
 * ## The join is a request, not a route
 *
 * §9: a URL does not grant room access. `room.join` is sent and *answered*,
 * and the room is only active once the gateway confirms it — a player who
 * types somebody else's match id gets `not_a_participant` and an
 * `unavailable` screen, which is the same answer the backend gives every
 * other surface.
 *
 * ## Resume is the reconnect path, and join is only the first time
 *
 * After the first snapshot this client knows a sequence, so a reconnect
 * sends `game.resume` with it rather than starting over. The gateway
 * decides what comes back: the frames we missed if it can prove it holds
 * them all, a fresh snapshot if it cannot, or `game.resync_required` if the
 * gap is unprovable. §18 forbids merging heuristically and nothing here
 * does — a snapshot replaces.
 *
 * ## Why the frame handler is a ref
 *
 * It closes over the current state, and the socket outlives every render.
 * Holding it in a ref means the subscription is installed once and always
 * sees fresh state, rather than being torn down and rebuilt between a frame
 * arriving and being handled.
 */
export interface GameRoom {
  state: GameState;
  /** Sends a move and waits for the acknowledgement. Never throws. */
  submit: (path: string[]) => Promise<void>;
  /** Re-asks for a full snapshot. The recovery `resyncing` offers. */
  resync: () => void;
  /**
   * Sends one participant command and waits for its authoritative answer —
   * A64-020.5C §12. Never throws.
   *
   * On the same hook rather than a hook of its own, because a command and a
   * move contend for the same room, the same socket and the same reducer.
   * A second hook would need its own copy of `matchId`, the realtime client
   * and the dispatch, and §3 forbids a second controls store.
   */
  command: (command: ActiveCommand) => Promise<void>;
}

/** Which frame each control sends. One table, so the mapping is readable. */
const COMMAND_FRAMES: Record<ActiveCommand, GameCommandType> = {
  resign: "game.resign",
  offer: "game.draw.offer",
  accept: "game.draw.accept",
  decline: "game.draw.decline",
};

export function useGameRoom(matchId: string): GameRoom {
  const realtime = useRealtime();
  const status = useConnectionStatus();
  const { state: session } = useSession();
  const [state, dispatch] = useReducer(reduce, matchId, initialState);

  const viewerId = isAuthenticated(session) ? session.user.id : null;

  // The last sequence the server confirmed. A ref as well as reducer state
  // because the reconnect effect reads it from a closure that must not be
  // re-run on every ply — re-running it would re-join on every move.
  const sequence = useRef(-1);
  sequence.current = state.sequence;

  const joined = useRef(false);

  // Which seat this client holds, for the frame handler — see the
  // `game.draw.offered` case on why this is a ref.
  const sideRef = useRef(state.side);
  sideRef.current = state.side;

  // --- inbound ------------------------------------------------------------

  useFrames(
    useCallback(
      (frame: InboundFrame) => {
        if (frame.channel !== "game") return;
        const payload = frame.payload;

        switch (frame.type) {
          case "game.snapshot": {
            if (viewerId === null) return;
            // Validated before it is trusted: the transport hands over
            // `Record<string, unknown>` and a payload that is not a snapshot
            // must not become a board.
            const snapshot = asSnapshot(payload);
            if (snapshot === null || snapshot.match_id !== matchId) return;
            joined.current = true;
            dispatch({ type: "snapshot", payload: snapshot, viewerId });
            return;
          }

          case "game.move.applied":
          case "game.move.accepted": {
            const move = asMove(payload);
            if (move === null || move.match_id !== matchId) return;
            // Both are dispatched identically, and that is not a
            // contradiction of §11's "model them separately": the
            // *distinction* is that `accepted` resolves the submitter's
            // promise, which the request registry already did before this
            // ran. What each does to the board is the same, and writing it
            // twice would be two chances to diverge.
            dispatch({ type: "applied", payload: move });
            return;
          }

          case "game.move.rejected": {
            const code = payload.code;
            dispatch({
              type: "rejected",
              code: typeof code === "string" ? code : "internal_error",
            });
            return;
          }

          case "game.resync_required":
            dispatch({ type: "resyncing" });
            return;

          case "game.draw.offered": {
            const offered = asDrawOffered(payload);
            if (offered === null || offered.match_id !== matchId) return;
            // `sideRef` rather than `state.side`: this callback is installed
            // once and must see the current seat, and putting `side` in the
            // dependency list would tear the subscription down and rebuild
            // it the first time a snapshot named us.
            dispatch({ type: "draw_offered", payload: offered, viewerSide: sideRef.current });
            return;
          }

          case "game.draw.declined": {
            const declined = asDrawDeclined(payload);
            if (declined === null || declined.match_id !== matchId) return;
            dispatch({ type: "draw_declined", payload: declined });
            return;
          }

          case "game.completed": {
            const completed = asCompleted(payload);
            if (completed === null || completed.match_id !== matchId) return;
            // §15: the authoritative payload wins over whatever this client
            // asked for. A resignation that raced an accepted draw ends as
            // the server settled it.
            dispatch({ type: "completed", payload: completed });
            return;
          }

          case "game.draw.state": {
            // A64-020.5D §11, §13. The authoritative per-seat agreement,
            // which **replaces** A64-020.5C's snapshot-per-ply workaround:
            // that re-read a whole snapshot once per ply for a restricted
            // player, because permissions could not ride on
            // `game.move.applied`. They now arrive addressed.
            //
            // Order-independent by construction (§12): this touches the
            // agreement and nothing else, so it is harmless whether it
            // arrives before or after the move that caused it.
            const draw = asDrawState(payload);
            if (draw === null || draw.match_id !== matchId) return;
            dispatch({ type: "draw_state", payload: draw });
            return;
          }

          case "game.command.rejected": {
            const code = payload.code;
            dispatch({
              type: "command_rejected",
              code: typeof code === "string" ? code : "internal_error",
            });
            return;
          }

          case "error": {
            const code = payload.code;
            dispatch(fatalOrUnavailable(typeof code === "string" ? code : "internal_error"));
            return;
          }

          default:
            // `game.events` is unwrapped by the gateway into individual
            // frames before it reaches a subscriber, and `room.joined`,
            // `room.left` and `game.resumed` are answered through the
            // request registry. Nothing else needs handling, and an
            // unrecognised type is ignored rather than thrown (§7).
            return;
        }
      },
      [matchId, viewerId],
    ),
  );

  // --- joining and resuming -----------------------------------------------

  useEffect(() => {
    if (!isReady(status)) {
      if (joined.current) dispatch({ type: "disconnected" });
      return;
    }

    let cancelled = false;

    const enter = async () => {
      dispatch(joined.current ? { type: "resuming" } : { type: "joining" });
      try {
        await realtime.request("room.join", { match_id: matchId }, "game");
        if (cancelled) return;

        // Resume with what we know. `-1` means "nothing" and the gateway
        // reads an absent sequence as "start me over", which is exactly
        // right for a first join.
        const known = sequence.current;
        await realtime.request(
          "game.resume",
          known >= 0
            ? { match_id: matchId, last_known_sequence: known }
            : { match_id: matchId },
          "game",
        );
      } catch (error) {
        if (cancelled) return;
        const code = error instanceof RealtimeError ? error.code : "internal_error";
        if (code === "disconnected" || code === "timeout") {
          // The socket went away mid-handshake. The status effect will run
          // again when it comes back; this is not a failure of the room.
          dispatch({ type: "disconnected" });
          return;
        }
        dispatch(fatalOrUnavailable(code));
      }
    };

    void enter();
    return () => {
      cancelled = true;
    };
    // `status` alone: re-entering on every ply would re-join the room on
    // every move. `sequence` is read through the ref precisely so it can
    // stay out of this list.
  }, [status, matchId, realtime]);

  // --- resync -------------------------------------------------------------

  const resync = useCallback(() => {
    if (!isReady(realtime.currentStatus)) return;
    dispatch({ type: "resyncing" });
    // No `last_known_sequence`: §18's "the client is asking to start over".
    void realtime
      .request("game.resume", { match_id: matchId }, "game")
      .catch((error: unknown) => reportError(error, { scope: "game-resync", matchId }));
  }, [matchId, realtime]);

  // A gap detected while applying a frame puts the reducer in `resyncing`;
  // this is what actually asks. Kept out of the reducer because a reducer
  // must not perform I/O.
  const resyncing = state.phase === "resyncing";
  useEffect(() => {
    if (resyncing) resync();
  }, [resyncing, resync]);

  // --- leaving ------------------------------------------------------------

  useEffect(() => {
    return () => {
      // §30: detach the room, keep the socket. Fire-and-forget — the page is
      // unmounting and there is nobody left to tell if it failed, and the
      // gateway drops the room when the socket closes anyway.
      if (joined.current) realtime.send("room.leave", { match_id: matchId }, "game");
      joined.current = false;
    };
  }, [matchId, realtime]);

  // --- submitting ---------------------------------------------------------

  const submit = useCallback(
    async (path: string[]) => {
      const requestId = realtime.requests.nextId();
      dispatch({ type: "submitting", move: { path, requestId } });
      try {
        // The acknowledgement arrives as `game.move.accepted`, which the
        // frame handler above also applies to the board. Awaiting it here is
        // what turns a refusal into a rejection this player sees.
        await realtime.request("game.move.submit", { match_id: matchId, path }, "game");
      } catch (error) {
        const code = error instanceof RealtimeError ? error.code : "internal_error";
        // §16: never resubmit automatically. A timeout means the server may
        // or may not have applied it, and the only safe answer is to ask
        // again what the truth is.
        if (code === "timeout" || code === "disconnected") {
          dispatch({ type: "resyncing" });
          return;
        }
        dispatch({ type: "rejected", code });
      }
    },
    [matchId, realtime],
  );

  // --- participant commands — §5, §6, §8, §9, §12 -------------------------

  const command = useCallback(
    async (kind: ActiveCommand) => {
      dispatch({ type: "command_sent", command: kind });
      try {
        // The answer is the authoritative event correlated to our
        // `request_id` — `game.draw.offered`, `game.draw.declined` or
        // `game.completed`. The frame handler above applies it, so awaiting
        // here exists to surface a *refusal*, which arrives as
        // `game.command.rejected` and rejects this promise.
        //
        // `match_id` and nothing else (§19). The socket's redeemed ticket is
        // the identity, and the frame has no field for a side.
        await realtime.request(COMMAND_FRAMES[kind], { match_id: matchId }, "game");
      } catch (error) {
        const code = error instanceof RealtimeError ? error.code : "internal_error";
        // §12: never resubmit after an ambiguous timeout. A resignation the
        // server may or may not have applied must not be sent twice — the
        // only safe answer is to ask what the truth is, which `resyncing`
        // does by requesting a fresh snapshot.
        if (code === "timeout" || code === "disconnected") {
          dispatch({ type: "resyncing" });
          return;
        }
        dispatch({ type: "command_rejected", code });
      }
    },
    [matchId, realtime],
  );

  return { state, submit, resync, command };
}

/**
 * Which refusals are recoverable and which are not.
 *
 * `unavailable` is "this match is not for you, or not now" — a wrong id, a
 * match that ended, a player who is not in it. `fatal` is reserved for a
 * refusal that says the connection itself is not trusted, where retrying is
 * a loop.
 */
function fatalOrUnavailable(
  code: string,
): { type: "unavailable"; code: string } | { type: "fatal"; code: string } {
  const fatal: GatewayErrorCode[] = ["invalid_ticket", "malformed_message"];
  return (fatal as string[]).includes(code)
    ? { type: "fatal", code }
    : { type: "unavailable", code };
}

// --- payload validation ----------------------------------------------------

function asSnapshot(payload: Record<string, unknown>): SnapshotPayload | null {
  if (typeof payload.match_id !== "string") return null;
  if (typeof payload.sequence !== "number") return null;
  if (!Array.isArray(payload.pieces)) return null;
  if (payload.side_to_move !== "light" && payload.side_to_move !== "dark") return null;
  const participants = payload.participants;
  if (typeof participants !== "object" || participants === null) return null;
  return payload as unknown as SnapshotPayload;
}

function asDrawState(payload: Record<string, unknown>): DrawStatePayload | null {
  if (typeof payload.match_id !== "string") return null;
  if (typeof payload.may_offer !== "boolean") return null;
  if (typeof payload.may_accept !== "boolean") return null;
  if (typeof payload.may_decline !== "boolean") return null;
  // `offer` is `null` or an object; anything else is not this frame.
  const offer = payload.offer;
  if (offer !== null && (typeof offer !== "object" || offer === undefined)) return null;
  return payload as unknown as DrawStatePayload;
}

function asDrawOffered(payload: Record<string, unknown>): DrawOfferedPayload | null {
  if (typeof payload.match_id !== "string") return null;
  if (payload.offered_by !== "light" && payload.offered_by !== "dark") return null;
  if (typeof payload.offered_at_ply !== "number") return null;
  if (typeof payload.offered_at !== "string") return null;
  return payload as unknown as DrawOfferedPayload;
}

function asDrawDeclined(payload: Record<string, unknown>): DrawDeclinedPayload | null {
  if (typeof payload.match_id !== "string") return null;
  if (payload.declined_by !== "light" && payload.declined_by !== "dark") return null;
  if (typeof payload.ply !== "number") return null;
  return payload as unknown as DrawDeclinedPayload;
}

function asCompleted(payload: Record<string, unknown>): GameCompletedPayload | null {
  if (typeof payload.match_id !== "string") return null;
  const result = payload.result;
  if (typeof result !== "object" || result === null) return null;
  if (typeof (result as Record<string, unknown>).outcome !== "string") return null;
  return payload as unknown as GameCompletedPayload;
}

function asMove(payload: Record<string, unknown>): MovePayload | null {
  if (typeof payload.match_id !== "string") return null;
  if (typeof payload.ply !== "number") return null;
  if (payload.side_to_move !== "light" && payload.side_to_move !== "dark") return null;
  const applied = payload.applied;
  if (typeof applied !== "object" || applied === null) return null;
  const path = (applied as Record<string, unknown>).path;
  if (!Array.isArray(path) || path.length < 2) return null;
  return payload as unknown as MovePayload;
}
