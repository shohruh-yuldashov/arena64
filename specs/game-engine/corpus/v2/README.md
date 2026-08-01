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

## What v2 still does not cover

Draws, and everything that needs a game rather than a position: repetition thresholds, move
limits, clocks. `terminal_positions` was the first expectation shape that is not about legal
moves; draws need a second, describing a sequence of plies.
