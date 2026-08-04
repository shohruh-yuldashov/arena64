"""`tournament` — single-elimination tournaments. SPEC-TOURNAMENT.

A64-019.1 builds the domain and nothing else: the lifecycle, the round
model and the bracket node, all pure. No persistence, no HTTP, no match
creation, no workers.

## What this module owns, and what it must never touch

    tournament   Tournament, Round, BracketNode, Registration, Standing
    game         Match, the move log, the result
    rating       PlayerRating, RatingAdjustment

`tournament` never writes to `game`'s schema (R-3) and never references its
relations with a foreign key (DB-03). It creates matches through
`game.public`'s `CreateMatch` — the same port `matchmaking` uses — and
recognises them again by the opaque `origin_ref` A64-019.0 added.

That is the whole integration, and it is why `services.md` §11.3 could
predict this feature needs no new mechanism.
"""
