"""The durable `Notification` — NT-1, and A64-021.1's whole product model.

Framework-free (architecture.md §8), and the aggregate `domain-model.md`
§9.3 has specified since the beginning: *"a durable record that something
happened which the player should know about, independent of whether any
delivery channel worked."*

Until this task, `notifications` had only the *transient* half:
`SocialNotification`, a rendered value handed to a sink. That value's own
docstring predicted this file — *"when NT-1's history arrives, a second sink
persists `Notification` rows. Neither needs this type to change"* — and that
is exactly the shape this takes: a second sink, and no change to the
dispatcher that feeds it.

## A projection, not a second copy of the source

A notification carries only what is needed to **render it and navigate from
it**, and it is a record of what was true when it was written. That has two
consequences worth stating rather than discovering:

  - The actor's display name is the name they had *then*. A notification is
    history; re-resolving it at read time would cost a profile lookup per
    row (§31 forbids exactly that) and would rewrite the past.
  - Nothing here is authoritative about anything. Delete every row and the
    friendship, the request and the match are all still there.

## What is deliberately absent

`dismissed_at`, `expires_at` and `correlation_id` are all in
`database.md` §10.2 and none is here. Dismissal is a second read-state with
no UI to set it, `expires_at` serves NT-3's *delivery* staleness horizon and
there is no delivery channel yet, and a correlation id would be a column
nothing reads — the outbox row named by `source_event_id` already carries
one. Each is additive when its consumer arrives; a column that looks
maintained and is not is worse than an absent one.

## Why the payload is typed rather than free JSON

§3 forbids arbitrary unvalidated JSON. The stored shape is decided by the
`type`, decoded through `payload_of` on the way out, and a row whose payload
does not match its type raises rather than reaching a response — which is
what makes "the frontend supplies presentation, the backend supplies facts"
safe to rely on.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final
from uuid import UUID


class NotificationCategory(StrEnum):
    """The bounded families a future preference switch will address.

    Four, and the set is chosen so that a player muting one is muting
    something they can name. `marketing` is deliberately absent: this
    product defines no such notification, and a category nothing produces is
    a preference that silently does nothing.

    `SOCIAL`, `TOURNAMENT` and `GAME` all have producers as of A64-021.4.
    `SYSTEM` does not, and is the one a player may never mute — see
    `domain.preference.LOCKED`.
    """

    SOCIAL = "social"
    GAME = "game"
    TOURNAMENT = "tournament"
    SYSTEM = "system"


class NotificationType(StrEnum):
    """What happened, and therefore which sentence the client renders.

    The template key of `database.md` §10.2 under the name A64-021.1 gives
    it. **No rendered text is stored** — §15.2 of that document explains
    why, and it is the reason this enum is the contract: the backend states
    a fact, the frontend translates it into uz, ru or en.

    ## Six members, and each one earned its place

    A64-021.1 shipped two, and recorded the bar the rest had to clear: a
    real source event that *names its recipients*. A64-021.4 admits four
    more that clear it and defers the rest — see `specs/notifications.md`
    §14 for the deferral table, which names the missing seam in each case
    rather than the intention to build one.

    The bar matters because a durable notification is a permanent record.
    A type whose recipients had to be guessed would be a row somebody
    cannot explain receiving, and a type whose fact expires in seconds
    would be an inbox entry that is already wrong when it is read.
    """

    FRIEND_REQUEST_RECEIVED = "friend_request_received"
    """Someone sent you a friend request."""

    FRIEND_REQUEST_ACCEPTED = "friend_request_accepted"
    """Someone accepted the request you sent them."""

    TOURNAMENT_REGISTRATION_CONFIRMED = "tournament_registration_confirmed"
    """You are entered in a tournament — A64-021.4.

    A receipt, and the reason it is durable rather than a toast: entering a
    tournament is a commitment to turn up at a time the player does not
    choose, and the confirmation is the thing they look back for. A
    withdrawal does not delete it; it records what was true then."""

    TOURNAMENT_ROUND_PUBLISHED = "tournament_round_published"
    """A round of a tournament you are in has been paired."""

    TOURNAMENT_COMPLETED = "tournament_completed"
    """A tournament you played in is over, and the results are final."""

    FRIEND_CHALLENGE_RECEIVED = "friend_challenge_received"
    """A friend invited you to play — A64-022.4.

    Durable because a challenge **waits**: it stays answerable for
    twenty-four hours, and a recipient who was not in the app when it
    arrived has no other way to find it. That is the same bar
    `TOURNAMENT_REGISTRATION_CONFIRMED` clears and the opposite of the
    pairing events A64-021.4 refused — those resolve in under a minute, so a
    row about one is stale before it is read.
    """

    FRIEND_CHALLENGE_ACCEPTED = "friend_challenge_accepted"
    """Your challenge was accepted, and the game exists — A64-022.4.

    The **handoff**: the match is created at acceptance and both players
    must still join it, so this row is how a challenger who was away learns
    that there is a board waiting. It carries the match id, which is why its
    target is the live game rather than a list.
    """

    GAME_COMPLETED = "game_completed"
    """A game you played has finished — A64-021.4.

    Durable **because the participant may not have been watching**: a
    tournament no-show adjudication, a clock expiry on a closed tab, or an
    abandonment all end a match with nobody looking at it. A player who
    was at the board saw the result live and gets a row they can ignore;
    one who was not gets the only notice there is."""


#: Which family each type belongs to. A mapping rather than a field on the
#: enum, so that a type cannot be added without deciding its category —
#: `CATEGORY_OF[new]` raises at import of the first user rather than
#: defaulting to something plausible.
CATEGORY_OF: Final[Mapping[NotificationType, NotificationCategory]] = {
    NotificationType.FRIEND_REQUEST_RECEIVED: NotificationCategory.SOCIAL,
    NotificationType.FRIEND_REQUEST_ACCEPTED: NotificationCategory.SOCIAL,
    NotificationType.TOURNAMENT_REGISTRATION_CONFIRMED: NotificationCategory.TOURNAMENT,
    NotificationType.TOURNAMENT_ROUND_PUBLISHED: NotificationCategory.TOURNAMENT,
    NotificationType.TOURNAMENT_COMPLETED: NotificationCategory.TOURNAMENT,
    NotificationType.GAME_COMPLETED: NotificationCategory.GAME,
    NotificationType.FRIEND_CHALLENGE_RECEIVED: NotificationCategory.SOCIAL,
    NotificationType.FRIEND_CHALLENGE_ACCEPTED: NotificationCategory.SOCIAL,
}


class NavigationTargetType(StrEnum):
    """Where tapping a notification takes the recipient — §6.

    A closed set of **internal** destinations. No URL is ever stored: an
    event-supplied URL would be an open redirect written into a table, and
    a pre-rendered path would bake this build's routing into rows that
    outlive it. The client maps a target type plus one safe identifier onto
    a route it already owns.

    Five members. `play` and `match_history` are named by A64-021.1 §6 and
    are still absent, for the reason the two social ones were the only
    members then: nothing produces them. A destination is added when a
    notification needs it, never in anticipation — an unreachable target
    type is a branch every client must handle and no server can send.
    """

    PLAYER_PROFILE = "player_profile"
    """`ref` is the player's **username** — the identifier `/players/{username}`
    takes. Usernames are not editable on this platform (`ProfileUpdateRequest`
    offers no field for one), so a stored username stays resolvable."""

    FRIEND_REQUESTS = "friend_requests"
    """The incoming-requests list. `ref` is `None`: the destination is the
    viewer's own page and carries no identifier to get wrong."""

    FRIENDS = "friends"
    """The friend list. `ref` is `None`.

    **Retired as a destination in A64-022.5, and kept because rows hold it.**
    A64-022.4 wrote it on every received challenge as a stated placeholder —
    there was no challenge surface to point at — and A64-022.5 built one, so
    `CHALLENGES` below is what new rows carry.

    This member stays because a notification is history: rows written in the
    interval say `friends`, and deleting the member would make them raise on
    the way out. It is now readable and unproducible, which is the correct
    end state for a target that was honest when it was written.
    """

    CHALLENGES = "challenges"
    """The challenge list — `/challenges`. `ref` is `None`.

    A64-022.5. `ref` is absent for `FRIEND_REQUESTS`'s reason: the
    destination is the viewer's own page, and it carries no identifier to
    get wrong. A challenge id in the route would name a row the recipient
    already sees at the top of that page, and would put an identifier in a
    URL for nothing.
    """

    TOURNAMENT = "tournament"
    """`ref` is the tournament's **id** — `/tournaments/{id}`. An id rather
    than a name: a name is not unique and is not a route parameter here."""

    LIVE_GAME = "live_game"
    """`ref` is the match's **id** — `/games/{id}`."""

    MATCH_REPLAY = "match_replay"
    """`ref` is the match's **id** — `/games/{id}/replay`.

    Separate from `LIVE_GAME` rather than derived from it by the client:
    which of the two a finished match should open is a *server* decision
    about whether the game is still being played, and a client that guessed
    would send somebody to a live board that ended yesterday."""


