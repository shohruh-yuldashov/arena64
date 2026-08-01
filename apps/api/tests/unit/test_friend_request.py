"""The `FriendRequest` aggregate and `VisibilityLevel` — the two pieces of
A64-013.2 that are pure logic.

Everything about *storing* requests needs PostgreSQL and lives in
`tests/contract/test_friend_requests_api.py`. What is here is the state
machine, the ownership rules and the audience model — all of which are
security controls, and none of which needs a database to assert.

A64-013.2 asks for essential tests only. The four send rules, the two
ownership rules per resolution and the visibility serialisation it names are
here; the rest is the small set of properties that would be *silently* wrong
rather than loudly broken — a second resolution succeeding, or a
`VisibilityLevel` that treats `FRIENDS` as public.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.modules.friends.domain.exceptions import (
    FriendRequestAlreadyResolved,
    FriendRequestNotPending,
    NotRequestAddressee,
    NotRequestRequester,
    SelfFriendRequest,
)
from app.modules.friends.domain.friend_request import FriendRequest, FriendRequestStatus
from app.modules.users.domain.privacy import PrivacySettings
from app.modules.users.domain.visibility import ViewerRelationship, VisibilityLevel

SENT_AT = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
ANSWERED_AT = datetime(2026, 8, 1, 12, 5, tzinfo=UTC)

ALICE = UUID("019fb9ea-0a0c-7cec-9c5f-402727c31a96")
BOB = UUID("019fb9ea-1b1d-7ded-8b60-513838d42b07")


def pending(requester: UUID = ALICE, addressee: UUID = BOB) -> FriendRequest:
    return FriendRequest.send(requester_id=requester, addressee_id=addressee, sent_at=SENT_AT)


class TestSend:
    def test_a_new_request_is_pending_and_unanswered(self) -> None:
        request = pending()

        assert request.status is FriendRequestStatus.PENDING
        assert request.created_at == SENT_AT
        assert request.responded_at is None

    def test_a_self_request_is_refused(self) -> None:
        """A64-013.2's "cannot send to self", at the aggregate.

        Enforced in three places — here, in the validator before any I/O,
        and in `ck_friend_request__not_self` — and BE-06 makes the
        constraint the authoritative one. This copy is what makes the
        invalid object unconstructible, so no future caller can reach a
        state the database would refuse.
        """
        with pytest.raises(SelfFriendRequest):
            FriendRequest.send(requester_id=ALICE, addressee_id=ALICE, sent_at=SENT_AT)

    def test_a_rehydrated_self_request_is_refused_too(self) -> None:
        """The repository constructs instances directly rather than through
        `send`, so `__post_init__` is what stops a corrupt row from reaching
        a response."""
        with pytest.raises(SelfFriendRequest):
            FriendRequest(requester_id=ALICE, addressee_id=ALICE)

    def test_expiry_is_prepared_but_unset(self) -> None:
        """A64-013.2 excludes expiry and asks that adding it need no
        redesign. `expires_at` is a field from this release, and `None`
        means *no window* rather than *never expires* — a distinction that
        matters the moment a sweep exists."""
        assert pending().expires_at is None
        assert FriendRequestStatus.EXPIRED in set(FriendRequestStatus)


class TestAccept:
    def test_the_addressee_can_accept(self) -> None:
        request = pending()

        request.accept(by=BOB, at=ANSWERED_AT)

        assert request.status is FriendRequestStatus.ACCEPTED
        assert request.responded_at == ANSWERED_AT

    def test_the_requester_cannot_accept_their_own_request(self) -> None:
        """The ownership rule that matters most: a sender who could accept
        would be adding themselves to somebody else's friend list."""
        request = pending()

        with pytest.raises(NotRequestAddressee):
            request.accept(by=ALICE, at=ANSWERED_AT)

        assert request.status is FriendRequestStatus.PENDING

    def test_a_stranger_cannot_accept(self) -> None:
        request = pending()

        with pytest.raises(NotRequestAddressee):
            request.accept(by=uuid4(), at=ANSWERED_AT)

    def test_the_rejection_names_neither_party(self) -> None:
        """A rejection that named the requester would turn a guessed request
        id into a way to learn who is sending requests to whom."""
        request = pending()

        with pytest.raises(NotRequestAddressee) as rejected:
            request.accept(by=uuid4(), at=ANSWERED_AT)

        assert str(ALICE) not in rejected.value.message
        assert str(BOB) not in rejected.value.message


class TestDecline:
    def test_the_addressee_can_decline(self) -> None:
        request = pending()

        request.decline(by=BOB, at=ANSWERED_AT)

        assert request.status is FriendRequestStatus.DECLINED
        assert request.responded_at == ANSWERED_AT

    def test_a_non_owner_cannot_decline(self) -> None:
        request = pending()

        with pytest.raises(NotRequestAddressee):
            request.decline(by=ALICE, at=ANSWERED_AT)


class TestCancel:
    def test_the_sender_can_cancel(self) -> None:
        request = pending()

        request.cancel(by=ALICE, at=ANSWERED_AT)

        assert request.status is FriendRequestStatus.CANCELLED
        assert request.responded_at == ANSWERED_AT

    def test_the_addressee_cannot_cancel(self) -> None:
        """They have `decline`, which reaches the same practical outcome and
        leaves different history — FR-5's future cooldown reads it, so the
        two must not be interchangeable."""
        request = pending()

        with pytest.raises(NotRequestRequester):
            request.cancel(by=BOB, at=ANSWERED_AT)

    def test_a_stranger_cannot_cancel(self) -> None:
        request = pending()

        with pytest.raises(NotRequestRequester):
            request.cancel(by=uuid4(), at=ANSWERED_AT)


