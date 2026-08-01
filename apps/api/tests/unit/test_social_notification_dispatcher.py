"""`SocialNotificationDispatcher` — who is told what, A64-013.7.

Three rules from the brief, and each is a class below:

    TestBlocking      "blocked users must never receive notifications", and
                      the re-read that makes it true after the event was
                      recorded
    TestPermissions   "audience membership does NOT imply permission" —
                      every payload goes through the composer, and a hidden
                      field stays hidden
    TestAudiences     who receives each of the six events, including the
                      three that notify nobody by domain rule

The **real** dispatcher runs over fakes of the two published ports it reads
through. The renderer is the exception: `TestPermissions` runs the *real*
`BatchProfileRenderer` over the *real* `PublicProfileComposer`, because a
faked renderer would make "the privacy gate is applied" a statement about
the fake.
"""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest

from app.core.enums import Locale
from app.modules.friends.domain.events import (
    FriendRemoved,
    FriendRequestAccepted,
    PlayerBlocked,
    PlayerUnblocked,
)
from app.modules.notifications.application.services import SocialNotificationDispatcher
from app.modules.notifications.domain.notification import NotificationKind, SocialNotification
from app.modules.profiles.application.services.profile_composer import PublicProfileComposer
from app.modules.profiles.application.services.profile_renderer import BatchProfileRenderer
from app.modules.profiles.domain.profile import PublicProfile
from app.modules.profiles.infrastructure.rating_providers import UnratedRatingProvider
from app.modules.users.domain.events import PresenceOffline, PresenceOnline
from app.modules.users.domain.presence import Presence
from app.modules.users.public import (
    AvatarReference,
    ProfileVisibility,
    PublicUserProfile,
    ViewerRelationship,
    VisibilityLevel,
)
from app.platform.outbox import OutboxEntry

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
SUBJECT = UUID("019fbc30-0001-7000-8000-000000000001")
FRIEND = UUID("019fbc30-0002-7000-8000-000000000002")
OTHER_FRIEND = UUID("019fbc30-0003-7000-8000-000000000003")
BLOCKED = UUID("019fbc30-0004-7000-8000-000000000004")


class _StubAudience:
    """`friends.public.PresenceAudience`, with a fixed answer.

    Records its calls, because "the audience was resolved at delivery" is
    only observable as "it was asked at all".
    """

    def __init__(self, observers: set[UUID]) -> None:
        self._observers = observers
        self.calls: list[UUID] = []

    async def observers_of(self, player_id: UUID) -> frozenset[UUID]:
        self.calls.append(player_id)
        return frozenset(self._observers)


class _StubGraph:
    """`friends.public.SocialGraphReader`, block half only.

    `friend_ids_among` is on the port and unused by the dispatcher, so it is
    present and returns nothing — a fake that omitted it would satisfy the
    dispatcher and not the protocol, which is the drift RP-05 warns about.
    """

    def __init__(self, blocked: set[UUID]) -> None:
        self.blocked = blocked
        self.calls: list[UUID] = []

    async def blocked_ids_for(self, player_id: UUID) -> frozenset[UUID]:
        self.calls.append(player_id)
        return frozenset(self.blocked)

    async def friend_ids_among(self, player_id: UUID, others: Sequence[UUID]) -> set[UUID]:
        return set()


class _StubRenderer:
    """`profiles.public.ProfileRenderer`, returning a marker profile.

    Used everywhere except `TestPermissions`, where the real one runs.
    Records the relationship it was asked for, because "one render per
    audience, under the asserted relationship" is the batching claim.
    """

    def __init__(self, *, known: set[UUID] | None = None) -> None:
        self._known = known
        self.calls: list[tuple[tuple[UUID, ...], ViewerRelationship]] = []

    async def render_many(
        self, player_ids: Sequence[UUID], *, relationship: ViewerRelationship
    ) -> Mapping[UUID, PublicProfile]:
        self.calls.append((tuple(player_ids), relationship))
        known = self._known if self._known is not None else set(player_ids)
        return {
            player_id: cast(PublicProfile, _MarkerProfile(player_id))
            for player_id in player_ids
            if player_id in known
        }


