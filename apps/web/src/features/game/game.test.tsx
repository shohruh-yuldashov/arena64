import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { allSquares, boardFrom, isPlayable, toCoordinate, toSquare } from "@/entities/board";
import { legalMoves } from "@/features/game/engine/moves";
import { initialState, reduce } from "@/features/game/model/state";
import type { MovePayload, SnapshotPayload } from "@/shared/realtime";
import { parseFrame } from "@/shared/realtime/protocol";
import { reconnectDelay } from "@/shared/realtime/reconnect-policy";
import { RealtimeError, RequestRegistry } from "@/shared/realtime/request-registry";

/**
 * The live game's rules and protocol — A64-020.5B §32.
 *
 * Everything here is a property that a broken implementation would pass a
 * rendering test on. The board's *appearance* is not tested; its
 * **coordinates**, its **rules** and its **state transitions** are, because
 * those are the three things that decide whether a move a player makes is
 * the move the server receives.
 */

const CORPUS = resolve(process.cwd(), "../../specs/game-engine/corpus");

interface CorpusCase {
  id: string;
  variant: string;
  side_to_move: "light" | "dark";
  pieces: { square: string; side: "light" | "dark"; rank: "man" | "king" }[];
  expected_moves: { path: string[]; captured: string[]; promotes_to: string | null }[];
}

function corpus(): CorpusCase[] {
  const files = ["v1/men-basic.json", "v1/men-capture-sequences.json", "v2/kings.json"];
  const superseded = new Set<string>();
  const cases: CorpusCase[] = [];

  for (const file of files) {
    const data = JSON.parse(readFileSync(resolve(CORPUS, file), "utf8")) as {
      cases?: CorpusCase[];
      supersedes?: { id: string }[];
    };
    for (const entry of data.supersedes ?? []) superseded.add(entry.id);
    cases.push(...(data.cases ?? []));
  }

  // Only the variant this platform offers. The corpus also carries
  // international and English cases, whose rules genuinely differ — a
  // kernel that passed those would be implementing three games.
  return cases.filter((c) => !superseded.has(c.id) && c.variant === "russian_8x8");
}

describe("the board's coordinates", () => {
  it("agrees with the engine's frame in both directions", () => {
    // §13. A mapping that is wrong by one square sends a legal move for a
    // different piece, and nothing above this layer could notice.
    expect(toCoordinate("a1")).toEqual({ file: 0, rank: 0 });
    expect(toCoordinate("h8")).toEqual({ file: 7, rank: 7 });
    expect(toSquare({ file: 2, rank: 2 })).toBe("c3");

    for (const square of allSquares()) {
      const coordinate = toCoordinate(square);
      expect(coordinate).not.toBeNull();
      expect(toSquare(coordinate!)).toBe(square);
    }

    // Playable squares are the dark ones, and `a1` is one — which is what
    // the corpus's opening position asserts by occupying it.
    expect(isPlayable({ file: 0, rank: 0 })).toBe(true);
    expect(isPlayable({ file: 1, rank: 0 })).toBe(false);
    expect(toCoordinate("i9")).toBeNull();
  });
});

describe("the rules kernel", () => {
  it("reproduces every russian_8x8 case in the shared corpus", () => {
    // §12: the rules are not written from memory, they are the corpus's.
    // Reading the files rather than copying them means a rules change
    // breaks this test rather than the product — which is what AD-14's
    // "the corpus is the contract" is for.
    const cases = corpus();
    expect(cases.length).toBeGreaterThan(20);

    const failures: string[] = [];
    for (const testCase of cases) {
      const actual = legalMoves(boardFrom(testCase.pieces), testCase.side_to_move)
        .map((move) => `${move.path.join("x")}|${[...move.captured].sort().join(",")}`)
        .sort();
      const expected = testCase.expected_moves
        .map((move) => `${move.path.join("x")}|${[...move.captured].sort().join(",")}`)
        .sort();
      if (JSON.stringify(actual) !== JSON.stringify(expected)) {
        failures.push(
          `${testCase.id}: expected [${expected.join(" ")}] got [${actual.join(" ")}]`,
        );
      }
    }

    expect(failures).toEqual([]);
  });

  it("offers only complete capture sequences, never a prefix", () => {
    // Called out separately because it is the property the *interaction*
    // depends on: `useMoveSelection` narrows candidates by prefix, and a
    // generator that emitted prefixes would let a player submit a half-move
    // the server refuses.
    const board = boardFrom([
      { square: "c3", side: "light", rank: "man" },
      { square: "d4", side: "dark", rank: "man" },
      { square: "f6", side: "dark", rank: "man" },
    ]);

    const moves = legalMoves(board, "light");

    expect(moves.map((m) => m.path.join("x"))).toEqual(["c3xe5xg7"]);
  });
});

