"""`GameNotificationDispatcher` — a finished game, told to the two people who
played it. A64-021.4 §11, §19.

## Why a completed game is worth a durable row at all

Both participants usually watched it end, and for them this is a row they
will scroll past. It exists for the times nobody was looking:

    a tournament no-show adjudication ends a match the player never opened
    a clock expiry ends one on a tab that was closed
    an abandonment ends one after somebody's connection went

In every one of those the live result screen reached nobody, and this is the
only notice there is. That is the test §19 sets — *does it add value beyond
the live result screen* — and the answer is yes for exactly the cases where
the live screen was not there.

## Only a game that was played produces a row

`_parse` recognises **`win` with a named winner, and `draw`**, and produces
nothing for anything else. That is an allow-list, matching what
`statistics.match_projection_service` decided for the same payload and for
the same reason: an aborted match is `outcome=none` — MT-11's "a match that
did not happen" — and a permanent record saying somebody's game finished
would describe a non-event.

Written as an allow-list rather than `if outcome != "none"`, so a value this
build does not know produces silence rather than a guess. A future outcome
would otherwise be told to two people as whichever sentence happened to be
the fallback.

## The outcome is resolved per recipient, before it is stored

The event says `outcome=win, winner=light`. The row says `win` for the light
player and `loss` for the dark one. Doing that here rather than on the
client is what keeps the frontend from needing the seats — and the seats are
two more player ids in a payload about one game.

## What it reads

One batch profile render, for the two opponents, through the same privacy
gate every other surface uses. A player with no public profile any more
renders as absent rather than blank, and the notification keeps the game
while losing the name — which is the honest shape: the game was played, the
account is gone.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

from app.core.identifiers import generate_uuid7
from app.modules.game.public import MatchCompleted
from app.modules.notifications.application.ports import DurableNotificationStore
from app.modules.notifications.domain.record import (
    CATEGORY_OF,
    ActorSummary,
    GameResultSummary,
    NavigationTarget,
    NavigationTargetType,
    NotificationRecord,
    NotificationType,
)
from app.modules.profiles.public import ProfileRenderer, PublicProfile
from app.modules.users.public import ViewerRelationship
from app.platform.outbox import OutboxEntry

logger = logging.getLogger(__name__)

#: This consumer's own `platform.processed_event` partition.
CONSUMER_NAME: Final = "game_notifications"

#: One event type, and that is the whole subscription. `match_created`,
#: `match_activated` and `match_declined` are all about a pairing that
#: resolves in under a minute — a durable row for one would be an inbox entry
#: that is already stale when it is read (§3).
SUBSCRIBED_EVENT_TYPES: frozenset[str] = frozenset({MatchCompleted.event_type})

#: `(outcome, winner)` on the event -> what light is told, what dark is told.
#:
#: An exhaustive table rather than nested conditionals: the three things a
#: pair of players can be told are enumerable at a glance, and a payload that
#: matches no key produces no notification rather than falling through to a
#: default that would tell somebody they won.
_TOLD: Final[Mapping[tuple[str, str | None], tuple[str, str]]] = {
    ("win", "light"): ("win", "loss"),
    ("win", "dark"): ("loss", "win"),
    ("draw", None): ("draw", "draw"),
}


@dataclass(frozen=True, slots=True)
class _Failed:
    entry_id: UUID
    reason: str


@dataclass(frozen=True, slots=True)
class _Finished:
    """One completed match, as this consumer needs it."""

    match_id: UUID
    light_id: UUID
    dark_id: UUID
    light_outcome: str
    dark_outcome: str
    termination_reason: str

    def seats(self) -> tuple[tuple[UUID, UUID, str], ...]:
        """`(recipient, opponent, outcome)` for both players."""
        return (
            (self.light_id, self.dark_id, self.light_outcome),
            (self.dark_id, self.light_id, self.dark_outcome),
        )


class GameNotificationDispatcher:
    """Turns finished games into durable notifications for their players."""

    def __init__(
        self,
        *,
        profiles: ProfileRenderer,
        store: DurableNotificationStore,
    ) -> None:
        self._profiles = profiles
        self._store = store

    @property
    def consumer(self) -> str:
        return CONSUMER_NAME

    def handles(self, event_type: str) -> bool:
        return event_type in SUBSCRIBED_EVENT_TYPES

    async def handle(self, entries: Sequence[OutboxEntry]) -> Sequence[_Failed]:
        """Delivers a batch.

        **One profile render for the whole batch**, not one per match: a
        relay tick that carries twenty completions renders forty players in
        one call, which is the difference between a query and forty on the
        busiest event type this platform has (§27).
        """
        finished = [(entry, parsed) for entry in entries if (parsed := _parse(entry)) is not None]
        if not finished:
            return []

        # `STRANGER` is the correct relationship and not a simplification:
        # the two people are opponents, not friends, and rendering them as
        # anything closer would show a field the subject chose to show only
        # to friends. If they *are* friends, the friends-only fields are on
        # their profile page — a notification is not the surface to widen.
        profiles = await self._profiles.render_many(
            sorted({player for _, match in finished for player in (match.light_id, match.dark_id)}),
            relationship=ViewerRelationship.STRANGER,
        )

        records = [
            record for entry, match in finished for record in _records_for(entry, match, profiles)
        ]

        await self._store.store(records)
        logger.info(
            "game_notifications_composed",
            extra={"matches": len(finished), "recipients": len(records)},
        )
        return []


def _records_for(
    entry: OutboxEntry,
    match: _Finished,
    profiles: Mapping[UUID, PublicProfile],
) -> list[NotificationRecord]:
    return [
        NotificationRecord(
            id=generate_uuid7(),
            recipient_id=recipient,
            type=NotificationType.GAME_COMPLETED,
            category=CATEGORY_OF[NotificationType.GAME_COMPLETED],
            payload=GameResultSummary(
                match_id=match.match_id,
                outcome=outcome,
                termination_reason=match.termination_reason,
                opponent=_actor(profiles.get(opponent)),
            ),
            # **The replay, not the live board.** By the time anybody taps
            # this the game is over, and `/games/{id}` would open a room
            # that has nothing to show.
            target=NavigationTarget(
                type=NavigationTargetType.MATCH_REPLAY, ref=str(match.match_id)
            ),
            source_event_id=entry.id,
            created_at=entry.occurred_at,
        )
        for recipient, opponent, outcome in match.seats()
    ]


def _parse(entry: OutboxEntry) -> _Finished | None:
    """One completion event as this consumer needs it, or `None` to skip.

    `None` for the three cases that are correct to ignore rather than retry:

        an outcome nobody was told                an abort, or a value this
                                                  build does not recognise
        a match with no seats                     predates A64-017.2's
                                                  snapshots, so the payload
                                                  names no players
        a `winner` that is not a side             `MatchResult`'s invariant
                                                  and a database `CHECK` both
                                                  forbid it; a producer that
                                                  broke them is not something
                                                  to guess around

    Skipped rather than failed, in all three. A retry cannot change any of
    them, and an entry that fails forever holds the relay's backlog open.
    """
    payload: Mapping[str, Any] = entry.payload

    told = _TOLD.get((str(payload.get("outcome")), _optional_text(payload.get("winner"))))
    if told is None:
        return None

    light, dark = payload.get("light"), payload.get("dark")
    if not isinstance(light, Mapping) or not isinstance(dark, Mapping):
        logger.warning(
            "game_notification_skipped",
            extra={"source_event_type": entry.event_type, "reason": "no_seats"},
        )
        return None

    return _Finished(
        match_id=UUID(str(payload["match_id"])),
        light_id=UUID(str(light["player_id"])),
        dark_id=UUID(str(dark["player_id"])),
        light_outcome=told[0],
        dark_outcome=told[1],
        termination_reason=str(payload["termination_reason"]),
    )


def _optional_text(value: Any) -> str | None:
    return None if value is None else str(value)


def _actor(profile: PublicProfile | None) -> ActorSummary | None:
    if profile is None:
        return None
    identity = profile.identity
    return ActorSummary(
        player_id=identity.id,
        username=identity.username,
        display_name=identity.display_name,
        avatar_object_key=identity.avatar.object_key,
        avatar_version=identity.avatar.version,
    )


__all__ = [
    "CONSUMER_NAME",
    "SUBSCRIBED_EVENT_TYPES",
    "GameNotificationDispatcher",
]
