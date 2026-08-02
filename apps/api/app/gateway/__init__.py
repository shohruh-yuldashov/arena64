"""The realtime WebSocket gateway — AD-09, AD-11, R-7. A64-016.1.

## Why `app/gateway/` and not `app/modules/gateway/`

architecture.md names it twice and both times as a **tier**, not a context:
the module dependency diagram labels it `gateway — transport only`, and R-7
states "the gateway contains no domain logic. It validates, authenticates,
rate-limits, routes, and fans out. It never decides whether a move is legal."

A bounded context under `app/modules/` would come with a `domain/` package,
and the first thing anyone would do is put something in it. There is nothing
to put: a connection is not an aggregate, a heartbeat is not a business rule,
and the one decision this tier makes — when a player becomes online — is
`users`' rule invoked through `users`' published port.

So it sits beside `app/api/`, which is the same kind of thing: an interface
layer that terminates a transport and calls application services. The
architecture diagram draws exactly that, with a dotted edge from the gateway
to the API denoting that both "invoke the same application services".

It is **not** `app/platform/` either, and that boundary is enforced: the
`platform-imports-no-module` contract forbids `app.platform` from importing
any bounded context, and this tier's entire job is to call two of them.

## What is here

    protocol.py      the versioned envelope and four message types (§6)
    ports.py         four protocols, none of which mentions a framework (§8)
    connections.py   the connection lifecycle — the only file with rules in it
    registry.py      the fleet-wide connection registry, over Redis (§3)
    socket.py        the Starlette adapter — the only file that imports it
    router.py        `GET /ws`, three statements long (§1)
    dependencies.py  the composition root
    metrics.py       three counters and one observation, bounded labels (§9)

## What this build deliberately does not do

No rooms, no move submission, no clocks, no state synchronisation, no
reconnection replay, no spectators, no chat. A64-016.1 is the foundation:
a socket that is authenticated, registered, counted, kept alive and cleaned
up. `MessageType` has four members and every one of them is implemented,
which is the same posture `TokenType` took with `ACCESS` alone — an unused
member on a protocol surface reads as "this is wired up" to whoever adds the
next task.

The routing table those types will need arrives in A64-016.2, when there is
something to route to. See `docs/01-architecture/websocket.md`.
"""