describe("the protocol codec", () => {
  it("refuses anything it does not speak instead of throwing", () => {
    // §7: one unsupported frame must not take the app down, and a transport
    // callback has no boundary above it.
    expect(parseFrame("not json")).toBeNull();
    expect(parseFrame('"a string"')).toBeNull();
    expect(parseFrame(JSON.stringify({ v: 2, type: "pong", payload: {} }))).toBeNull();
    expect(parseFrame(JSON.stringify({ v: 1, type: "made.up", payload: {} }))).toBeNull();
    expect(parseFrame(JSON.stringify({ v: 1, type: "pong" }))).toBeNull();

    const frame = parseFrame(
      JSON.stringify({ v: 1, type: "pong", request_id: "r1", channel: "game", payload: {} }),
    );
    expect(frame).toEqual({
      v: 1,
      type: "pong",
      request_id: "r1",
      channel: "game",
      payload: {},
    });

    // The gateway defaults an absent channel to `system`, and so does this.
    expect(parseFrame(JSON.stringify({ v: 1, type: "pong", payload: {} }))?.channel).toBe(
      "system",
    );
  });
});

describe("request correlation", () => {
  it("resolves the matching frame, rejects a refusal, and cleans up on close", async () => {
    // §8: a move that stays pending forever is a board that never becomes
    // interactive again, and the player cannot tell that from a slow server.
    const registry = new RequestRegistry();

    const id = registry.nextId();
    const waiting = registry.await(id);
    expect(registry.size).toBe(1);
    registry.settle({
      v: 1,
      type: "game.move.accepted",
      request_id: id,
      channel: "game",
      payload: { ply: 3 },
    });
    await expect(waiting).resolves.toMatchObject({ type: "game.move.accepted" });
    expect(registry.size).toBe(0);

    // A refusal carrying our id is *this request* failing, so it rejects
    // rather than resolving — which is what lets a caller handle it in one
    // place instead of inspecting every resolved frame.
    const refused = registry.nextId();
    const rejected = registry.await(refused);
    registry.settle({
      v: 1,
      type: "game.move.rejected",
      request_id: refused,
      channel: "game",
      payload: { code: "not_your_turn" },
    });
    await expect(rejected).rejects.toMatchObject({ code: "not_your_turn" });

    // A dropped socket takes every in-flight answer with it; leaving the
    // entries would fire their timers minutes later against a finished game.
    const orphan = registry.await(registry.nextId());
    registry.rejectAll(new RealtimeError("disconnected", "closed"));
    await expect(orphan).rejects.toMatchObject({ code: "disconnected" });
    expect(registry.size).toBe(0);
  });
});

describe("the reconnect policy", () => {
  it("backs off to a ceiling and never exceeds it", () => {
    // §6. The ceiling is what stops the tight loop; the jitter is what
    // stops two players whose game reconnected them simultaneously from
    // retrying in lockstep.
    const lowest = (attempt: number) => reconnectDelay(attempt, () => 0);
    const highest = (attempt: number) => reconnectDelay(attempt, () => 1);

    expect(lowest(1)).toBe(375);
    expect(highest(1)).toBe(625);
    expect(lowest(2)).toBe(750);

    for (let attempt = 1; attempt <= 20; attempt += 1) {
      expect(highest(attempt)).toBeLessThanOrEqual(15_000);
      expect(lowest(attempt)).toBeGreaterThan(0);
    }
    // Reached, and then held.
    expect(highest(10)).toBe(highest(20));
  });
});

const VIEWER = "11111111-1111-1111-1111-111111111111";
const OPPONENT = "22222222-2222-2222-2222-222222222222";

function snapshot(overrides: Partial<SnapshotPayload> = {}): SnapshotPayload {
  return {
    match_id: "m1",
    engine_version: 2,
    variant: "russian_8x8",
    status: "active",
    sequence: 4,
    side_to_move: "light",
    fingerprint: "fp4",
    pieces: [
      { square: "c3", side: "light", rank: "man" },
      { square: "f6", side: "dark", rank: "man" },
    ],
    participants: { light: VIEWER, dark: OPPONENT },
    clock: null,
    result: null,
    server_time: "2026-08-05T10:00:00Z",
    ...overrides,
  };
}

