# Game Engine Conformance Corpus — v2

> **Status:** Active — the corpus in force is v1 plus v2, minus what v2 supersedes
> **Owner:** _Unassigned_
> **Related:** `../v1/README.md`, `specs/game-engine.md`, `docs/01-architecture/architecture.md` AD-14

## Why there is a v2

v1's rule was that a version is **append-only**: "a case may be added, but an existing case is
never edited or deleted, because a case that changes retroactively cannot tell you which
implementation was right. A rules change that invalidates a case opens `v2` and states what
moved."

A64-014.5 is that rules change. Kings move now, so the one v1 case asserting that the engine
*refuses* a position containing a king of the side to move is no longer true of any correct
implementation.

v1 is untouched on disk. Nothing in it was edited, renumbered or deleted.

## How supersession works

A v2 file may carry a top-level `supersedes` array naming cases from earlier versions that no
longer apply:

```jsonc
{
  "corpus_version": 2,
  "supersedes": [
    {
      "version": 1,
      "id": "a-king-of-the-side-to-move-cannot-be-evaluated",
      "replaced_by": "king-quiet-moves-along-open-diagonals",
      "reason": "…what rule changed…"
    }
  ],
  "cases": [ … ]
}
```

A reader loads every version up to the one it implements, collects every `supersedes` entry, and
drops those ids from the earlier versions' cases. **The mechanism is data, not prose**, so a
TypeScript engine gets the same active set from the same files without reading this document.

`reason` and `replaced_by` are for humans. They are what makes the history explainable a year
later: v1 says the engine refused kings, v2 says why it stopped and where to look instead.

### What v2 supersedes

| Version | Case | Replaced by | Why |
| --- | --- | --- | --- |
| 1 | `a-king-of-the-side-to-move-cannot-be-evaluated` *(a rejection)* | `king-quiet-moves-along-open-diagonals` | The position is the same and the expectation is inverted: A64-014.3 refused it with `UnsupportedPieceMovement` because kings had no moves; A64-014.5 implements them, and the same lone king on `c3` now has eleven |

`UnsupportedPieceMovement` is deleted from the engine. `RejectionCategory.unsupported_piece`
survives in the **corpus format** — v1's files still contain it, and a reader that could not parse
them would make the history unreadable, which is the whole point of keeping it. No active case
uses it.

## Format

Unchanged from v1, and documented there: the same `cases` and `rejections` shapes, the same
algebraic square notation, the same ordering rule. v2 adds only the optional top-level
`supersedes` array described above.

## Files

| File | Key | Covers |
| --- | --- | --- |
| `kings.json` | `cases`, `supersedes` | King quiet moves and captures, multiple landing squares, direction changes, the taken-once rule with kings, maximum capture with kings, promotion continuation under all three variant rules, and a mixed man-and-king position — A64-014.5 |
| `terminal-positions.json` | `terminal_positions` | Which positions have ended, who won, and why — A64-014.6 |
| `draw-sequences.json` | `draw_sequences` | Draw rules and lifecycle counters over an ordered list of plies — A64-014.7 |
| `replays.json` | `replays` | Stored games replayed through the live rules, and records that must be refused — A64-014.8 |

## Format — `terminal_positions`

A **third** expectation shape, beside v1's `cases` and `rejections`. "These are the legal moves"
and "this position has ended" are different claims about a position, and bending one into the
other would make a reader guess which it was looking at.

```jsonc
{
  "corpus_version": 2,
  "terminal_positions": [
    {
      "id": "kebab-case, unique within the version, never reused",
      "description": "what the case proves, in one sentence",
      "variant": "russian_8x8",
      "side_to_move": "light",
      "pieces": [{ "square": "c3", "side": "dark", "rank": "man" }],
      "terminal": true,
      "winner": "dark",
      "reason": "all_pieces_captured"
    }
  ]
}
```

`variant`, `side_to_move` and `pieces` are exactly as above — one format for a square, whichever
kind of case it appears in.

| Field | Rule |
| --- | --- |
| `terminal` | Whether the game has ended in this position |
| `winner` | The side that won, or `null` |
| `reason` | `all_pieces_captured` or `no_legal_moves`, or `null` |

**`winner` and `reason` are absent together and present together** — the same shape the evaluator
answers with, so a case cannot be written half-filled. A reader compares the verdict whole: one
that checked only `terminal` would pass a corpus naming the wrong winner.

### No draws here

Every draw in draughts is a property of the game's *history* — the same position occurring often
enough, or too many plies without progress — and a single position cannot show one. Draw
expectations need a case shape that describes a game rather than a board, and they arrive with
the rules that find them (A64-014.7).

## Variants

v2 is the first version to use `english_8x8` beside `russian_8x8` and `international_10x10`.
English draughts is configuration only — men that capture forward only, kings that move one
square, and crowning that ends the ply — and it is here because it is the one rule set that gives
those three axes a second value. Without it they are settings nothing can tell apart from
constants.

## Format — `draw_sequences`