@dataclass(frozen=True, slots=True)
class NavigationTarget:
    """One destination, as a type and at most one safe identifier."""

    type: NavigationTargetType
    ref: str | None = None


@dataclass(frozen=True, slots=True)
class ActorSummary:
    """The player a social notification is *about*, as the recipient may see
    them at the moment the notification was written.

    **Composed through `PublicProfileComposer`**, never read from a table
    directly — so a field the actor withheld is not here, and there is no
    path from this record back to an unfiltered identity. That gate is
    applied by `SocialNotificationDispatcher`, which is also where the block
    list is re-read; this type only holds the result.

    The avatar is carried as an **object key and a version**, not a URL.
    Composing the URL needs the deployment's storage provider
    (`avatars.public.AvatarLinkBuilder`), and a stored URL would freeze one
    CDN hostname into every historical row — the same reason
    `users.public.AvatarReference` carries no URL either.
    """

    player_id: UUID
    username: str
    display_name: str | None
    avatar_object_key: str | None
    avatar_version: int


@dataclass(frozen=True, slots=True)
class TournamentSummary:
    """The tournament a notification is about — A64-021.4 §8.

    Four fields, and two of them are `None` for most types. That is
    deliberate: a player reading "round 3 is paired" and one reading "you
    finished 5th" are looking at the same tournament through the same row
    shape, and three near-identical payload classes would be three decoders,
    three wire keys and three frontend branches for one noun.

    **The name is a snapshot**, like `ActorSummary`'s display name and for
    the same reason: re-reading it at render time would cost one query per
    row (§31) and would rewrite history if a tournament were ever renamed.

    What is deliberately absent: the bracket, the standings table, the
    entrant list, the schedule. A notification says *what happened*; the
    tournament page says what it looks like — and a payload carrying a field
    of 128 players would put a tournament's whole state into every one of
    their inboxes.
    """

    tournament_id: UUID
    tournament_name: str

    round_number: int | None = None
    """Which round was paired. `None` for every type that is not about one."""

    final_rank: int | None = None
    """**This recipient's** finishing position, not the winner's.

    The one recipient-specific field on this platform's fan-out payloads,
    and it is what makes a completion notification worth sending: "the
    tournament ended" is on the page already, "you came 5th" is not.

    `None` when the tournament has no standing for them — a player who
    withdrew before seeding has no result, and inventing a rank would be
    reporting a placement they never earned.
    """