class _MarkerProfile:
    """Stands in for a composed `PublicProfile` where the composition is not
    what is being asserted."""

    def __init__(self, player_id: UUID) -> None:
        self.player_id = player_id


class _RecordingSink:
    def __init__(self, *, raises: bool = False) -> None:
        self.delivered: list[SocialNotification] = []
        self._raises = raises

    async def deliver(self, notifications: list[SocialNotification]) -> None:
        if self._raises:
            raise ConnectionError("transport is down")
        self.delivered.extend(notifications)


def _entry(event: Any) -> OutboxEntry:
    """One outbox entry from a real event, exactly as the publisher makes it."""
    return OutboxEntry.of(event)


def _dispatcher(
    *,
    observers: set[UUID] | None = None,
    blocked: set[UUID] | None = None,
    renderer: Any = None,
    sink: _RecordingSink | None = None,
) -> tuple[SocialNotificationDispatcher, _RecordingSink]:
    recording = sink or _RecordingSink()
    return (
        SocialNotificationDispatcher(
            audience=cast(Any, _StubAudience(observers or set())),
            graph=cast(Any, _StubGraph(blocked or set())),
            profiles=renderer or _StubRenderer(),
            sink=cast(Any, recording),
        ),
        recording,
    )


class TestBlocking:
    async def test_a_blocked_player_never_receives_a_presence_notification(self) -> None:
        """The audience is *already* friends minus blocked — this asserts the
        dispatcher does not add anybody back."""
        dispatcher, sink = _dispatcher(observers={FRIEND})

        await dispatcher.handle([_entry(PresenceOnline(occurred_at=NOW, player_id=SUBJECT))])

        assert [notification.recipient_id for notification in sink.delivered] == [FRIEND]

    async def test_the_audience_is_resolved_at_delivery_and_not_taken_from_the_payload(
        self,
    ) -> None:
        """The event carries only `player_id`, so there is nothing stale to
        trust — asserted by checking the live port was asked."""
        audience = _StubAudience({FRIEND})
        dispatcher = SocialNotificationDispatcher(
            audience=cast(Any, audience),
            graph=cast(Any, _StubGraph(set())),
            profiles=_StubRenderer(),
            sink=cast(Any, _RecordingSink()),
        )

        await dispatcher.handle([_entry(PresenceOffline(occurred_at=NOW, player_id=SUBJECT))])

        assert audience.calls == [SUBJECT]

    async def test_a_block_placed_after_the_event_suppresses_the_acceptance_notice(
        self,
    ) -> None:
        """The case A64-013.7 is written around.

        A request is accepted, the event is recorded, and *then* the
        addressee blocks the requester. The payload still names the
        requester; the live block list is what decides, and the notification
        is not sent.
        """
        accepted = FriendRequestAccepted(
            occurred_at=NOW,
            request_id=UUID("019fbc30-0010-7000-8000-000000000010"),
            requester_id=FRIEND,
            addressee_id=SUBJECT,
            friendship_id=UUID("019fbc30-0011-7000-8000-000000000011"),
        )
        dispatcher, sink = _dispatcher(blocked={FRIEND})

        await dispatcher.handle([_entry(accepted)])

        assert sink.delivered == []

    async def test_an_unblocked_pair_still_receives_the_acceptance_notice(self) -> None:
        """The other half of the same read: re-checking must not suppress
        everything, only the pair that was actually blocked."""
        accepted = FriendRequestAccepted(
            occurred_at=NOW,
            request_id=UUID("019fbc30-0010-7000-8000-000000000010"),
            requester_id=FRIEND,
            addressee_id=SUBJECT,
            friendship_id=UUID("019fbc30-0011-7000-8000-000000000011"),
        )
        dispatcher, sink = _dispatcher(blocked={OTHER_FRIEND})

        await dispatcher.handle([_entry(accepted)])

        assert [notification.recipient_id for notification in sink.delivered] == [FRIEND]