function applied(ply: number, overrides: Partial<MovePayload> = {}): MovePayload {
  return {
    match_id: "m1",
    ply,
    side_to_move: "dark",
    fingerprint: `fp${ply}`,
    applied: { path: ["c3", "d4"], captured: [], promoted_to: null },
    ...overrides,
  };
}

describe("the game state machine", () => {
  it("takes a snapshot as authoritative replacement and names our side", () => {
    // §18: replacement, never a merge. A client that reconciled a snapshot
    // against what it already held would invent a third state neither side
    // agreed to.
    const state = reduce(initialState("m1"), {
      type: "snapshot",
      payload: snapshot(),
      viewerId: VIEWER,
    });

    expect(state.phase).toBe("active");
    expect(state.side).toBe("light");
    expect(state.sequence).toBe(4);
    expect(state.board.get("c3")).toEqual({ square: "c3", side: "light", rank: "man" });

    // A snapshot of a finished game resumes as finished — reconnecting to a
    // match that ended while you were away is normal, not an error.
    const over = reduce(initialState("m1"), {
      type: "snapshot",
      payload: snapshot({
        result: { outcome: "win", termination_reason: "timeout", winner: "dark" },
      }),
      viewerId: VIEWER,
    });
    expect(over.phase).toBe("completed");
  });

  it("ignores a duplicate, refuses to move backward, and resyncs on a gap", () => {
    // §17, and the three failures it prevents are all ordinary on a
    // reconnect: the buffer replays frames we already have, out of order, or
    // with one missing.
    const base = reduce(initialState("m1"), {
      type: "snapshot",
      payload: snapshot(),
      viewerId: VIEWER,
    });

    const next = reduce(base, { type: "applied", payload: applied(5) });
    expect(next.sequence).toBe(5);
    expect(next.board.has("c3")).toBe(false);
    expect(next.board.get("d4")).toMatchObject({ side: "light" });

    // The same frame again changes nothing at all — object identity proves
    // it, which is stronger than comparing fields.
    expect(reduce(next, { type: "applied", payload: applied(5) })).toBe(next);
    // An older one cannot roll the board back.
    expect(reduce(next, { type: "applied", payload: applied(4) })).toBe(next);
    // A gap is not applied; it is admitted.
    expect(reduce(next, { type: "applied", payload: applied(7) }).phase).toBe("resyncing");
  });

  it("needs no rollback on a rejection, because it never advanced", () => {
    // §11 and §16. The board is not optimistically advanced, so a refusal
    // clears a flag and nothing else — there is no state to restore and so
    // no way to restore it wrongly.
    const base = reduce(initialState("m1"), {
      type: "snapshot",
      payload: snapshot(),
      viewerId: VIEWER,
    });
    const sent = reduce(base, {
      type: "submitting",
      move: { path: ["c3", "d4"], requestId: "r1" },
    });

    expect(sent.phase).toBe("submitting_move");
    expect(sent.sequence).toBe(4);

    const refused = reduce(sent, { type: "rejected", code: "not_your_turn" });

    expect(refused.phase).toBe("active");
    expect(refused.pending).toBeNull();
    expect(refused.sequence).toBe(4);
    expect(refused.board).toBe(base.board);
    expect(refused.lastRejection).toBe("not_your_turn");
  });

  it("applies the server's own move, not the kernel's opinion of it", () => {
    // §11. The board is rebuilt from the payload's path, captures and
    // promotion — so a client kernel that disagreed with the engine would
    // mis-highlight a square and never corrupt the position.
    const base = reduce(initialState("m1"), {
      type: "snapshot",
      payload: snapshot({
        pieces: [
          { square: "c3", side: "light", rank: "man" },
          { square: "d4", side: "dark", rank: "man" },
        ],
      }),
      viewerId: VIEWER,
    });

    const jumped = reduce(base, {
      type: "applied",
      payload: applied(5, {
        applied: { path: ["c3", "e5"], captured: ["d4"], promoted_to: "king" },
      }),
    });

    expect(jumped.board.has("d4")).toBe(false);
    expect(jumped.board.get("e5")).toEqual({ square: "e5", side: "light", rank: "king" });
    expect(jumped.history).toHaveLength(1);
  });
});
