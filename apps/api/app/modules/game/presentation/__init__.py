"""`game`'s presentation layer — a composition root and nothing else.

`game` serves no HTTP route of its own and is not expected to: its surface is
`public/`, and the routes that expose match acceptance belong to
`matchmaking`, which owns the queue journey a match comes out of.

What arrived in A64-016.2 is a **second consumer** — the WebSocket gateway,
which needs `MatchRosterReader` to decide whether a socket may join a match's
room. Until then `matchmaking`'s composition root named `game`'s concrete
classes directly, which A64-015.4 recorded as the deliberate arrangement
("`game`'s concrete classes are named here, and that is the pattern").

Two consumers is the point at which that stops scaling. The alternative was
the gateway naming `GameMatchRoster` and `SqlAlchemyMatchRecordRepository`
itself, which `.importlinter`'s `gateway-reaches-modules-through-public`
contract forbids — correctly, because a transport tier that can construct a
repository is one that can reach past every port it was given.

So `game` gets what every other module already has: one file that knows how
its own services are assembled. `presentation/dependencies` is excluded from
the privacy contracts for exactly this reason (BR-6 — a *module* must not
reach for the container; a root wiring modules together is the root's job).
"""