@dataclass(frozen=True, slots=True)
class GameResultSummary:
    """One finished game, from the recipient's own point of view — §8.

    `outcome` is **already resolved against this recipient**: the stored
    value is `win`, `loss` or `draw` for the person holding the row, not the
    match's `light_wins`. A client that had to work out which seat it was
    would need the seats, and the seats are two more players in a payload
    about one game.
    """

    match_id: UUID
    outcome: str
    """`win`, `loss` or `draw`. A closed vocabulary, resolved by
    `GameNotificationDispatcher` — see `RecipientOutcome`."""

    termination_reason: str
    """How it ended, as `game`'s own `TerminationReason` value. Carried
    because "you lost" and "you lost on time" are different sentences, and
    because an adjudicated result is the case this notification exists for."""

    opponent: ActorSummary | None
    """Who they played, through the privacy gate. `None` when the opponent
    has no public profile any more — a deactivated account still leaves a
    game that was played."""


@dataclass(frozen=True, slots=True)
class ChallengeSummary:
    """The friend challenge a notification is about — A64-022.4 §4.

    Two types share this shape, and two of its fields are `None` for one of
    them. That is `TournamentSummary`'s precedent and the same argument: an
    invitation and its acceptance are two moments of **one** challenge, so
    two payload classes would be two decoders, two wire keys and two client
    branches for one noun.

        expires_at   when the invitation stops being answerable. `None` on
                     an acceptance, which has already been answered
        match_id     the game acceptance produced. `None` on an invitation,
                     because there is no game until somebody says yes

    ## What is here and what is deliberately not

    The settings are here — a challenge that said only "somebody invited
    you" would make the recipient open a surface to learn what they were
    being asked to play, which is the reason `FriendChallengeCreated`
    carries them too.

    The **other player** is here as an `ActorSummary`, composed through the
    privacy gate exactly as every other social payload's actor is, and
    `None` when they no longer have a public profile. What is not here is
    anything about the *viewer*: no rating, no eligibility, no "can you
    accept this" — a notification is a record of a fact, and whether a
    challenge is still answerable is a question for the endpoint that
    answers it.
    """

    challenge_id: UUID
    opponent: ActorSummary | None
    """The **other** party: the challenger on a received invitation, the
    recipient on an accepted one. `None` when that account no longer has a
    public profile — the challenge still happened."""

    time_control_id: str
    """A `reference.TimeControlId` value. Carried as its stable code rather
    than as clock numbers, so a client renders it through the same catalogue
    every other surface reads."""

    variant: str
    """A `game.ProductVariant` value."""

    rated: bool
    expires_at: datetime | None = None
    match_id: UUID | None = None


