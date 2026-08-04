"""Projecting `game`'s published types onto the wire — A64-016.6, A64-016.7.

One module, because two projections of one state are two things to keep in
step and the one that drifts is the one used less. A resuming participant and
a joining spectator receive the **same** snapshot payload, which is §5's "do
not create a second broadcast pipeline" applied to the read side.

## Why the projection lives at the gateway

`protocol` knows nothing about `game` and must not — it is a codec.
`game.public` knows nothing about this wire format and must not — it serves
whatever transport asks. The gateway is where the two vocabularies meet,
which is what a transport adapter is for.

## What never crosses

No handles, no ratings, no email, no connection ids and no node identity.
Those are other modules' or are internal topology, and a snapshot that grew
them would make `game` depend on a module it has no business knowing about.
"""

from typing import Any

from app.modules.game.public import MatchSnapshot


def snapshot_payload(snapshot: MatchSnapshot) -> dict[str, Any]:
    """The published snapshot as wire primitives.

    Projected here rather than in `protocol`, which knows nothing about
    `game` and must not — and rather than in `game`, which knows nothing
    about this protocol. The gateway is where the two vocabularies meet,
    which is what a transport adapter is for.

    Carries **no player handles and no ratings**: those are `users`' and are
    composed by whoever renders them.
    """
    return {
        "match_id": str(snapshot.match_id),
        "engine_version": snapshot.engine_version,
        "variant": snapshot.variant.value,
        "status": snapshot.status.value,
        "sequence": snapshot.sequence,
        "side_to_move": snapshot.side_to_move.value,
        "fingerprint": snapshot.fingerprint,
        "pieces": [
            {"square": piece.square, "side": piece.side, "rank": piece.rank}
            for piece in snapshot.pieces
        ],
        "participants": {
            "light": str(snapshot.light_player_id),
            "dark": str(snapshot.dark_player_id),
        },
        "clock": _clock_payload(snapshot),
        "result": _result_payload(snapshot),
        "server_time": snapshot.observed_at.isoformat(),
    }


def _clock_payload(snapshot: MatchSnapshot) -> dict[str, Any] | None:
    """The authoritative clock — §7.

    Absolute instants, never durations. A reconnecting client is exactly the
    one whose latency is unknown, so a duration re-based on receipt would
    drift by the amount it was meant to describe — and §7 forbids the client
    extrapolating from stale values.
    """
    if snapshot.clock is None:
        return None
    return {
        "light_ms": snapshot.clock.light_ms,
        "dark_ms": snapshot.clock.dark_ms,
        "active_side": snapshot.clock.active_side.value,
        "deadline": snapshot.clock.deadline.isoformat(),
        "server_time": snapshot.clock.server_time.isoformat(),
    }


def _result_payload(snapshot: MatchSnapshot) -> dict[str, Any] | None:
    if snapshot.outcome is None or snapshot.termination_reason is None:
        return None
    return {
        "outcome": snapshot.outcome.value,
        "termination_reason": snapshot.termination_reason.value,
        "winner": snapshot.winner.value if snapshot.winner is not None else None,
    }


__all__ = ["snapshot_payload"]
