"""`GameOriginMatches` — what became of the matches another context asked for.

Implements `game.public.OriginMatchReader`, whose docstring records why the
question exists and why it is keyed on R-25's opaque `origin_ref` rather
than on a match id. Nothing here re-argues it.

What this file *does* own is the translation, and it is the reason the
adapter is not four lines: `MatchRecord` says where a match is with a
status, an outcome and a winner, and a reconciler needs the one answer those
three combine into. Doing that join here rather than publishing all three
means every caller cannot get it wrong in a different way — in particular
the join that matters, which is that a match with **no winner** is a draw or
an abandonment depending on its outcome, and treating the second as the
first records a game nobody played as a result.
"""

from collections.abc import Sequence
from uuid import UUID

from app.modules.game.application.ports import MatchRecordRepository
from app.modules.game.domain.match_record import MatchRecord, MatchRecordStatus
from app.modules.game.domain.result import MatchOutcome
from app.modules.game.domain.variants import MatchOrigin
from app.modules.game.public.reconciliation import OriginMatchOutcome, OriginMatchState


class GameOriginMatches:
    """The originating context's read, over one session."""

    def __init__(self, matches: MatchRecordRepository) -> None:
        self._matches = matches

    async def outcomes_for(
        self, origin_refs: Sequence[UUID], *, origin: MatchOrigin
    ) -> Sequence[OriginMatchOutcome]:
        """What became of the matches created for these references.

        **Propagates**, like `GamePairingSettlements` and unlike the reads
        that degrade: there is no safe default here. Guessing "no match"
        makes the caller create a second game for two players who already
        have one; guessing "unfinished" stalls whatever was waiting. Failing
        the tick and trying again is the only correct answer, and it is
        available only if this raises.
        """
        if not origin_refs:
            return ()

        records = await self._matches.by_origin_refs(origin_refs, origin=origin)
        return [
            _outcome(record, reference)
            for record in records
            # Narrowed here rather than asserted: the query matched on
            # `origin_ref`, so a `None` is unreachable, and a filter states
            # that without a runtime check that could fire in production.
            if (reference := record.origin_ref) is not None
        ]


def _outcome(record: MatchRecord, reference: UUID) -> OriginMatchOutcome:
    return OriginMatchOutcome(
        match_id=record.id,
        origin=record.origin,
        origin_ref=reference,
        state=_state(record),
        winner=record.winner,
        created_at=record.created_at,
        ended_at=record.ended_at,
    )


def _state(record: MatchRecord) -> OriginMatchState:
    """One of four, from three columns — see this module's docstring.

    Order matters. `LIVE` is asked first because a pending or active match
    has no outcome at all, and every question below it is about one.
    """
    if record.status in (MatchRecordStatus.PENDING_ACCEPTANCE, MatchRecordStatus.ACTIVE):
        return OriginMatchState.LIVE
    if record.outcome is MatchOutcome.WIN:
        return OriginMatchState.DECIDED
    if record.outcome is MatchOutcome.DRAW:
        return OriginMatchState.DRAWN

    # `MatchOutcome.NONE` — an aborted game — and every settled match that
    # never had an outcome at all: declined, expired. MT-11 keeps all of
    # them out of ratings and statistics, and they are one answer here for
    # the same reason: nothing was played.
    return OriginMatchState.ABANDONED


__all__ = ["GameOriginMatches"]
