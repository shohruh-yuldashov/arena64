# Game Engine Conformance Corpus — v1

> **Status:** Active — v1 covers men's moves only (A64-014.2)
> **Owner:** _Unassigned_
> **Related:** `specs/game-engine.md`, `docs/01-architecture/architecture.md` AD-14

## What this is

AD-14: "The Python engine and the TypeScript client engine are two implementations governed by
one versioned corpus of positions, legal move sets, and expected outcomes, executed by both in
CI… divergence is caught by a failing test rather than by a player disputing a result. **The
corpus is the contract.**"

This directory is that corpus. It is data, not code, and it is deliberately in `specs/` rather
than beside either implementation: a corpus owned by one engine is that engine's test suite, and
the point is that neither owns it.

Today only the Python engine executes it (`apps/api/tests/unit/test_engine_corpus.py`). There is
no TypeScript engine yet, and the format below exists so that adding one is a reader, not a
renegotiation.

## Versioning

One directory per version. **A version is append-only:** a case may be added, but an existing
case is never edited or deleted, because a case that changes retroactively cannot tell you which
implementation was right. A rules change that invalidates a case opens `v2` and states what moved.

`corpus_version` inside each file repeats the directory's version so a file that is copied
somewhere still says what it is.

## Files

| File | Covers |
| --- | --- |
| `men-basic.json` | Quiet moves, single captures, mandatory-capture priority and promotion detection for men — the whole of A64-014.2 |

## Format

```jsonc
{
  "corpus_version": 1,
  "scope": "…what this file covers and what it deliberately omits",
  "cases": [
    {
      "id": "kebab-case, unique within the version, never reused",
      "description": "what the case proves, in one sentence",
      "variant": "russian_8x8",
      "side_to_move": "light",
      "pieces": [
        { "square": "c3", "side": "light", "rank": "man" }
      ],
      "expected_moves": [
        { "path": ["c3", "e5"], "captured": ["d4"], "promotes_to": "king" }
      ]
    }
  ]
}
```

### Field rules

| Field | Rule |
| --- | --- |
| `variant` | A `BoardVariant` value — `russian_8x8` or `international_10x10` |
| `side_to_move` | `light` or `dark` |
| `pieces` | The complete position. Every square not listed is empty; there is no "and the rest as usual" |
| `square` | Algebraic notation, `a1` at the near-left corner: file letter `a`–`j` for columns 0–9, then a one-based rank. Row 0 is LIGHT's back rank |
| `rank` | `man` or `king` |
| `expected_moves` | **Ordered.** The sequence is part of the expectation, not a set written down in some order — see below |
| `path` | The complete ordered path, origin first, destination last. A quiet move is a two-square path |
| `captured` | Squares of the pieces taken, in the order they are jumped. `[]` for a quiet move |
| `promotes_to` | `king` when the move crowns the moving piece, otherwise `null` |

### Why `expected_moves` is ordered

Because move order is part of the engine's contract, not a rendering detail. A replay reproduces
a game by index, a search visits siblings in a fixed sequence, and two implementations that agree
on the *set* but not the order will diverge the first time either of those matters. The order is
ascending by `(path, captured)`, which is defined in `Move.sort_key` and is the same total order
in any language that compares tuples of `(row, column)` pairs.

## What v1 does not cover

King movement, capture sequences longer than one jump, maximum-capture selection, promotion in
the middle of a sequence, terminal states and draws. Those arrive with the tasks that implement
them, as new cases in this version or as `v2` if a rule they settle contradicts a case here.

A file in this corpus is a claim about **complete** legal move sets. Until kings move
(A64-014.5), every case must therefore be a position with no king belonging to the side to move —
otherwise the expected list would be complete only by accident.