#: The payload union. Four members, and the alias is what `payload_of`
#: dispatches on: a stored row is decoded against its own `type`, so a
#: payload written by a producer that no longer exists fails at the row
#: rather than reaching a client half-rendered.
NotificationPayload = ActorSummary | TournamentSummary | GameResultSummary | ChallengeSummary


@dataclass(frozen=True, slots=True)
class NotificationRecord:
    """One durable notification, owned by its recipient.

    Frozen: read state is changed by writing a row, not by mutating a value
    somebody is holding.
    """

    id: UUID
    recipient_id: UUID
    type: NotificationType
    category: NotificationCategory
    payload: NotificationPayload
    target: NavigationTarget
    source_event_id: UUID
    """The outbox entry that caused this — the durable half of exactly-once.

    Never published on the API (§16): it names an internal record, and a
    client has nothing to do with it.
    """

    created_at: datetime
    read_at: datetime | None = None

    @property
    def is_read(self) -> bool:
        return self.read_at is not None


@dataclass(frozen=True, slots=True)
class NotificationAnnouncement:
    """That a notification now exists — A64-021.2 §2.

    The value that crosses into `app.gateway`, and it is deliberately the
    **smallest** thing a client needs in order to know it should re-read.

    ## Why it carries three fields and not the notification

    §2 allows `notification_id`, `type` and `created_at`, and forbids
    everything else — no actor, no username, no avatar, no rendered text.
    That is not miserliness: a pushed payload is a **second copy** of a
    record the client is about to fetch anyway, and a second copy is a
    second thing that can be stale, be wrong, or leak. The frame says
    *something happened*; `GET /notifications` says what.

    It is also what makes the frame safe to send to a socket whose owner
    may have signed out, changed their privacy settings, or blocked the
    actor between the write and the push: none of those can change what
    these three fields mean.

    `recipient_id` is the **address**, not payload. The gateway uses it to
    choose a socket and never puts it on the wire — a client already knows
    who it is.
    """

    notification_id: UUID
    recipient_id: UUID
    type: NotificationType
    created_at: datetime

    @classmethod
    def of(cls, record: "NotificationRecord") -> "NotificationAnnouncement":
        """The announcement for a record that was **actually written**.

        A classmethod rather than a constructor call at the call site, so
        the projection is defined once and a field added to
        `NotificationRecord` does not silently appear on the wire.
        """
        return cls(
            notification_id=record.id,
            recipient_id=record.recipient_id,
            type=record.type,
            created_at=record.created_at,
        )


