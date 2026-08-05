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

## The first consumer — A64-020.5D

`app.gateway`, and it needs exactly one thing: the **shape of a pending
match offer**, so it can project one onto a socket. `PendingMatchOffer` and
`OpponentPreview` are published for that and nothing else.

Published rather than the gateway importing `matchmaking.domain` — which
`.importlinter` would in fact permit today, because `app.gateway` is absent
from the forbidden-source list. That permission is an accident of the
contract having been written before a gateway existed, not an invitation:
every other module reaches `game` and `friends` through `.public`, and one
adapter reaching past that line is how the line stops meaning anything.

**The direction still points the right way.** `matchmaking` does not learn
that a gateway exists: it holds `PendingMatchSink`, a port in the layer that
needs it (AD-06), and the composition root supplies an implementation. What
crosses is a value, not a capability.

## What will not land here

A reader of who is currently queueing. Who is in a pool right now is the
information that would let a player wait for a favourable one, and there is
no consumer whose need for it outweighs that — the same reasoning
`GET /matchmaking/queue/me` applies at the HTTP surface.

`QueueTicket`, `QueuePool` and the pairing services stay private for the
same reason they always have: a consumer that could name them could pair.
"""

from app.modules.matchmaking.domain.pending_match import OpponentPreview, PendingMatchOffer

__all__ = ["OpponentPreview", "PendingMatchOffer"]