class TestPermissions:
    """The real composer, so "rendered through `PublicProfileComposer`" is a
    fact about the platform rather than about a stub."""

    @staticmethod
    def _renderer(*, online_status_public: bool) -> BatchProfileRenderer:
        identity = _identity(online_status_public=online_status_public)
        return BatchProfileRenderer(
            players=cast(Any, _StubPlayers({SUBJECT: identity})),
            composer=PublicProfileComposer(
                ratings=cast(Any, _StubRatings()),
                statistics=cast(Any, _StubStatistics()),
                presence=cast(Any, _StubPresence()),
                relationships=cast(Any, _StubRelationships()),
            ),
        )

    async def test_a_friend_receives_the_presence_the_subject_publishes(self) -> None:
        dispatcher, sink = _dispatcher(
            observers={FRIEND}, renderer=self._renderer(online_status_public=True)
        )

        await dispatcher.handle([_entry(PresenceOnline(occurred_at=NOW, player_id=SUBJECT))])

        assert len(sink.delivered) == 1
        assert sink.delivered[0].subject.is_online is True

    async def test_a_subject_who_hides_presence_publishes_none_of_it(self) -> None:
        """Audience membership is not permission. Every recipient is a
        friend and every one of them is entitled to be told *something* — but
        the field itself is gated, and the composer is what gates it."""
        dispatcher, sink = _dispatcher(
            observers={FRIEND}, renderer=self._renderer(online_status_public=False)
        )

        await dispatcher.handle([_entry(PresenceOnline(occurred_at=NOW, player_id=SUBJECT))])

        assert len(sink.delivered) == 1
        assert sink.delivered[0].subject.is_online is None

    async def test_the_payload_is_the_composed_view_and_not_the_raw_identity(self) -> None:
        """A `PublicProfile`, which has no email field and no unfiltered
        identity behind it — the leak is unreachable rather than avoided."""
        dispatcher, sink = _dispatcher(
            observers={FRIEND}, renderer=self._renderer(online_status_public=True)
        )

        await dispatcher.handle([_entry(PresenceOnline(occurred_at=NOW, player_id=SUBJECT))])

        assert isinstance(sink.delivered[0].subject, PublicProfile)