class MalformedNotification(ValueError):
    """A stored row whose payload does not match its type.

    Raised on the way *out*, so a row written by an older or broken producer
    fails loudly at the one place that can still refuse it, rather than
    reaching a client as a half-rendered card. §17 maps it to a stable code
    and never exposes what was wrong with it.
    """


def payload_as_json(payload: NotificationPayload) -> dict[str, Any]:
    """The stored form. Explicit, field by field.

    `dataclasses.asdict` would be shorter and would serialise whatever the
    dataclass grows next — including something that should not be stored.
    Writing the keys out means the stored contract changes only when
    somebody edits this function.

    Dispatched on the payload's own type rather than on the notification's,
    so encoding and decoding cannot disagree about which shape a row holds.
    """
    if isinstance(payload, ActorSummary):
        return _actor_as_json(payload)
    if isinstance(payload, TournamentSummary):
        return {
            "tournament_id": str(payload.tournament_id),
            "tournament_name": payload.tournament_name,
            "round_number": payload.round_number,
            "final_rank": payload.final_rank,
        }
    if isinstance(payload, ChallengeSummary):
        return {
            "challenge_id": str(payload.challenge_id),
            "time_control_id": payload.time_control_id,
            "variant": payload.variant,
            "rated": payload.rated,
            "expires_at": None if payload.expires_at is None else payload.expires_at.isoformat(),
            "match_id": None if payload.match_id is None else str(payload.match_id),
            # Nested rather than flattened, for the reason the game payload
            # below nests its own: the other player **is** an actor, and a
            # second spelling of a shape this file already encodes is a
            # second decoder to keep in step.
            "opponent": None if payload.opponent is None else _actor_as_json(payload.opponent),
        }
    return {
        "match_id": str(payload.match_id),
        "outcome": payload.outcome,
        "termination_reason": payload.termination_reason,
        # Nested rather than flattened into `opponent_*` keys, because the
        # opponent **is** an actor and flattening it would be a second
        # spelling of a shape this file already encodes.
        "opponent": None if payload.opponent is None else _actor_as_json(payload.opponent),
    }


def _actor_as_json(actor: ActorSummary) -> dict[str, Any]:
    return {
        "actor_player_id": str(actor.player_id),
        "actor_username": actor.username,
        "actor_display_name": actor.display_name,
        "actor_avatar_object_key": actor.avatar_object_key,
        "actor_avatar_version": actor.avatar_version,
    }


#: Which payload shape each type stores. A mapping rather than a chain of
#: `if`s, so a type added without a decoder raises at the row it was asked
#: for instead of falling through to something plausible.
_PAYLOAD_SHAPE: Final[Mapping[NotificationType, str]] = {
    NotificationType.FRIEND_REQUEST_RECEIVED: "actor",
    NotificationType.FRIEND_REQUEST_ACCEPTED: "actor",
    NotificationType.TOURNAMENT_REGISTRATION_CONFIRMED: "tournament",
    NotificationType.TOURNAMENT_ROUND_PUBLISHED: "tournament",
    NotificationType.TOURNAMENT_COMPLETED: "tournament",
    NotificationType.GAME_COMPLETED: "game",
    NotificationType.FRIEND_CHALLENGE_RECEIVED: "challenge",
    NotificationType.FRIEND_CHALLENGE_ACCEPTED: "challenge",
}


