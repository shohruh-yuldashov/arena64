"""Stand-ins for the two collaborators realtime delivery reaches outward
to — A64-015.5 §4.

`PendingMatchNotifier` runs for real against these, so the re-read, the
deadline check and the batching are genuinely exercised. What is faked is
the transport and `users`' profile store.
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from uuid import UUID

from app.common.locale import Locale
from app.modules.matchmaking.domain.pending_match import PendingMatchOffer
from app.modules.users.public import (
    AvatarReference,
    ProfileVisibility,
    PublicUserProfile,
    VisibilityLevel,
)


class RecordingPendingMatchSink:
    """A `PendingMatchSink` that keeps what it was given.

    Keeps the offers themselves rather than their fields, so a test asserts
    on a type and an attribute instead of on a dict of strings — the same
    choice `RecordingPublisher` makes for events.

    `fails` makes it raise, which is the behaviour a real transport has and
    the reason the port says a sink *may* raise: a delivery that failed is
    one the platform should retry.
    """

    def __init__(self) -> None:
        self.delivered: list[PendingMatchOffer] = []
        self.batches: list[int] = []
        self.fails = False

    async def deliver(self, offers: Sequence[PendingMatchOffer]) -> None:
        if self.fails:
            raise RuntimeError("the gateway is unreachable")
        self.batches.append(len(offers))
        self.delivered.extend(offers)


class StubPublicProfiles:
    """A `users.public.PublicProfileReader` a test dictates the answers of.

    Records every batch it was asked about, so a test can assert the read
    was **batched** — one call for a relay tick rather than one per match,
    which is the property no assertion on the result could catch.

    A deactivated player is *omitted* rather than returned with a flag,
    which is exactly what the real port does: "which handles belong to
    withdrawn accounts is itself a disclosure", so a consumer cannot render
    one even if it tries.
    """

    def __init__(self) -> None:
        self.profiles: dict[UUID, str] = {}
        self.calls: list[tuple[UUID, ...]] = []

    def register(self, player_id: UUID, username: str) -> None:
        self.profiles[player_id] = username

    def deactivate(self, player_id: UUID) -> None:
        self.profiles.pop(player_id, None)

    async def find_public_profile(self, username: str) -> PublicUserProfile | None:
        return None

    async def find_public_profiles(
        self, player_ids: Sequence[UUID]
    ) -> Mapping[UUID, PublicUserProfile]:
        self.calls.append(tuple(player_ids))
        return {
            player_id: _profile(player_id, self.profiles[player_id])
            for player_id in player_ids
            if player_id in self.profiles
        }


#: The instant every stub profile reports as its join date. Fixed rather
#: than `now()`, because a test that cannot state its own clock cannot
#: assert on one (AD-07).
_JOINED = datetime(2026, 1, 1, tzinfo=UTC)


def _profile(player_id: UUID, username: str) -> PublicUserProfile:
    """The narrowest legal `PublicUserProfile`.

    Everything a privacy setting governs is set to its most restrictive
    value, because the consumer under test reads three fields — id,
    username, display name — and a stub that published a country would
    quietly make the test pass for a profile shape the real reader might
    never produce.
    """
    return PublicUserProfile(
        id=player_id,
        username=username,
        display_name=username.title(),
        avatar=AvatarReference(object_key=None, version=0, uploaded_at=None),
        country=None,
        preferred_language=Locale.EN,
        bio=None,
        created_at=_JOINED,
        visibility=ProfileVisibility(
            last_seen=VisibilityLevel.NOBODY,
            statistics=False,
            online_status=VisibilityLevel.NOBODY,
            activity=VisibilityLevel.NOBODY,
        ),
    )


__all__ = ["RecordingPendingMatchSink", "StubPublicProfiles"]
