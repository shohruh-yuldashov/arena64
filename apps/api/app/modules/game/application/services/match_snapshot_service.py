"""`GameMatchSnapshot` — the live snapshot, built from the durable log.
A64-016.6 §1.

Replays the match and projects the result. Twelve lines of body, because
everything it needs already exists: `PersistedMatchReplay` builds the
`ReplayData`, `ReplayEngine` plays it, and the projection is a rename.

## Why it replays rather than reading the cache

The Redis live position is a cache (A64-016.4), and a snapshot is precisely
the thing a client asks for when it has fallen behind — which is
disproportionately often the same moment the platform itself has just
restarted and the cache is cold. A snapshot built from the cache would be
fastest exactly when it is least likely to exist.

The durable log always answers, and the replay is the same code path a live
move takes, so a snapshot cannot disagree with the game it describes.

## Why the clock comes from the match row and not the replay

`ReplayEngine` reconstructs a position; it knows nothing about time, and it
should not — a replay must produce the same result whether the game took two
minutes or two days. The clock is authoritative on the match row, written by
the move transaction, and is read straight from there.
"""

import logging
from datetime import datetime
from uuid import UUID

from app.core.clock import Clock
from app.modules.engine import PlayerSide
from app.modules.game.application.ports import MatchRecordRepository
from app.modules.game.application.services.match_replay_service import (
    PersistedMatchReplay,
)
from app.modules.game.domain.clock import ClockState
from app.modules.game.domain.match_record import MatchRecord, SeatRating
from app.modules.game.domain.replay import ReplayEngine
from app.modules.game.public.matches import SeatRating as PublishedSeatRating
from app.modules.game.public.moves import ClockView
from app.modules.game.public.snapshots import DrawOfferState, MatchSnapshot, PlacedPiece

logger = logging.getLogger(__name__)


class GameMatchSnapshot:
    """`MatchSnapshotReader` over the durable log and the match row."""

    def __init__(
        self,
        *,
        matches: MatchRecordRepository,
        replays: PersistedMatchReplay,
        engine: ReplayEngine,
        clock: Clock,
    ) -> None:
        self._matches = matches
        self._replays = replays
        self._engine = engine
        self._clock = clock

    async def snapshot_of(self, match_id: UUID) -> MatchSnapshot | None:
        """The match's current state, or `None` if there is no such match."""
        record = await self._matches.by_id(match_id)
        if record is None:
            return None

        data = await self._replays.replay_data(match_id)
        if data is None:  # pragma: no cover — the record exists, so this does
            return None

        aggregate = self._engine.replay(data)
        result = record.result
        observed_at = self._clock.now()

        return MatchSnapshot(
            match_id=record.id,
            engine_version=record.engine_version.number,
            variant=record.variant,
            status=record.status,
            sequence=record.ply_number,
            side_to_move=aggregate.position.side_to_move,
            fingerprint=aggregate.position.fingerprint,
            pieces=tuple(
                PlacedPiece(square=str(square), side=piece.side.value, rank=piece.rank.value)
                for square, piece in sorted(
                    aggregate.position.board.occupied_squares.items(),
                    key=lambda entry: entry[0],
                )
            ),
            rated=record.rated,
            light_player_id=record.light.player_id,
            dark_player_id=record.dark.player_id,
            # From the record already read above — no second query and no
            # read of `rating`. See `_published_rating`.
            light_rating=_published_rating(record.light.rating),
            dark_rating=_published_rating(record.dark.rating),
            clock=_clock_view(record.clock, at=observed_at),
            draw_offer=_draw_offer_state(record),
            # Both sides, because a snapshot has no viewer — see
            # `MatchSnapshot.may_offer_light`. Computed from the same
            # `DrawAgreement` the command path checks, so the button a
            # client renders and the rule that would refuse it cannot
            # disagree.
            may_offer_light=record.draw_agreement.may_offer(
                PlayerSide.LIGHT, at_ply=record.ply_number
            ),
            may_offer_dark=record.draw_agreement.may_offer(
                PlayerSide.DARK, at_ply=record.ply_number
            ),
            outcome=result.outcome if result is not None else None,
            termination_reason=result.reason if result is not None else None,
            winner=result.winner if result is not None else None,
            observed_at=observed_at,
        )


def _published_rating(seat: SeatRating | None) -> PublishedSeatRating | None:
    """The aggregate's seat snapshot as the published one — A64-025.6B.

    The inverse of `match_creation_service._seat_rating`, and it exists for
    the same reason: two identical shapes, one on the port and one in the
    domain, so neither side holds a type the other decides. The conversion
    is the boundary.

    Nothing is recomputed here. `is_provisional` in particular is the flag
    captured at creation rather than a fresh comparison against a threshold
    — the threshold is `rating`'s and may have moved since.
    """
    if seat is None:
        return None
    return PublishedSeatRating(
        value=seat.value,
        deviation=seat.deviation,
        volatility=seat.volatility,
        games_played=seat.games_played,
        is_provisional=seat.is_provisional,
        speed_class=seat.speed_class,
    )


def _draw_offer_state(record: MatchRecord) -> DrawOfferState | None:
    """The standing offer, or `None` — A64-020.5C-pre §9.

    Straight from the durable record, which is what makes reconnect
    recovery work without the client reconstructing anything: an offer made
    before a refresh is in the row, so it is in the snapshot.
    """
    offer = record.draw_agreement.offer
    if offer is None:
        return None
    return DrawOfferState(
        offered_by=offer.offered_by,
        offered_at_ply=offer.offered_at_ply,
        offered_at=offer.offered_at,
    )


def _clock_view(clock: ClockState | None, *, at: datetime) -> ClockView | None:
    """The stored clock as a client renders it — §7.

    Absolute, never relative: a duration re-based on receipt drifts by the
    network latency it was meant to describe, and a reconnecting client is
    exactly the one whose latency is unknown.
    """
    if clock is None:
        return None
    return ClockView(
        light_ms=clock.light_ms,
        dark_ms=clock.dark_ms,
        active_side=clock.active_side,
        deadline=clock.deadline(),
        server_time=at,
    )


__all__ = ["GameMatchSnapshot"]