class TestAudiences:
    async def test_every_observer_receives_one_notification(self) -> None:
        dispatcher, sink = _dispatcher(observers={FRIEND, OTHER_FRIEND})

        await dispatcher.handle([_entry(PresenceOnline(occurred_at=NOW, player_id=SUBJECT))])

        assert {n.recipient_id for n in sink.delivered} == {FRIEND, OTHER_FRIEND}
        assert all(n.kind is NotificationKind.FRIEND_ONLINE for n in sink.delivered)

    async def test_going_offline_is_a_different_kind(self) -> None:
        dispatcher, sink = _dispatcher(observers={FRIEND})

        await dispatcher.handle([_entry(PresenceOffline(occurred_at=NOW, player_id=SUBJECT))])

        assert sink.delivered[0].kind is NotificationKind.FRIEND_OFFLINE

    async def test_the_requester_is_told_that_their_request_was_accepted(self) -> None:
        """And the addressee is not: they performed the acceptance, and
        telling somebody what they just did is not a notification."""
        accepted = FriendRequestAccepted(
            occurred_at=NOW,
            request_id=UUID("019fbc30-0010-7000-8000-000000000010"),
            requester_id=FRIEND,
            addressee_id=SUBJECT,
            friendship_id=UUID("019fbc30-0011-7000-8000-000000000011"),
        )
        dispatcher, sink = _dispatcher()

        await dispatcher.handle([_entry(accepted)])

        assert [n.recipient_id for n in sink.delivered] == [FRIEND]
        assert sink.delivered[0].kind is NotificationKind.FRIEND_REQUEST_ACCEPTED
        # About the addressee — the person the requester now knows accepted.
        assert sink.delivered[0].subject.player_id == SUBJECT  # type: ignore[attr-defined]

    @pytest.mark.parametrize(
        "event",
        [
            FriendRemoved(
                occurred_at=NOW,
                friendship_id=UUID("019fbc30-0020-7000-8000-000000000020"),
                removed_by=SUBJECT,
                removed_player_id=FRIEND,
            ),
            PlayerBlocked(
                occurred_at=NOW,
                blocker_id=SUBJECT,
                blocked_id=BLOCKED,
                friendship_ended=True,
                requests_voided=0,
            ),
            PlayerUnblocked(occurred_at=NOW, blocker_id=SUBJECT, blocked_id=BLOCKED),
        ],
        ids=["friend-removed", "player-blocked", "player-unblocked"],
    )
    async def test_three_events_are_recorded_and_notify_nobody(self, event: Any) -> None:
        """FS-2 and BL-1, enforced at delivery.

        Removal is unilateral and silent; a block and its lifting are
        invisible to their subject. All three are still *subscribed to*, so
        the ledger records that this consumer considered them — an event
        nobody subscribed to would be indistinguishable from one a consumer
        silently dropped.
        """
        dispatcher, sink = _dispatcher(observers={FRIEND, OTHER_FRIEND})

        failures = await dispatcher.handle([_entry(event)])

        assert sink.delivered == []
        assert failures == []
        assert dispatcher.handles(type(event).event_type)

    async def test_a_subject_with_no_public_profile_notifies_nobody(self) -> None:
        """Deactivated between the event and its delivery. No notification
        rather than a rendered tombstone — the renderer omits them, and this
        is what the dispatcher does about it."""
        dispatcher, sink = _dispatcher(observers={FRIEND}, renderer=_StubRenderer(known=set()))

        await dispatcher.handle([_entry(PresenceOnline(occurred_at=NOW, player_id=SUBJECT))])

        assert sink.delivered == []


class TestBatching:
    async def test_an_audience_of_many_costs_one_render(self) -> None:
        """A64-013.7: "avoid N+1 profile rendering."

        Every recipient is a friend by construction, and a `PublicProfile` is
        a function of (identity, relationship) — so one render serves the
        whole audience.
        """
        renderer = _StubRenderer()
        dispatcher = SocialNotificationDispatcher(
            audience=cast(Any, _StubAudience({FRIEND, OTHER_FRIEND, BLOCKED})),
            graph=cast(Any, _StubGraph(set())),
            profiles=renderer,
            sink=cast(Any, _RecordingSink()),
        )

        await dispatcher.handle([_entry(PresenceOnline(occurred_at=NOW, player_id=SUBJECT))])

        assert len(renderer.calls) == 1
        assert renderer.calls[0] == ((SUBJECT,), ViewerRelationship.FRIEND)

    async def test_an_event_with_no_recipients_renders_nothing_at_all(self) -> None:
        """Not fetched is stronger than fetched and discarded: a player with
        no friends costs no profile read."""
        renderer = _StubRenderer()
        dispatcher = SocialNotificationDispatcher(
            audience=cast(Any, _StubAudience(set())),
            graph=cast(Any, _StubGraph(set())),
            profiles=renderer,
            sink=cast(Any, _RecordingSink()),
        )

        await dispatcher.handle([_entry(PresenceOnline(occurred_at=NOW, player_id=SUBJECT))])

        assert renderer.calls == []


