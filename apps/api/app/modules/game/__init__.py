"""`game` — one complete contest between two players.

architecture.md §6 gives this module the platform's central aggregate:
"`game` | A single contest between two players | `specs/game-engine.md` |
`Match`". domain-model.md §10.4 calls it "the platform's central aggregate
root — everything the platform sells is either an input to a match, a
record of a match, or a consequence of a match."

It is one of the three modules architecture.md R-2 allows to import
`engine`, and the only one allowed to use it to *mutate* state (R-3).

## A64-014.6 builds the rules-facing core, and only that

Present: `Match` — identity, engine version, variant, the authoritative
position, status, ply number, last move, result, position history and the
counter future draw rules read. Plus `MatchStatus`, `MatchResult`,
`MatchOutcome` and the full `TerminationReason`.

Absent, and each a real part of the aggregate domain-model.md §10.4
describes: the two `MatchParticipant` seats, the append-only move log
(MT-5), `ClockState` and time control, `Offer` (draw, takeback, rematch,
abort), the per-match sequence number AD-12's reconnection protocol needs,
and the domain events `match.created` / `move.applied` / `match.completed`.
None is a rules concern, and none of them can be built without the tasks
that own clocks, transport and persistence.

There is also no `application/`, `infrastructure/` or `presentation/`
layer. This task is pure domain: no repository, no endpoint, no storage.

## Why `Match` and not `Game` or `GameState`

domain-model.md §16.1 rejects the alternative by name: "**Game** *and*
**Match** as two entities → One entity: `Match`. They are the same concept
under two names, and two names for one thing guarantees that half the
codebase means one and half means the other."
"""