def payload_of(type_: NotificationType, stored: Mapping[str, Any]) -> NotificationPayload:
    """Decodes a stored payload against its type. Raises `MalformedNotification`.

    The dispatch is on `type_` rather than on the stored keys, which is the
    whole point of the seam: a row is read as the shape its type promises,
    so a payload written by a producer that has since changed fails here
    instead of being duck-typed into whichever class happens to match.
    """
    shape = _PAYLOAD_SHAPE.get(type_)
    if shape == "actor":
        return _actor_of(stored)
    if shape == "tournament":
        return _tournament_of(stored)
    if shape == "game":
        return _game_result_of(stored)
    if shape == "challenge":
        return _challenge_of(stored)
    # Unreachable while every member is mapped. Kept so that adding a type
    # without a decoder fails at the row rather than silently returning the
    # wrong shape.
    raise MalformedNotification(f"no payload decoder for {type_}")


def _tournament_of(stored: Mapping[str, Any]) -> TournamentSummary:
    try:
        return TournamentSummary(
            tournament_id=UUID(str(stored["tournament_id"])),
            tournament_name=str(stored["tournament_name"]),
            round_number=_optional_int(stored["round_number"]),
            final_rank=_optional_int(stored["final_rank"]),
        )
    except (KeyError, TypeError, ValueError) as malformed:
        raise MalformedNotification("stored payload does not match its type") from malformed


def _game_result_of(stored: Mapping[str, Any]) -> GameResultSummary:
    try:
        opponent = stored["opponent"]
        return GameResultSummary(
            match_id=UUID(str(stored["match_id"])),
            outcome=str(stored["outcome"]),
            termination_reason=str(stored["termination_reason"]),
            opponent=None if opponent is None else _actor_of(opponent),
        )
    except (KeyError, TypeError, ValueError) as malformed:
        raise MalformedNotification("stored payload does not match its type") from malformed


def _challenge_of(stored: Mapping[str, Any]) -> ChallengeSummary:
    try:
        opponent = stored["opponent"]
        return ChallengeSummary(
            challenge_id=UUID(str(stored["challenge_id"])),
            opponent=None if opponent is None else _actor_of(opponent),
            time_control_id=str(stored["time_control_id"]),
            variant=str(stored["variant"]),
            rated=bool(stored["rated"]),
            expires_at=_optional_instant(stored["expires_at"]),
            match_id=_optional_uuid(stored["match_id"]),
        )
    except (KeyError, TypeError, ValueError) as malformed:
        raise MalformedNotification("stored payload does not match its type") from malformed


def _actor_of(stored: Mapping[str, Any]) -> ActorSummary:
    try:
        return ActorSummary(
            player_id=UUID(str(stored["actor_player_id"])),
            username=str(stored["actor_username"]),
            display_name=_optional_str(stored["actor_display_name"]),
            avatar_object_key=_optional_str(stored["actor_avatar_object_key"]),
            avatar_version=int(stored["actor_avatar_version"]),
        )
    except (KeyError, TypeError, ValueError) as malformed:
        # The cause is chained (CLAUDE.md §9.4) so an operator sees which
        # key was wrong; the message that reaches a client does not.
        raise MalformedNotification("stored payload does not match its type") from malformed


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _optional_instant(value: Any) -> datetime | None:
    return None if value is None else datetime.fromisoformat(str(value))


def _optional_uuid(value: Any) -> UUID | None:
    return None if value is None else UUID(str(value))


__all__ = [
    "CATEGORY_OF",
    "ActorSummary",
    "ChallengeSummary",
    "GameResultSummary",
    "TournamentSummary",
    "NotificationAnnouncement",
    "MalformedNotification",
    "NavigationTarget",
    "NavigationTargetType",
    "NotificationCategory",
    "NotificationPayload",
    "NotificationRecord",
    "NotificationType",
    "payload_as_json",
    "payload_of",
]