class TestFailures:
    async def test_a_failing_sink_is_reported_rather_than_swallowed(self) -> None:
        """A delivery that failed is one to retry. Swallowing it would mark
        the event published and lose the notification silently."""
        entry = _entry(PresenceOnline(occurred_at=NOW, player_id=SUBJECT))
        dispatcher, _ = _dispatcher(observers={FRIEND}, sink=_RecordingSink(raises=True))

        failures = await dispatcher.handle([entry])

        assert [failure.entry_id for failure in failures] == [entry.id]
        assert failures[0].reason == "ConnectionError"

    async def test_one_failing_event_does_not_stop_the_others(self) -> None:
        """Per-event failure, so a poison event does not hold back the
        batch — see `OutboxRelay` on what the relay does with the result."""
        good = _entry(PresenceOnline(occurred_at=NOW, player_id=SUBJECT))
        malformed = replace(good, payload={"player_id": "not-a-uuid"})
        dispatcher, sink = _dispatcher(observers={FRIEND})

        failures = await dispatcher.handle([malformed, good])

        assert [failure.entry_id for failure in failures] == [malformed.id]
        assert [n.recipient_id for n in sink.delivered] == [FRIEND]

    async def test_the_notification_carries_the_event_s_instant_not_the_delivery_s(
        self,
    ) -> None:
        """A notification that arrives late must still say when the thing it
        describes happened, or a client renders "just now" for a friend who
        came online while the relay was catching up."""
        dispatcher, sink = _dispatcher(observers={FRIEND})

        await dispatcher.handle([_entry(PresenceOnline(occurred_at=NOW, player_id=SUBJECT))])

        assert sink.delivered[0].occurred_at == NOW


class _StubPlayers:
    def __init__(self, identities: Mapping[UUID, Any]) -> None:
        self._identities = identities

    async def find_public_profiles(self, player_ids: Sequence[UUID]) -> Mapping[UUID, Any]:
        return {
            player_id: self._identities[player_id]
            for player_id in player_ids
            if player_id in self._identities
        }


class _StubRatings:
    """The real placeholder provider, which is already "every player is
    unrated" — so this delegates rather than inventing a second answer."""

    def __init__(self) -> None:
        self._real = UnratedRatingProvider()

    async def ratings_for(self, player_id: UUID) -> Any:
        return await self._real.ratings_for(player_id)

    async def ratings_for_many(self, player_ids: Sequence[UUID]) -> Mapping[UUID, Any]:
        return await self._real.ratings_for_many(player_ids)


class _StubStatistics:
    async def statistics_for(self, player_id: UUID) -> Any:
        return None

    async def statistics_for_many(self, player_ids: Sequence[UUID]) -> Mapping[UUID, Any]:
        return {}


class _StubPresence:
    async def presence_for(self, player_id: UUID) -> Any:
        return _online_presence()

    async def presence_for_many(self, player_ids: Sequence[UUID]) -> Mapping[UUID, Any]:
        return {player_id: _online_presence() for player_id in player_ids}


class _StubRelationships:
    async def relationship_to(self, viewer_id: UUID | None, player_id: UUID) -> Any:
        return ViewerRelationship.STRANGER

    async def relationships_to(
        self, viewer_id: UUID | None, player_ids: Sequence[UUID]
    ) -> Mapping[UUID, Any]:
        return dict.fromkeys(player_ids, ViewerRelationship.STRANGER)


def _online_presence() -> Presence:
    return Presence(is_online=True, last_seen=NOW, session_id=None, device_type=None)


def _identity(*, online_status_public: bool) -> PublicUserProfile:
    """One player's published identity, with presence visible or not.

    Built here rather than borrowed from another suite: what varies is one
    `VisibilityLevel`, and a shared builder with a flag for every setting
    would be a fixture nobody can read at the call site.
    """
    level = VisibilityLevel.EVERYONE if online_status_public else VisibilityLevel.NOBODY
    return PublicUserProfile(
        id=SUBJECT,
        username="subject",
        display_name=None,
        avatar=AvatarReference(object_key=None, version=0, uploaded_at=None),
        country=None,
        preferred_language=Locale.EN,
        bio=None,
        created_at=NOW,
        visibility=ProfileVisibility(
            last_seen=level,
            statistics=True,
            online_status=level,
            activity=level,
        ),
    )