The **fourth** expectation shape. A draw is a property of a *game* rather than of a board, so a
case cannot state it the way `terminal_positions` states a loss: it names an opening position, an
ordered list of moves, and what the match looks like once they have all been played.

```jsonc
{
  "corpus_version": 2,
  "draw_sequences": [
    {
      "id": "kebab-case, unique within the version, never reused",
      "description": "what the case proves, in one sentence",
      "engine_version": 2,
      "variant": "russian_8x8",
      "side_to_move": "light",
      "pieces": [{ "square": "a1", "side": "light", "rank": "king" }],
      "moves": [{ "path": ["a1", "b2"], "captured": [], "promotes_to": null }],
      "expected_status": "active",
      "expected_outcome": null,
      "expected_reason": null,
      "expected_winner": null,
      "expected_position_occurrences": 1,
      "expected_plies_since_progress": 1
    }
  ]
}
```

`variant`, `side_to_move`, `pieces` and the shape of a move are exactly as above — one format for
a square and one for a move, whichever kind of case they appear in.

| Field | Rule |
| --- | --- |
| `engine_version` | Which rules build the expectation was written for. **Part of the expectation, not metadata**: draws arrived in version 2, so a reader on 1 would disagree about the last ply of half of these |
| `moves` | Played in order from the opening position. Every one must be legal |
| `expected_status` | `created`, `active`, `completed` or `aborted` |
| `expected_outcome` | `win`, `draw`, `none`, or `null` while the match is still running |
| `expected_reason` | A `TerminationReason` value, or `null` |
| `expected_winner` | `light`, `dark`, or `null`. Present exactly when the outcome is `win` |
| `expected_position_occurrences` | How often the final position has occurred, **counting itself** |
| `expected_plies_since_progress` | Plies since the last capture or man's move |

### Repetition counts occurrences, not returns

The opening position has occurred **once** before anybody has moved, so a threefold rule fires on
the *second* return:

| | Occurrence |
| --- | --- |
| Opening | 1 |
| First return | 2 — not a draw |
| Second return | 3 — draw |

A case exists for each of those three states, because an implementation that counted returns
would pass a corpus that only tested the draw.

## Format — `replays`

The **fifth** expectation shape: a stored game, and what replaying it must produce — or refuse.

```jsonc
{
  "corpus_version": 2,
  "replays": [
    {
      "id": "kebab-case, unique within the version, never reused",
      "description": "what the case proves, in one sentence",
      "engine_version": 2,
      "variant": "russian_8x8",
      "opening_position": { "variant": "…", "side_to_move": "light", "pieces": [] },
      "records": [
        {
          "ply_number": 1,
          "move": { "path": ["c3", "d4"], "captured": [], "promotes_to": null },
          "resulting_position_hash": "russian_8x8/dark/d4=light:man",
          "think_time_ms": null,
          "remaining_clock_ms": null
        }
      ],
      "expected_rejection": null,
      "expected_final_position_hash": "russian_8x8/dark/d4=light:man",
      "expected_status": "active",
      "expected_result": null,
      "expected_position_occurrences": 1,
      "expected_plies_since_progress": 1
    }
  ]
}
```

| Field | Rule |
| --- | --- |
| `engine_version` | Explicit, always. Never inferred from anything |
| `opening_position` | The position the game started from, in the same shape a position takes anywhere in this corpus |
| `records` | The move log. Ply numbers contiguous from 1; each carries the fingerprint of the position it produced |
| `resulting_position_hash` | `Position.fingerprint`. **Not a Zobrist hash** — see `specs/game-engine.md` §8.4 |
| `think_time_ms`, `remaining_clock_ms` | Required keys, `null` until clocks exist. `null` says "not measured"; a zero would say "measured, and instant" |
| `expected_rejection` | A refusal category, or `null` for a replay that must succeed |
| everything else `expected_*` | The reconstructed match. All `null` when a rejection is expected, because a refused replay reaches no state to compare |

### Rejection categories

| Category | Meaning |
| --- | --- |
| `unsupported_engine_version` | Rules this build cannot reproduce |
| `malformed_move_log` | Ply numbers not contiguous from 1 |
| `corrupt_move_log` | A move the rules refuse, or one recorded after the game ended |
| `position_hash_mismatch` | A recorded position and the one the rules produce disagree |

### A replay reproduces *why*, not *where*

`expected_position_occurrences` and `expected_plies_since_progress` are part of the expectation
because a replay that rebuilt only the final board would agree about where the pieces stopped and
have nothing to say about a draw by repetition. Both are recomputed by applying the log; neither
appears in `records`.

**No case exercises a no-progress draw**, because no variant configures a move limit — see
`specs/game-engine.md` §7.7.

## What v2 still does not cover

**The move-limit draws.** The mechanism exists and is tested, but three of the four thresholds
are undecided product rules and no variant configures one — so no case here can exercise them.
See `specs/game-engine.md` §7.7. Also absent: clocks, flag falls, and draw agreement.
