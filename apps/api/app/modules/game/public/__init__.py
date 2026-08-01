"""`game`'s published surface — deliberately empty.

BE-03 and architecture.md R-1 make this the only package another module
may import. Nothing is published yet, and that is the correct state rather
than an oversight: R-3 says the modules that care about matches —
`rating`, `statistics`, `achievements`, `leaderboard`, `notifications`,
`replay`, `fairplay`, `spectator` — "**never call into `game` to change
anything**; they subscribe to its events".

So the first thing to appear here is unlikely to be `Match`. It will be
whatever a synchronous reader genuinely needs: architecture.md §7 draws
exactly four inbound edges, and each is a *port* (`spectator`'s read-only
view, `replay`'s and `fairplay`'s history reads, `admin`'s adjudication).
Publishing the aggregate now would let a consumer take a dependency none
of those edges intends.

The package exists so the boundary is enforceable today —
`.importlinter`'s `game-internals-are-private` contract needs somewhere to
point — rather than added in the first change that has something to
publish and no rule stopping it publishing too much.
"""
