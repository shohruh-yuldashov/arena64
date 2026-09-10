"""An announcement is readable where it lands — A64-031.D.

The defect: a platform announcement was delivered, stored and counted, and
what a player saw was "New notification". Two independent halves caused it,
and each is asserted separately below.

  **The API never sent the text.** `NotificationResponse` grew one nullable
  subject key per payload — actor, tournament, game, challenge — and
  `announcement` was not among them, so the `title` and `body` an operator
  wrote were read out of storage, decoded into an `AnnouncementSummary`, and
  dropped on the way to the wire.

  **The client had no branch for it.** `notificationMessage` fell through to
  the generic sentence, which is the correct behaviour for a type a build has
  never heard of and the wrong one for a type it ships support for. That half
  is asserted in `apps/web`.

The third test here guards the push table, and it is the one that would have
caught this class of defect earlier: `PUSH_CAPABLE_TYPES` and the service
worker's presentation table are two closed sets that must agree, in two
languages, in two repositories' worth of directory. Nothing but an assertion
keeps them together.
"""

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.modules.admin.presentation.schemas.broadcasts import BroadcastCreateRequest
from app.modules.notifications.domain.broadcast import BroadcastAudience, BroadcastChannel
from app.modules.notifications.domain.push import PUSH_CAPABLE_TYPES
from app.modules.notifications.domain.record import (
    ActorSummary,
    AnnouncementSummary,
    NavigationTarget,
    NavigationTargetType,
    NotificationCategory,
    NotificationRecord,
    NotificationType,
)
from app.modules.notifications.presentation.schemas import NotificationResponse

NOW = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)
RECIPIENT = UUID("019fe700-0000-7000-8000-0000000000a1")


class _Avatars:
    """Never has a rendition. Announcements carry no actor at all, so this
    is only here to satisfy the signature."""

    def links_for(self, reference: object) -> None:
        return None


def _record(payload: object, *, type_: NotificationType, category: NotificationCategory):
    return NotificationRecord(
        id=uuid4(),
        recipient_id=RECIPIENT,
        type=type_,
        category=category,
        payload=payload,  # type: ignore[arg-type]
        target=NavigationTarget(type=NavigationTargetType.HOME),
        source_event_id=uuid4(),
        created_at=NOW,
    )


def _announcement(**overrides: str) -> NotificationRecord:
    fields = {
        "title": "Scheduled maintenance",
        "body": "The platform is unavailable from 02:00 to 04:00 UTC.",
        "locale": "en",
    }
    fields.update(overrides)
    return _record(
        AnnouncementSummary(**fields),  # type: ignore[arg-type]
        type_=NotificationType.PLATFORM_ANNOUNCEMENT,
        category=NotificationCategory.ANNOUNCEMENT,
    )


class TestTheTextReachesTheWire:
    def test_the_operators_words_are_sent_verbatim(self) -> None:
        response = NotificationResponse.of(_announcement(), avatars=_Avatars())  # type: ignore[arg-type]

        assert response.announcement is not None
        assert response.announcement.title == "Scheduled maintenance"
        assert response.announcement.body == (
            "The platform is unavailable from 02:00 to 04:00 UTC."
        )

    def test_the_authoring_language_travels_with_it(self) -> None:
        # Not the reader's language. It is sent so a client can set `lang`,
        # which is what lets a screen reader pronounce Russian prose with
        # Russian rules inside an Uzbek interface.
        response = NotificationResponse.of(
            _announcement(locale="ru"),
            avatars=_Avatars(),  # type: ignore[arg-type]
        )

        assert response.announcement is not None
        assert response.announcement.locale == "ru"

    def test_text_is_carried_rather_than_interpreted(self) -> None:
        # The response is a transport, not a sanitiser: what an operator
        # wrote is what a client receives, and the client renders it as a
        # text node. Escaping here would corrupt an announcement that
        # legitimately mentions a `<` and would still not make an unescaped
        # renderer safe.
        response = NotificationResponse.of(
            _announcement(body="Read <b>this</b> & note the 5 < 7 rule"),
            avatars=_Avatars(),  # type: ignore[arg-type]
        )

        assert response.announcement is not None
        assert response.announcement.body == "Read <b>this</b> & note the 5 < 7 rule"

    def test_a_notification_that_is_not_an_announcement_carries_none(self) -> None:
        # The key is per-payload and exactly one is populated. A social
        # notification that also carried an announcement would be a second
        # sentence with no rule for which one a row renders.
        social = _record(
            ActorSummary(
                player_id=uuid4(),
                username="rival",
                display_name="Rival",
                avatar_object_key=None,
                avatar_version=0,
            ),
            type_=NotificationType.FRIEND_REQUEST_RECEIVED,
            category=NotificationCategory.SOCIAL,
        )

        response = NotificationResponse.of(social, avatars=_Avatars())  # type: ignore[arg-type]

        assert response.announcement is None
        assert response.actor is not None


