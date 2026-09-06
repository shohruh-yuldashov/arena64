"""Whether this instance wants traffic — A64-028.6 §9 and §11.

## The gap this closes

A64-028.1 filed two findings that are really one. P1-5: `/health/ready`
returned HTTP 200 with `status: "degraded"` when PostgreSQL and Redis were
both unreachable, so a load balancer — which reads the status line, not the
body — kept a database-less instance in rotation. P1-6: a deploy severed
every live game, because there was no moment at which an instance could
say "stop sending me new work" before it was signalled.

Both need the same missing thing: a readiness answer that can be **no**.

## Why draining is a request and not a signal handler

The obvious implementation is to flip this on `SIGTERM`. It does not work,
and A64-028.4 already found out why: uvicorn closes the listening socket
and every WebSocket **before** the lifespan teardown runs, so by the time
application code hears about the signal the connections are gone and the
load balancer has not yet been told anything. Installing our own handler
would mean replacing uvicorn's, which breaks its shutdown.

So draining is a **deliberate step in the deploy**, in the order an
orchestrator already supports:

    POST /health/drain   →  readiness turns 503
                         →  the balancer stops routing new requests
                         →  a bounded settling period
    SIGTERM              →  uvicorn closes sockets, lifespan tears down

That is a `preStop` hook in Kubernetes and a script line everywhere else.
An unplanned exit — a crash, an OOM kill — skips it, which is correct:
there is nothing to co-ordinate with a process that is already gone, and
the durable move log plus `game.resume` is what makes that survivable.

## What draining does *not* do

It does not close anything, refuse anything, or abandon anything in
progress. An instance that is draining serves every request it is still
given, exactly as before — the only thing that changed is what it says
when asked whether it wants more. Refusing work while the balancer is
still routing it would turn a graceful deploy into an outage.
"""

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class ServiceLifecycle:
    """One flag, owned by the process, read by readiness.

    Mutable by design and by exactly one method — this is the one piece of
    request-scoped-adjacent state that is genuinely per **process**, and
    `CLAUDE.md` §2.1's "no hidden global state" is satisfied by it being an
    injected object rather than a module variable.
    """

    draining: bool = field(default=False)

    def begin_drain(self) -> bool:
        """Marks this instance as not wanting new traffic.

        Idempotent, and says whether it changed anything — a deploy that
        calls it twice is normal (a retried hook), and an operator reading
        the response should be able to tell a first call from a repeat.
        """
        if self.draining:
            return False
        self.draining = True
        logger.warning("instance_draining", extra={"draining": True})
        return True


__all__ = ["ServiceLifecycle"]