class TestResolvedOnce:
    @pytest.mark.parametrize(
        "second",
        ["accept", "decline"],
        ids=["accept-after-accept", "decline-after-accept"],
    )
    def test_a_resolved_request_cannot_be_resolved_again(self, second: str) -> None:
        """The invariant no other object can enforce, and the reason this is
        an aggregate root: exactly one transition out of `PENDING`.

        The concrete race is an addressee with the request open on a phone
        and a laptop. This is the in-memory half; the version column is what
        makes it hold across two processes.
        """
        request = pending()
        request.accept(by=BOB, at=ANSWERED_AT)

        with pytest.raises(FriendRequestAlreadyResolved):
            getattr(request, second)(by=BOB, at=ANSWERED_AT)

        assert request.status is FriendRequestStatus.ACCEPTED

    def test_a_cancelled_request_cannot_then_be_accepted(self) -> None:
        request = pending()
        request.cancel(by=ALICE, at=ANSWERED_AT)

        with pytest.raises(FriendRequestAlreadyResolved):
            request.accept(by=BOB, at=ANSWERED_AT)

    def test_require_pending_is_the_guard_a_block_handler_will_use(self) -> None:
        """A64-013.5's extension point: voiding must skip already-resolved
        rows, and needs to ask without attempting a transition."""
        request = pending()
        request.require_pending()

        request.decline(by=BOB, at=ANSWERED_AT)

        with pytest.raises(FriendRequestNotPending):
            request.require_pending()

    def test_ownership_is_checked_before_the_resolved_state(self) -> None:
        """A stranger probing a resolved request must learn that they are
        not party to it, not what state it is in."""
        request = pending()
        request.accept(by=BOB, at=ANSWERED_AT)

        with pytest.raises(NotRequestAddressee):
            request.accept(by=uuid4(), at=ANSWERED_AT)


class TestVisibilityLevel:
    """A64-013.2's required privacy tests: migration, serialisation,
    validation."""

    @pytest.mark.parametrize(
        ("visible", "expected"),
        [(True, VisibilityLevel.EVERYONE), (False, VisibilityLevel.NOBODY)],
        ids=["true-widens-to-everyone", "false-widens-to-nobody"],
    )
    def test_the_boolean_widening_is_the_one_the_migration_applies(
        self, visible: bool, expected: VisibilityLevel
    ) -> None:
        """The conversion in `c4e8b1a29f37`, asserted in Python.

        The migration writes the same mapping in SQL. Both exist because
        both have callers — the migration converts stored rows, and
        `VisibilityLevel.of` converts a deprecated boolean arriving on the
        API — and they must agree or a client's `true` would mean something
        different from a pre-migration `true`.
        """
        assert VisibilityLevel.of(visible=visible) is expected

    def test_serialisation_is_the_stored_and_wire_value(self) -> None:
        """A `StrEnum`, so the member, the column and the JSON are one
        string. Asserted against literals rather than the members, so this
        proves the *contract* rather than that the code agrees with
        itself."""
        assert VisibilityLevel.EVERYONE == "everyone"
        assert VisibilityLevel.FRIENDS == "friends"
        assert VisibilityLevel.NOBODY == "nobody"

    def test_an_unknown_value_is_refused(self) -> None:
        """Validation: the set is closed, so a typo cannot become a value no
        read path knows how to evaluate."""
        with pytest.raises(ValueError, match="not a valid"):
            VisibilityLevel("public")

    @pytest.mark.parametrize(
        ("level", "viewer", "permitted"),
        [
            (VisibilityLevel.EVERYONE, ViewerRelationship.STRANGER, True),
            (VisibilityLevel.EVERYONE, ViewerRelationship.FRIEND, True),
            (VisibilityLevel.FRIENDS, ViewerRelationship.STRANGER, False),
            (VisibilityLevel.FRIENDS, ViewerRelationship.FRIEND, True),
            (VisibilityLevel.NOBODY, ViewerRelationship.STRANGER, False),
            (VisibilityLevel.NOBODY, ViewerRelationship.FRIEND, False),
        ],
        ids=lambda value: str(value),
    )
    def test_permits_is_total(
        self, level: VisibilityLevel, viewer: ViewerRelationship, permitted: bool
    ) -> None:
        """Every combination has an answer, so no caller has to invent one.

        The `FRIENDS`/`STRANGER` cell is the one that carries the feature:
        it is `False` today for every real caller, because nothing yet
        produces `FRIEND` — which is why this task can change the storage
        model without changing a single response.
        """
        assert level.permits(viewer) is permitted

    def test_the_defaults_are_the_widening_of_the_old_ones(self) -> None:
        """No account's effective settings moved. Typed in by hand rather
        than imported, so this asserts the platform's answer rather than
        that the constants equal themselves."""
        defaults = PrivacySettings()

        assert defaults.show_country is True
        assert defaults.show_statistics is True
        assert defaults.last_seen is VisibilityLevel.NOBODY
        assert defaults.online_status is VisibilityLevel.EVERYONE
        assert defaults.activity is VisibilityLevel.EVERYONE

    def test_the_named_permission_helpers_agree_with_the_level(self) -> None:
        """Callers go through these rather than comparing a level to a
        member, which is what stops `is VisibilityLevel.EVERYONE` appearing
        at a call site and quietly ignoring `FRIENDS`."""
        settings = PrivacySettings(online_status=VisibilityLevel.FRIENDS)

        assert settings.permits_online_status(ViewerRelationship.FRIEND) is True
        assert settings.permits_online_status(ViewerRelationship.STRANGER) is False
