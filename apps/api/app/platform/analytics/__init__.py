"""The analytics taxonomy, in code — A64-027.1.

**Vocabulary only.** No collector, no store, no consumer, no emitter: those
are A64-027.2's, and `docs/01-architecture/analytics.md` §45 is the contract
they implement. What lives here is the part of the architecture that has to
be enforceable rather than described — which event names exist, who owns
each one, whether it is a server fact or a browser's report, and which of
them a browser is allowed to submit.

## Why `app/platform` and not a module

Two different consumers need this vocabulary and neither may import the
other: the collector on the HTTP path (which must reject a client-submitted
server event) and the outbox consumer that projects domain events. A
vocabulary owned by whichever of them was written first would make the other
import it, which is the argument `app/platform/events` already makes for
`DomainEvent`.

Nothing here imports `app.modules`, and `.importlinter` fails if that
changes. That constraint is also why this module holds no domain enums: the
per-event **property** schemas validate against `MatchOutcome`,
`TerminationReason` and `SpeedClass` themselves (analytics.md §39), and they
belong wherever A64-027.2 puts the collector — a layer that may see a
module's `public/` surface.
"""

from app.platform.analytics.registry import (
    CLIENT_EMITTABLE,
    DENIED_PROPERTY_NAMES,
    REGISTRY,
    EventName,
    EventSpec,
    Owner,
    Trust,
    is_client_emittable,
    spec_for,
)

__all__ = [
    "CLIENT_EMITTABLE",
    "DENIED_PROPERTY_NAMES",
    "REGISTRY",
    "EventName",
    "EventSpec",
    "Owner",
    "Trust",
    "is_client_emittable",
    "spec_for",
]
