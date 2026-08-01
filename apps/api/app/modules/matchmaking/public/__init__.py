"""`matchmaking`'s published surface — BE-03, architecture.md R-1.

**Empty, deliberately.** A64-010 set the precedent and gave the reason:
"publishing a port before there is a caller is speculative generality... the
first real consumer adds the narrow port it actually needs." Nothing on the
platform consumes matchmaking yet, so there is nothing here.

The package exists rather than being omitted because it is the *named place*
the first consumer looks, and because `.importlinter` enforces the rule this
package expresses: `matchmaking.domain`, `.application` and
`.infrastructure` are unreachable from every other module, so a consumer
that needs something has no way to reach past this file and must add a port
here instead.

## What will land here, and what will not

The consumer A64-014.2 brings is a **pairing worker**, and it lives inside
this module rather than outside it — so it needs nothing published. The
first genuine outside caller is `game`, and the port it needs is the one
architecture.md §7 already draws as `matchmaking -> game` in the opposite
direction: matchmaking *asks* `game` to create a match, and `game` sets that
contract. So it is entirely possible this package stays empty.

What must **not** appear here is a reader of who is currently queueing. Who
is in a pool right now is the information that would let a player wait for a
favourable one, and there is no consumer whose need for it outweighs that —
the same reasoning `GET /matchmaking/queue/me` applies at the HTTP surface.
"""

__all__: list[str] = []
