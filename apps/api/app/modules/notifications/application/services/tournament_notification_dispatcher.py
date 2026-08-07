"""`TournamentNotificationDispatcher` — three tournament facts, told to the
people they happened to. A64-021.4 §11, §15, §17.

The second outbox consumer this module owns, and it is deliberately shaped
like the first: it subscribes to a set of event types, resolves an audience
through a **published** port, composes records, and hands them to the durable
writer. What it does *not* share with `SocialNotificationDispatcher` is the
profile gate — a tournament has no subject whose privacy settings could hide
it, so there is nothing to render through `PublicProfileComposer`.

## Three types, and why the other candidates are not here

    tournament.player_registered  -> tournament_registration_confirmed
    tournament.round_published    -> tournament_round_published
    tournament.completed          -> tournament_completed

`tournament.cancelled` is declared and **never published** — no application
service emits it — so a consumer for it would be an entry point nothing
reaches. `tournament.started`, `registration_opened` and `registration_closed`
say nothing a player needs told that the round publication does not; a
tournament starting *is* its first round being paired, from the point of view
of somebody waiting to play.

A match becoming ready is the one genuinely missing type, and it is missing
for a reason worth stating: there is no event. Matches are launched by
`TournamentMatchLauncher`, which holds no publisher, and `game`'s
`MatchActivated` carries no `origin`, so a consumer could not tell a
tournament fixture from a queue pairing. `specs/notifications.md` §14 records
the seam.

## Fan-out is one query per event, never one per recipient

A round publication in a 128-player tournament is one `TournamentAudience`
read, one preference read inside the writer, and one insert per recipient.
The inserts are per-row because each is a different row; everything that
could have been per-recipient is not. §7's rule, and the one that only shows
up in production — a two-player test tournament makes an N+1 invisible.

## Exactly-once needs nothing new

`UNIQUE(recipient_id, source_event_id, type)`. Every recipient of one round
publication shares that publication's outbox id, and they differ by
recipient, so a redelivered event inserts nothing for anybody. That is the
same mechanism A64-021.1 built, working unchanged at 128 rows instead of 1.

## What a failure does

A tournament this consumer cannot read is **skipped, not failed**: a
completion event for a tournament that no longer exists will never succeed,
and retrying it forever would hold the relay's backlog open on a fact about
something gone. Standings that are not yet visible are the opposite — that
is a genuine transient, so the entry fails and is retried.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from app.core.identifiers import generate_uuid7
from app.modules.notifications.application.ports import DurableNotificationStore
from app.modules.notifications.domain.record import (
    CATEGORY_OF,
    NavigationTarget,
    NavigationTargetType,
    NotificationRecord,
    NotificationType,
    TournamentSummary,
)
from app.modules.tournament.public import (
    PlayerRegistered,
    RoundPublished,
    TournamentCompleted,
    TournamentNotificationReader,
)
from app.platform.outbox import OutboxEntry

logger = logging.getLogger(__name__)

#: This consumer's name in `platform.processed_event`.
#:
#: Its **own** partition, separate from `social_notifications`. Two consumers
#: sharing a ledger entry would mean a redelivery one had handled being
#: marked done for the other — the arrangement every relay consumer on this
#: platform already avoids.
CONSUMER_NAME: Final = "tournament_notifications"

#: The event types this consumer subscribes to. Built from the classes so a
#: renamed event fails to import rather than silently never matching.
SUBSCRIBED_EVENT_TYPES: frozenset[str] = frozenset(
    {
        PlayerRegistered.event_type,
        RoundPublished.event_type,
        TournamentCompleted.event_type,
    }
)


@dataclass(frozen=True, slots=True)
class _Failed:
    """One entry this consumer could not process — `outbox.ports.EventFailure`."""

    entry_id: UUID
    reason: str


class TournamentNotificationDispatcher:
    """Turns tournament events into durable notifications.

    Holds two ports and nothing else: `tournament`'s published reader and
    somewhere durable to put the result. No repository of its own, no clock —
    every instant it needs is on the event, which is what makes it correct to
    run minutes after the fact.
    """

    def __init__(
        self,
        *,
        tournaments: TournamentNotificationReader,
        store: DurableNotificationStore,
    ) -> None:
        self._tournaments = tournaments
        self._store = store

    @property
    def consumer(self) -> str:
        return CONSUMER_NAME

    def handles(self, event_type: str) -> bool:
        return event_type in SUBSCRIBED_EVENT_TYPES

    async def handle(self, entries: Sequence[OutboxEntry]) -> Sequence[_Failed]:
        """Delivers a batch, entry by entry.

        **Per entry rather than per batch**, so one tournament that cannot
        be read does not stop notifications for a different one in the same
        tick. That is the granularity the outbox ledger records at, so it is
        the granularity a failure should have.
        """
        failures: list[_Failed] = []
        for entry in entries:
            try:
                records = await self._records_for(entry)
            except _Transient as transient:
                failures.append(_Failed(entry_id=entry.id, reason=str(transient)))
                continue

            if not records:
                continue

            # Outside the `try` above on purpose: a storage failure is not a
            # composition failure, and wrapping it here would report "could
            # not read the tournament" for a database that went away.
            await self._store.store(records)
            logger.info(
                "tournament_notifications_composed",
                extra={
                    "source_event_type": entry.event_type,
                    "notification_type": records[0].type.value,
                    "recipients": len(records),
                },
            )
        return failures

    async def _records_for(self, entry: OutboxEntry) -> list[NotificationRecord]:
        if entry.event_type == PlayerRegistered.event_type:
            return await self._registration(entry)
        if entry.event_type == RoundPublished.event_type:
            return await self._round_published(entry)
        return await self._completed(entry)

    async def _registration(self, entry: OutboxEntry) -> list[NotificationRecord]:
        """One recipient: the player who entered — §14.

        The **only** type here that reads nothing back. `PlayerRegistered`
        carries the tournament's name and the player's id, which is the
        whole notification — and a receipt that had to look the tournament
        up would be a receipt that stopped working the day one was deleted.
        """
        tournament_id = _uuid(entry.payload, "tournament_id")
        player_id = _uuid(entry.payload, "player_id")
        name = _text(entry.payload, "name")

        return [
            _record(
                entry,
                recipient_id=player_id,
                type_=NotificationType.TOURNAMENT_REGISTRATION_CONFIRMED,
                payload=TournamentSummary(tournament_id=tournament_id, tournament_name=name),
            )
        ]

    async def _round_published(self, entry: OutboxEntry) -> list[NotificationRecord]:
        """Every live entrant — §15.

        The audience is read at **delivery** time, not carried on the event,
        which is the same rule preferences follow: somebody who withdrew
        between the publication and this tick has withdrawn, and the read
        excludes them by predicate rather than by a filter here.
        """
        tournament_id = _uuid(entry.payload, "tournament_id")
        round_number = _integer(entry.payload, "round_number")

        audience = await self._tournaments.audience_of(tournament_id)
        if audience is None:
            logger.warning(
                "tournament_notification_skipped",
                extra={"source_event_type": entry.event_type, "reason": "tournament_absent"},
            )
            return []

        summary = TournamentSummary(
            tournament_id=tournament_id,
            tournament_name=audience.name,
            round_number=round_number,
        )
        return [
            _record(
                entry,
                recipient_id=recipient,
                type_=NotificationType.TOURNAMENT_ROUND_PUBLISHED,
                payload=summary,
            )
            # Sorted so a batch's insert order is deterministic — a failing
            # test names the same recipient twice in a row rather than a
            # different one each run.
            for recipient in sorted(audience.participant_ids)
        ]

    async def _completed(self, entry: OutboxEntry) -> list[NotificationRecord]:
        """Everybody with a standing, each told their own placement — §17.

        Recipients come from the **standings**, not the registrations: a
        player who withdrew before the field was fixed has no result, and
        telling them where they did not place would be worse than silence.

        `final_rank` is the one recipient-specific field this platform
        stores, and it is why the payload is composed per recipient rather
        than once. Ranks are passed through exactly as recorded — ties share
        a rank and gaps are real (`specs/tournament.md` §6f).
        """
        tournament_id = _uuid(entry.payload, "tournament_id")

        results = await self._tournaments.results_of(tournament_id)
        if results is None:
            # **Transient, deliberately.** Absent standings mean either a
            # tournament that is gone or one whose completion transaction is
            # not visible yet, and the second is a retry. Failing both is the
            # safe direction: a retry that never succeeds is bounded by the
            # relay's attempt limit, where a skipped completion is a result
            # nobody is ever told.
            raise _Transient(f"no standings for tournament {tournament_id}")

        return [
            _record(
                entry,
                recipient_id=recipient,
                type_=NotificationType.TOURNAMENT_COMPLETED,
                payload=TournamentSummary(
                    tournament_id=tournament_id,
                    tournament_name=results.name,
                    final_rank=rank,
                ),
            )
            for recipient, rank in sorted(results.final_rank_by_player.items())
        ]


class _Transient(Exception):
    """A composition that should be retried rather than skipped."""


def _record(
    entry: OutboxEntry,
    *,
    recipient_id: UUID,
    type_: NotificationType,
    payload: TournamentSummary,
) -> NotificationRecord:
    """One durable row.

    `created_at` is the **event's** instant, never now: a relay catching up
    after an outage must not tell a player that a round published an hour
    ago was paired just now.
    """
    return NotificationRecord(
        id=generate_uuid7(),
        recipient_id=recipient_id,
        type=type_,
        category=CATEGORY_OF[type_],
        payload=payload,
        target=NavigationTarget(
            type=NavigationTargetType.TOURNAMENT,
            ref=str(payload.tournament_id),
        ),
        source_event_id=entry.id,
        created_at=entry.occurred_at,
    )


def _uuid(payload: Mapping[str, object], key: str) -> UUID:
    """A required id off a stored payload.

    A malformed payload raises `ValueError`, which the relay records as a
    failed entry — the correct outcome for a producer that changed its
    contract without telling this consumer. §13's "prefer failing the batch
    when data integrity is wrong."
    """
    return UUID(str(payload[key]))


def _text(payload: Mapping[str, object], key: str) -> str:
    return str(payload[key])


def _integer(payload: Mapping[str, object], key: str) -> int:
    return int(str(payload[key]))


__all__ = [
    "CONSUMER_NAME",
    "SUBSCRIBED_EVENT_TYPES",
    "TournamentNotificationDispatcher",
]
