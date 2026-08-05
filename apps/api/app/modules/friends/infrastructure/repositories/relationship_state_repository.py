"""`RelationshipStateReader` over the three social relations — A64-020.4.

## One statement, whatever the page size

Three relations answer this question — a block, a live friendship, a
pending request — and the obvious implementation reads each in turn. It
would be three statements, which is fixed and therefore acceptable; this is
one, which is better for the same reason `friend_ids_among` is one: it runs
on the profile composition path, so it is on every rendered page on the
platform.

A `UNION ALL` of the three legs, each tagged with the state it produces and
projected to the *other* player's id. Every leg is an index lookup on its
own relation, and nothing is joined:

    blocked_player   (blocker_id, blocked_id)     -> BLOCKED
    friendship       canonical pair, live only    -> FRIEND
    friend_request   pending, either direction    -> INCOMING / OUTGOING

Precedence is applied in Python afterwards, over at most a handful of rows
per player, because it is a rule about *meaning* rather than about data —
see `RelationshipState`. Expressing it in SQL would put a product decision
into a `CASE` nobody reads.

## What this must never return

`blocked-by-target`. The block leg filters `blocker_id = viewer` and
nothing else, so a block placed **on** the viewer produces no row and the
pair reads as whatever the other relations say. That is BL-1 working: a
blocked player must not be able to tell.
"""

import logging
from collections.abc import Mapping, Sequence
from uuid import UUID

from sqlalchemy import Select, case, literal, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.friends.domain.friend_request import FriendRequestStatus
from app.modules.friends.infrastructure.models import (
    BlockedPlayerModel,
    FriendRequestModel,
    FriendshipModel,
)
from app.modules.users.public import RelationshipState

logger = logging.getLogger(__name__)

#: The tags the three legs emit, and the order they win in. Mirrors
#: `RelationshipState`'s documented precedence; kept as an explicit tuple so
#: the resolution is a lookup rather than a chain of `if`s that a sixth
#: state would silently fall through.
_PRECEDENCE: tuple[RelationshipState, ...] = (
    RelationshipState.BLOCKED,
    RelationshipState.FRIEND,
    RelationshipState.INCOMING_REQUEST,
    RelationshipState.OUTGOING_REQUEST,
)


class SqlAlchemyRelationshipStateReader:
    """`friends.public.RelationshipStateReader` over one session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def relationship_states_for(
        self, viewer_id: UUID, player_ids: Sequence[UUID]
    ) -> Mapping[UUID, RelationshipState]:
        """One state per id — see this module's docstring."""
        wanted = [player_id for player_id in player_ids if player_id != viewer_id]
        if not wanted:
            # Also the self-profile case: a viewer is not a relationship
            # with themselves, and the composer omits the field entirely.
            return {}

        rows = (
            await self._session.execute(
                union_all(
                    _blocked_leg(viewer_id, wanted),
                    _friend_leg(viewer_id, wanted),
                    _request_leg(viewer_id, wanted),
                )
            )
        ).all()

        found: dict[UUID, RelationshipState] = {}
        for tag, other in rows:
            state = RelationshipState(tag)
            current = found.get(other)
            if current is None or _PRECEDENCE.index(state) < _PRECEDENCE.index(current):
                found[other] = state

        # Complete by construction: every id asked for gets an entry, so a
        # caller indexes rather than writing `.get(id) or NONE` at each site.
        return {player_id: found.get(player_id, RelationshipState.NONE) for player_id in wanted}


def _blocked_leg(viewer_id: UUID, player_ids: Sequence[UUID]) -> Select[tuple[str, UUID]]:
    """Blocks the **viewer placed**. Never the other direction — BL-1."""
    return select(
        literal(RelationshipState.BLOCKED.value).label("state"),
        BlockedPlayerModel.blocked_id.label("other"),
    ).where(
        BlockedPlayerModel.blocker_id == viewer_id,
        BlockedPlayerModel.blocked_id.in_(player_ids),
    )


def _friend_leg(viewer_id: UUID, player_ids: Sequence[UUID]) -> Select[tuple[str, UUID]]:
    """Live friendships, projected to whichever side is not the viewer.

    The pair is stored canonically (`low < high`, DB-12), so the viewer is
    on one side or the other and the `CASE` picks the opposite one.
    """
    return select(
        literal(RelationshipState.FRIEND.value).label("state"),
        case(
            (FriendshipModel.player_low_id == viewer_id, FriendshipModel.player_high_id),
            else_=FriendshipModel.player_low_id,
        ).label("other"),
    ).where(
        FriendshipModel.ended_at.is_(None),
        (
            (FriendshipModel.player_low_id == viewer_id)
            & (FriendshipModel.player_high_id.in_(player_ids))
        )
        | (
            (FriendshipModel.player_high_id == viewer_id)
            & (FriendshipModel.player_low_id.in_(player_ids))
        ),
    )


def _request_leg(viewer_id: UUID, player_ids: Sequence[UUID]) -> Select[tuple[str, UUID]]:
    """Pending requests, in both directions and distinguished by sender.

    The direction **is** the state: a request the viewer sent is something
    to cancel, one they received is something to accept. Reading only one
    leg would render "add friend" beside a request already waiting.
    """
    return select(
        case(
            (
                FriendRequestModel.requester_id == viewer_id,
                literal(RelationshipState.OUTGOING_REQUEST.value),
            ),
            else_=literal(RelationshipState.INCOMING_REQUEST.value),
        ).label("state"),
        case(
            (FriendRequestModel.requester_id == viewer_id, FriendRequestModel.addressee_id),
            else_=FriendRequestModel.requester_id,
        ).label("other"),
    ).where(
        FriendRequestModel.status == FriendRequestStatus.PENDING,
        (
            (FriendRequestModel.requester_id == viewer_id)
            & (FriendRequestModel.addressee_id.in_(player_ids))
        )
        | (
            (FriendRequestModel.addressee_id == viewer_id)
            & (FriendRequestModel.requester_id.in_(player_ids))
        ),
    )


__all__ = ["SqlAlchemyRelationshipStateReader"]
