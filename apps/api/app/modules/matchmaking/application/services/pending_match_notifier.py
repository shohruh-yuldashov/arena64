"""`PendingMatchNotifier` — pushing a match offer instead of waiting for a
client to ask. A64-015.5 §4 and §6.

A64-015.4 shipped acceptance behind a poll: a client that had just been told
`404` by `GET /matchmaking/queue/me` had to notice, then ask
`GET /matchmaking/matches/pending`, and it had thirty seconds to do both. Its
own recommendations named this first — "push the pending match instead of
polling it… `game.match_created` already carries everything a push needs".

This is that consumer. It is an `EventHandler` on the transactional outbox,
so the path is the one §4 requires end to end:

    business transaction  the match and `game.match_created`, one commit
    outbox                the relay claims the entry, ledger-deduplicated
    this consumer         re-reads, authorises, renders
    PendingMatchSink      the gateway delivery port

## Nothing is trusted from the payload except identity

§6 is the whole design of `_offer_for`. The event was written when the match
was created; this runs after the relay has claimed it, which is a second
later on a healthy platform and can be far longer on an unhealthy one — and
thirty seconds is the entire lifetime of the thing being delivered. So every
question is asked **now**:

    still a participant   `pending_match(player_id)` is scoped to the
                          caller; a player who is not in the match gets
                          `None` and no offer is built for them
    still pending         the same read returns `None` once the match is
                          answered, declined or expired
    deadline not passed   compared against the clock, because a match can
                          be pending *and* out of time — the reconciler
                          has not reached it yet
    block state           re-read, and it withholds the opponent's name
                          rather than the offer

That is the same rule `SocialNotificationDispatcher` states — "do NOT trust
enqueue-time state" — applied to a payload with a much shorter shelf life.

## A block withholds the name, never the match

If a block appeared between the pairing and this delivery, the two are still
in a match that exists and still has a deadline. Withholding the *offer*
would leave a player holding a match they cannot see, which the deadline
would then expire against them; withholding the *name* costs them a face on
a card. So the offer is delivered with `opponent=None`, and the same is true
for an account deactivated in the window.

BL-2 already keeps blocked pairs from being paired at all, so this path is
reached only by a block created inside the window — rare, and exactly the
case §6 asks to be handled rather than assumed away.

## Idempotency, and why the sink may still see a duplicate

`platform.processed_event` stops a redelivered entry reaching this consumer
twice, which covers the ordinary case. What it cannot cover is a consumer
that delivered and then failed before the ledger committed — at-least-once
means the sink can see one offer twice, and §11 requires that to be safe.

It is, because **an offer is a statement rather than a command**: it says
"this match is pending, here is its deadline", which is either still true
(so re-delivering it is harmless) or no longer true (so this consumer will
not build it again). Nothing about it accumulates. The authoritative state
stays in `game.match`, and a client that missed the push entirely recovers
through the polling endpoint — which is what §5 keeps it for.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from app.core.clock import Clock
from app.modules.friends.public import PairingExclusions
from app.modules.game.public import (
    MatchAcceptanceUseCase,
    MatchCreated,
    MatchRecordStatus,
    PendingMatchView,
)
from app.modules.matchmaking.application.metrics import (
    PENDING_MATCH_DELIVERIES,
    DeliveryOutcome,
)
from app.modules.matchmaking.application.ports import PendingMatchSink
from app.modules.matchmaking.domain.pending_match import OpponentPreview, PendingMatchOffer
from app.modules.users.public import PublicProfileReader, PublicUserProfile
from app.platform.metrics import MetricsRecorder
from app.platform.outbox import OutboxEntry

logger = logging.getLogger(__name__)

#: This consumer's name in `platform.processed_event`.
#:
#: Renaming it re-delivers every retained `match_created` to the new name,
#: which for this consumer means pushing offers for matches that ended weeks
#: ago. Every one would be discarded by the staleness check below — which is
#: the check earning its place rather than a reason to be careless.
CONSUMER_NAME = "matchmaking_pending_match"

#: The one event this consumer subscribes to.
#:
#: `match_created` alone. An acceptance, a decline and an expiry are all
#: things a client learns from its own request or from the next push, and
#: subscribing to them would make this a general match-state feed — which is
#: the live-game protocol A64-015.5 excludes by name.
SUBSCRIBED_EVENT_TYPES: frozenset[str] = frozenset({MatchCreated.event_type})


@dataclass(frozen=True, slots=True)
class _Failed:
    """One entry this consumer could not process — `outbox.ports.EventFailure`."""

    entry_id: UUID
    reason: str


class PendingMatchNotifier:
    """Turns a created match into two delivered offers.

    Holds published ports and nothing private to another module: `game`'s
    acceptance read, `friends`' block read, `users`' profile read, the sink,
    a clock and a metrics recorder. It has no repository and no session of
    its own — every read goes through a contract, which is what lets it run
    minutes after the fact and still be correct about permissions.
    """

    def __init__(
        self,
        *,
        acceptance: MatchAcceptanceUseCase,
        exclusions: PairingExclusions,
        players: PublicProfileReader,
        sink: PendingMatchSink,
        clock: Clock,
        metrics: MetricsRecorder,
    ) -> None:
        self._acceptance = acceptance
        self._exclusions = exclusions
        self._players = players
        self._sink = sink
        self._clock = clock
        self._metrics = metrics

    @property
    def consumer(self) -> str:
        return CONSUMER_NAME

    def handles(self, event_type: str) -> bool:
        return event_type in SUBSCRIBED_EVENT_TYPES

    async def handle(self, entries: Sequence[OutboxEntry]) -> Sequence[_Failed]:
        """Delivers a batch of match offers. Returns the entries that failed.

        **Resolution is per event, delivery is per batch.** Each match needs
        its own re-read — there is no shared answer between two matches —
        but the profile lookup and the sink call are batched across the
        whole tick, which is what keeps a relay page of twenty matches at
        one profile query rather than forty.

        One entry failing does not fail the batch: the relay records it,
        backs off, and redelivers that entry alone.
        """
        failures: list[_Failed] = []
        resolved: list[tuple[OutboxEntry, PendingMatchView, UUID]] = []

        for entry in entries:
            try:
                resolved.extend(await self._pending_for(entry))
            except Exception as error:  # noqa: BLE001 — one event must not fail the batch
                logger.warning(
                    "pending_match_resolution_failed",
                    extra={"event_id": str(entry.id), "error": type(error).__name__},
                    exc_info=error,
                )
                failures.append(_Failed(entry_id=entry.id, reason=type(error).__name__))

        if not resolved:
            return failures

        try:
            offers = await self._render(resolved)
            await self._sink.deliver(offers)
        except Exception as error:  # noqa: BLE001 — a sink failure is a retryable delivery
            # Every entry that contributed an offer failed, because the sink
            # is batch-shaped and cannot say which of them it got to. The
            # relay redelivers all of them, and the staleness check above
            # discards whatever has since been answered.
            logger.warning(
                "pending_match_delivery_failed",
                extra={"offers": len(resolved), "error": type(error).__name__},
                exc_info=error,
            )
            failed_ids = {entry.id for entry, _, _ in resolved}
            undelivered = (_Failed(entry_id=id_, reason="delivery_failed") for id_ in failed_ids)
            return [*failures, *undelivered]

        logger.info(
            "pending_matches_delivered",
            extra={"entries": len(entries), "offers": len(offers)},
        )
        return failures

    async def _pending_for(
        self, entry: OutboxEntry
    ) -> list[tuple[OutboxEntry, PendingMatchView, UUID]]:
        """The recipients of one `match_created` who may still be told.

        Both participants are asked **individually**, through a read scoped
        to each of them, so "is this player still in this match" is answered
        by the same query that answers "is it still pending" — there is no
        arrangement here where a stale participant list could be trusted.

        A player whose read comes back `None`, or whose match is no longer
        the one this event named, contributes nothing. Both are ordinary:
        the first is a match that has been answered, the second is a player
        who has already been paired again by a later scan.
        """
        payload = entry.payload
        match_id = UUID(str(payload["match_id"]))
        now = self._clock.now()
        recipients = (
            UUID(str(payload["light_player_id"])),
            UUID(str(payload["dark_player_id"])),
        )

        deliverable: list[tuple[OutboxEntry, PendingMatchView, UUID]] = []
        for player_id in recipients:
            view = await self._acceptance.pending_match(player_id)
            # **Still awaiting an answer**, not merely still current.
            # A64-020.5A widened `pending_match` to report a match that has
            # already started, which is what a lobby needs and is exactly
            # what must not be pushed as an offer: both players agreed
            # before the relay reached this entry, and delivering it would
            # open an acceptance dialog over a game already in progress.
            if view is not None and view.status is not MatchRecordStatus.PENDING_ACCEPTANCE:
                self._metrics.increment(
                    PENDING_MATCH_DELIVERIES, labels={"outcome": DeliveryOutcome.STALE}
                )
                continue
            if view is None or view.match_id != match_id:
                self._metrics.increment(
                    PENDING_MATCH_DELIVERIES, labels={"outcome": DeliveryOutcome.STALE}
                )
                continue
            if view.acceptance_deadline <= now:
                # Still pending, and out of time — the reconciler has not
                # reached it yet. Pushing it would start a client's
                # countdown below zero.
                self._metrics.increment(
                    PENDING_MATCH_DELIVERIES,
                    labels={"outcome": DeliveryOutcome.DEADLINE_PASSED},
                )
                continue
            deliverable.append((entry, view, player_id))

        return deliverable

    async def _render(
        self, resolved: Sequence[tuple[OutboxEntry, PendingMatchView, UUID]]
    ) -> list[PendingMatchOffer]:
        """Every deliverable view as an addressed offer, with two batched
        reads for the whole tick.

        The block read and the profile read are each one call over the union
        of everybody involved — the N+1 CLAUDE.md §10.4 names, avoided in a
        consumer that runs on every relay tick rather than per request.
        """
        blocked = await self._blocked_among(resolved)
        previews = await self._previews_for(resolved)

        offers: list[PendingMatchOffer] = []
        for _, view, recipient_id in resolved:
            opponent_id = view.opponent_player_id
            withheld = opponent_id in blocked.get(recipient_id, frozenset()) or recipient_id in (
                blocked.get(opponent_id, frozenset())
            )
            preview = None if withheld else previews.get(opponent_id)

            self._metrics.increment(
                PENDING_MATCH_DELIVERIES,
                labels={
                    "outcome": (
                        DeliveryOutcome.PREVIEW_WITHHELD if withheld else DeliveryOutcome.DELIVERED
                    )
                },
            )
            offers.append(_offer(view, recipient_id=recipient_id, opponent=preview))
        return offers

    async def _blocked_among(
        self, resolved: Sequence[tuple[OutboxEntry, PendingMatchView, UUID]]
    ) -> Mapping[UUID, frozenset[UUID]]:
        """Who may not see whom, **now**.

        Re-read at delivery rather than carried on the payload, which is
        A64-013.7's rule for exactly this class of state: the block list at
        delivery is the only one that counts, and a copy on the event would
        be a stale answer competing with the live one.

        Degrades to "nobody is blocked" on failure. The direction is chosen:
        an unreadable block graph must not stop a match offer, because a
        player who never learns they have a match loses it to the deadline —
        and BL-2 already made a blocked pairing impossible at the point it
        mattered most.
        """
        involved = {recipient for _, _, recipient in resolved}
        involved.update(view.opponent_player_id for _, view, _ in resolved)
        if len(involved) < 2:
            return {}

        try:
            return await self._exclusions.blocked_pairs_among(sorted(involved))
        except Exception as error:  # noqa: BLE001 — an unreadable graph must not stop delivery
            logger.error(
                "pending_match_block_read_failed",
                extra={"players": len(involved), "error": type(error).__name__},
                exc_info=error,
            )
            return {}

    async def _previews_for(
        self, resolved: Sequence[tuple[OutboxEntry, PendingMatchView, UUID]]
    ) -> Mapping[UUID, OpponentPreview]:
        """Every opponent's public identity, in one query.

        `find_public_profiles` omits deactivated accounts, so a player who
        withdrew between the pairing and this delivery simply has no entry
        and their opponent's offer carries no preview — the same answer
        every other surface on this platform gives for a withdrawn account.
        """
        opponents = {view.opponent_player_id for _, view, _ in resolved}
        if not opponents:
            return {}

        profiles = await self._players.find_public_profiles(sorted(opponents))
        return {player_id: _preview(profile) for player_id, profile in profiles.items()}


def _preview(profile: PublicUserProfile) -> OpponentPreview:
    """The three public fields a match card needs.

    Built here rather than by the schema layer because this offer never
    reaches a `presentation` package — it goes to a socket. Deliberately not
    the whole `PublicUserProfile`: country, biography and join date are data
    the recipient may be entitled to see and that a match offer has no
    business carrying, and the narrow type is what makes that structural.
    """
    return OpponentPreview(
        player_id=profile.id, username=profile.username, display_name=profile.display_name
    )


def _offer(
    view: PendingMatchView, *, recipient_id: UUID, opponent: OpponentPreview | None
) -> PendingMatchOffer:
    """One view, addressed."""
    return PendingMatchOffer(
        recipient_id=recipient_id,
        match_id=view.match_id,
        status=view.status,
        your_side=view.your_side,
        opponent=opponent,
        variant=view.variant,
        rated=view.rated,
        acceptance_deadline=view.acceptance_deadline,
        you_accepted=view.you_accepted,
        opponent_accepted=view.opponent_accepted,
        created_at=view.created_at,
    )


__all__ = ["CONSUMER_NAME", "SUBSCRIBED_EVENT_TYPES", "PendingMatchNotifier"]