class TestTheChannelIsAClosedChoice:
    def test_an_omitted_channel_is_the_quiet_one(self) -> None:
        # The property that makes this safe to ship: a console that has not
        # been updated cannot start interrupting people.
        request = BroadcastCreateRequest(
            title="Maintenance",
            body="Back at 04:00.",
            locale="en",
            audience=BroadcastAudience.ALL_PLAYERS,
            idempotency_key="key-00000001",
        )

        assert request.channel is BroadcastChannel.IN_APP

    def test_push_is_requested_explicitly(self) -> None:
        request = BroadcastCreateRequest(
            title="Maintenance",
            body="Back at 04:00.",
            locale="en",
            audience=BroadcastAudience.ALL_PLAYERS,
            channel=BroadcastChannel.IN_APP_AND_PUSH,
            idempotency_key="key-00000001",
        )

        assert request.channel.includes_push

    def test_an_unknown_channel_is_refused_at_the_boundary(self) -> None:
        # §2.4. The one field that decides whether phones buzz cannot be
        # widened by a request — `email`, `sms` and anything else is a
        # validation error before the service is reached.
        with pytest.raises(ValidationError):
            BroadcastCreateRequest(
                title="Maintenance",
                body="Back at 04:00.",
                locale="en",
                audience=BroadcastAudience.ALL_PLAYERS,
                channel="sms",  # type: ignore[arg-type]
                idempotency_key="key-00000001",
            )


class TestTheWorkerAndTheBackendAgreeOnWhatIsPushed:
    """`PUSH_CAPABLE_TYPES` and the service worker's table are one decision
    split across two languages — see `pwa/push-presentation.ts`.

    A type the backend pushes with no entry there renders "You have a new
    notification" and opens the list, which is a deliberate degradation
    rather than a break. It is still a defect worth failing a build for: the
    whole reason the type set is small is that each member earned a sentence
    somebody wrote, and one that silently did not get one is a member that
    was added without that decision being made.
    """

    @staticmethod
    def _table() -> str:
        worker = Path(__file__).resolve().parents[3] / "web" / "pwa" / "push-presentation.ts"
        return worker.read_text()

    def test_the_service_worker_names_every_pushable_type(self) -> None:
        table = self._table()
        missing = sorted(
            type_.value for type_ in PUSH_CAPABLE_TYPES if f"{type_.value}: {{" not in table
        )

        assert missing == [], f"no push presentation for: {missing}"

    def test_an_announcement_push_carries_no_authored_text(self) -> None:
        # §12, approach B, and the reason it is not relaxed for this type:
        # an announcement can say "your account has been restricted", and a
        # lock screen is not a surface the reader chose. The operator's
        # words are behind their session, one tap away.
        table = self._table()
        entry = table.split("platform_announcement: {", 1)[1].split("},", 1)[0]

        assert 'title: "Arena64"' in entry
        assert 'body: "You have a new announcement."' in entry
