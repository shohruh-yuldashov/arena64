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
from uuid import UUID

from app.modules.game.public import DrawOfferState, DrawOfferView, MatchSnapshot


def spectator_snapshot_payload(snapshot: MatchSnapshot) -> dict[str, Any]:
    """The snapshot an **audience** may see — A64-020.5C-pre §8, §9.

    Everything a participant gets except the draw negotiation. A separate
    function rather than a flag, because a flag defaults to something and
    the default that leaks is the one that ships: with two functions, a
    caller has to name which audience it is serving, and the spectator one
    is physically incapable of carrying an offer.

    That is the same allowlist direction `SPECTATOR_SAFE_EVENTS` takes, and
    it closes the other half of the hole: withholding
    `game.draw.offered` from the live fan-out would achieve nothing if a
    viewer could read the same offer by joining and taking a snapshot.
    """
    return _base_payload(snapshot)


def participant_snapshot_payload(snapshot: MatchSnapshot, *, viewer: UUID) -> dict[str, Any]:
    """The snapshot a **participant** may see — §9.

    The base payload plus the draw agreement, resolved to this viewer:
    whether an offer stands, who made it, and which of the three actions
    they may take.

    The permissions are computed here rather than sent as raw facts,
    because the alternative is every client reimplementing "I may accept
    only if the offer is not mine" — and a client that got it backwards
    would show an accept button that the server refuses.

    **No thresholds and no bookkeeping.** §9 forbids publishing the
    cooldown internals; `may_offer` is the answer, and the arithmetic
    behind it stays in `game`.
    """
    payload = _base_payload(snapshot)
    payload["draw"] = _draw_payload(snapshot, viewer=viewer)
    return payload


def draw_payload_for(
    *,
    offer: DrawOfferState | DrawOfferView | None,
    may_offer_light: bool,
    may_offer_dark: bool,
    side: str | None,
) -> dict[str, Any]:
    """One participant's draw agreement, from both sides' facts —
    A64-020.5D §11.

    **One resolver, three call sites**: the resume snapshot, the live
    `game.draw.state` frame after a command, and the one after a move. A
    second copy is how the button a client renders comes to disagree with
    the rule that would refuse it.

    `side` is `None` for a viewer who is neither seat, and everything then
    resolves to `False` — which is what makes a spectator projection safe
    by construction rather than by remembering. `spectator_snapshot_payload`
    does not call this at all, so that is belt and braces.

    Three booleans rather than one state string, because they are not
    mutually exclusive in the way a string would imply: a player with no
    offer standing may offer and may do nothing else, and a recipient may
    accept and decline but not offer.
    """
    may_offer = may_offer_light if side == "light" else may_offer_dark if side == "dark" else False
    is_recipient = offer is not None and side is not None and offer.offered_by.value != side

    return {
        "offer": (
            {
                "offered_by": offer.offered_by.value,
                "offered_at_ply": offer.offered_at_ply,
                "offered_at": offer.offered_at.isoformat(),
            }
            if offer is not None
            else None
        ),
        "may_offer": may_offer,
        "may_accept": is_recipient,
        "may_decline": is_recipient,
    }


def side_of(snapshot: MatchSnapshot, viewer: UUID) -> str | None:
    """Which seat this viewer holds, or `None` for anybody else."""
    if viewer == snapshot.light_player_id:
        return "light"
    if viewer == snapshot.dark_player_id:
        return "dark"
    return None


def _draw_payload(snapshot: MatchSnapshot, *, viewer: UUID) -> dict[str, Any]:
    """This viewer's draw-agreement state, from a snapshot.

    `viewer` is a participant by construction — the resume path proves it
    with `MatchSnapshot.includes` before projecting — so `side_of` cannot
    fall through to a stranger here.
    """
    return draw_payload_for(
        offer=snapshot.draw_offer,
        may_offer_light=snapshot.may_offer_light,
        may_offer_dark=snapshot.may_offer_dark,
        side=side_of(snapshot, viewer),
    )


def _base_payload(snapshot: MatchSnapshot) -> dict[str, Any]:
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
        "rated": snapshot.rated,
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


__all__ = [
    "draw_payload_for",
    "participant_snapshot_payload",
    "side_of",
    "spectator_snapshot_payload",
]
