"""Which gateway process this is — A64-016.2 §3.

A64-016.1's registry recorded *that* a player had a connection. It could not
record **where**, so nothing could route a message to the socket holding it,
and A64-016.1's own known-gaps list named this as the shape change that would
be needed. A node identifier is the missing half.

## The three properties §3 asks for, and how each is held

    configured at startup    `GATEWAY_NODE_ID`, read once through the
                             cached settings
    stable for the process   `lru_cache(maxsize=1)` — the same string for
                             every connection this process ever accepts
    never client-facing      no message type carries it, and
                             `GatewayMessage` has no field it could land in

The third is the one worth stating loudly. A node identifier is internal
topology: publishing it tells a client which process holds its socket, which
is the first thing anybody mapping a fleet wants and is of no use whatever to
a browser. It is likewise absent from every metric label (§11) — one time
series per node is a cardinality that grows with the fleet — while being
present in *logs*, where correlating an incident to a process is the whole
job.

## Why a generated fallback, and why that is not "implicitly the hostname"

§3 forbids using the hostname implicitly. This does not: with nothing
configured it draws a random identifier once per process. That is honest
about what it knows — a process that was not told who it is has no basis for
claiming a name that means something to an operator — and it is *correct*
for the registry, because the identity that matters there is "this process
instance", which is exactly what a restart should change.

What a deployment loses by not setting it is **legibility**, not
correctness: a route resolving to `d4f1a2b8` says the connection is
elsewhere just as well as `gateway-3` does, but only the second can be found
in a dashboard. So the fallback keeps local development working with no
configuration, and `.env.example` says to set it in a real deployment.
"""

import secrets
from functools import lru_cache
from typing import Final

from app.config.settings import GatewaySettings

#: How many bytes of randomness a generated identifier carries.
#:
#: Four is not a security parameter — nothing authenticates on a node id.
#: It is a collision parameter, and the population is the number of gateway
#: processes alive at one time (tens). Eight hex characters is short enough
#: to read in a log line and wide enough that two live nodes colliding is
#: not a thing that happens.
_GENERATED_ID_BYTES: Final = 4

#: The longest identifier this platform will accept.
#:
#: A bound because the value is written into **every connection member** in
#: the registry (`gwconn:v2:`), so its length is multiplied by the number of
#: live sockets. Thirty-two characters is a generous name and a bounded key.
MAX_NODE_ID_LENGTH: Final = 32


@lru_cache(maxsize=1)
def _generated_node_id() -> str:
    """One identifier per process, drawn once.

    Cached rather than assigned to a module global, for the reason
    `engine_services` gives: the sharing is explicit at the call site and a
    test can clear it. Clearing it here means "pretend this is a different
    process", which is exactly what a two-node routing test wants.
    """
    return secrets.token_hex(_GENERATED_ID_BYTES)


def resolve_node_id(settings: GatewaySettings) -> str:
    """This process's node identifier.

    Configured wins; otherwise the generated one. Validated rather than
    trusted, because an over-long or blank value is a configuration mistake
    whose symptom is a registry full of unusable routes discovered days
    later — the same posture `GatewaySettings` takes with its three timers.
    """
    configured = (settings.node_id or "").strip()
    if not configured:
        return _generated_node_id()

    if len(configured) > MAX_NODE_ID_LENGTH:
        raise ValueError(
            f"GATEWAY_NODE_ID must be at most {MAX_NODE_ID_LENGTH} characters — "
            "it is written into every connection record in the registry"
        )
    if _SEPARATOR in configured:
        # `gwconn:v2:` stores `connection_id|node_id` in one member, so a
        # separator inside the name would make the member ambiguous to
        # parse. Refused at startup rather than producing routes that
        # decode to the wrong node.
        raise ValueError(f"GATEWAY_NODE_ID must not contain {_SEPARATOR!r}")

    return configured


#: The character that separates a connection id from its node in a registry
#: member. Declared here rather than in `registry.py` because it is the
#: reason `resolve_node_id` rejects a name containing it, and a constraint
#: whose reason lives in another module is one somebody relaxes.
_SEPARATOR: Final = "|"


def member_separator() -> str:
    """The registry's member separator. See `_SEPARATOR`."""
    return _SEPARATOR


__all__ = ["MAX_NODE_ID_LENGTH", "member_separator", "resolve_node_id"]
