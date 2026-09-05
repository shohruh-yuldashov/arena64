"""Every analytics event this platform has, and who is allowed to say it.

The table in `docs/01-architecture/analytics.md` §18 and this module are one
decision expressed twice; a test asserts they do not drift. What the
document explains, this enforces.

## The one thing that must be code rather than prose

`CLIENT_EMITTABLE` is **derived** from `Owner`, never written by hand.

A browser may report that somebody clicked a button. It may not report that
an account was created, that a match completed, or that a rating moved —
those are facts the server establishes, and a collector that accepted them
from a request body would let anyone write Arena64's own history. Deriving
the allowlist from ownership means the security boundary cannot drift from
the taxonomy: adding a backend event adds nothing a client may send, and
there is no second list for somebody to forget.

## Why a `StrEnum` rather than strings

The name is the join key between an event stored today and a query written
in a year. A typo in a string literal produces an event nobody counts and no
error; a typo here does not compile.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class Owner(StrEnum):
    """Which side of the system establishes this event.

    Not a deployment detail: it is what decides whether a client may submit
    the name at all (`CLIENT_EMITTABLE`).
    """

    #: A browser reporting an interaction. Untrusted, lossy, and fine to be
    #: both — analytics.md §36 states which metrics that costs.
    FRONTEND = "frontend"

    #: A fact the server established, reaching analytics through the outbox
    #: as a projection of a domain event that already exists.
    BACKEND = "backend"


class Trust(StrEnum):
    """How much a stored event may be relied on.

    `DERIVED` is deliberately absent. A derived quantity is a query, never a
    row — analytics.md §8: an emitted derivation is a number nobody can
    recompute, and it disagrees with its own inputs the first time the
    formula changes.
    """

    #: The server says this happened.
    AUTHORITATIVE = "authoritative"

    #: A browser reported it.
    BEHAVIOURAL = "behavioural"


class Identity(StrEnum):
    """Whose event this is — analytics.md §18's "Identity" column.

    The document specified this per event from the start; A64-027.1's
    registry did not encode it, so nothing could check it. A64-027.2 needs
    it, because "a row belonging to nobody" is a real category and
    "a row that lost its identity" is a defect, and without this they look
    identical in a table.
    """

    #: A browser, before there is an account. `anonymous_id` is required;
    #: a `subject_key` may also be present when a signed-in player fires a
    #: behavioural event.
    ANONYMOUS = "anonymous"

    #: A person. `subject_key` is required — the server derives it from the
    #: authenticated principal, never from a request body.
    ACTOR = "actor"

    #: A match or a tournament, not a person. `match_completed` is the
    #: example: it describes one game with two seats, and attributing it to
    #: one of them would double-count or pick a side. Neither identity
    #: field is set, and the event is counted by its own dimensions.
    ENTITY = "entity"


class EventName(StrEnum):
    """The complete taxonomy — analytics.md §18.

    Nineteen events, and the count is the point: every one of them is read
    by a metric in §29. An event nothing reads is volume with no question
    behind it, and the deliberate absences (`move_made`, a generic
    `page_view`, `login_succeeded`) are recorded there with reasons.
    """

    # --- acquisition: what a visitor with no account did ------------------
    LANDING_VIEWED = "landing_viewed"
    REGISTER_CTA_CLICKED = "register_cta_clicked"
    PUBLIC_TOURNAMENT_VIEWED = "public_tournament_viewed"
    SHARE_CLICKED = "share_clicked"

    # --- registration and activation --------------------------------------
    USER_REGISTERED = "user_registered"
    EMAIL_VERIFIED = "email_verified"

    # --- matchmaking -------------------------------------------------------
    QUEUE_JOINED = "queue_joined"
    QUEUE_LEFT = "queue_left"
    MATCH_FOUND = "match_found"
    MATCH_OFFER_RESOLVED = "match_offer_resolved"

    # --- game --------------------------------------------------------------
    MATCH_STARTED = "match_started"
    MATCH_COMPLETED = "match_completed"
    RATING_CHANGED = "rating_changed"

    # --- tournament ---------------------------------------------------------
    TOURNAMENT_ENTERED = "tournament_entered"
    TOURNAMENT_WITHDRAWN = "tournament_withdrawn"
    TOURNAMENT_COMPLETED = "tournament_completed"

    # --- social -------------------------------------------------------------
    FRIEND_REQUEST_SENT = "friend_request_sent"
    FRIENDSHIP_CREATED = "friendship_created"
    CHALLENGE_SENT = "challenge_sent"
    CHALLENGE_RESOLVED = "challenge_resolved"


@dataclass(frozen=True)
class EventSpec:
    """What the platform knows about one event before any of it is built."""

    name: EventName
    owner: Owner
    identity: Identity

    #: analytics.md §7. Bumped when a reader cannot detect the change —
    #: a removed property, a changed unit, a changed trigger. Adding an
    #: optional property is additive and bumps nothing.
    version: int = 1

    @property
    def trust(self) -> Trust:
        """Derived, because the two are the same fact stated twice.

        A backend event is a server fact and a frontend event is a
        browser's report; there is no fifth combination worth representing,
        and a hand-written `trust` field would be a second place for one of
        them to be wrong.
        """
        return Trust.AUTHORITATIVE if self.owner is Owner.BACKEND else Trust.BEHAVIOURAL


def _spec(name: EventName, owner: Owner, identity: Identity) -> tuple[EventName, EventSpec]:
    return name, EventSpec(name=name, owner=owner, identity=identity)


#: The taxonomy. Totality over `EventName` is asserted by a test, so an
#: event added to the enum and forgotten here fails the suite rather than
#: reaching a collector that cannot classify it.
REGISTRY: Final[Mapping[EventName, EventSpec]] = dict(
    [
        _spec(EventName.LANDING_VIEWED, Owner.FRONTEND, Identity.ANONYMOUS),
        _spec(EventName.REGISTER_CTA_CLICKED, Owner.FRONTEND, Identity.ANONYMOUS),
        _spec(EventName.PUBLIC_TOURNAMENT_VIEWED, Owner.FRONTEND, Identity.ANONYMOUS),
        _spec(EventName.SHARE_CLICKED, Owner.FRONTEND, Identity.ANONYMOUS),
        _spec(EventName.USER_REGISTERED, Owner.BACKEND, Identity.ACTOR),
        _spec(EventName.EMAIL_VERIFIED, Owner.BACKEND, Identity.ACTOR),
        _spec(EventName.QUEUE_JOINED, Owner.BACKEND, Identity.ACTOR),
        _spec(EventName.QUEUE_LEFT, Owner.BACKEND, Identity.ACTOR),
        _spec(EventName.MATCH_FOUND, Owner.BACKEND, Identity.ACTOR),
        _spec(EventName.MATCH_OFFER_RESOLVED, Owner.BACKEND, Identity.ACTOR),
        _spec(EventName.MATCH_STARTED, Owner.BACKEND, Identity.ACTOR),
        _spec(EventName.MATCH_COMPLETED, Owner.BACKEND, Identity.ENTITY),
        _spec(EventName.RATING_CHANGED, Owner.BACKEND, Identity.ACTOR),
        _spec(EventName.TOURNAMENT_ENTERED, Owner.BACKEND, Identity.ACTOR),
        _spec(EventName.TOURNAMENT_WITHDRAWN, Owner.BACKEND, Identity.ACTOR),
        _spec(EventName.TOURNAMENT_COMPLETED, Owner.BACKEND, Identity.ENTITY),
        _spec(EventName.FRIEND_REQUEST_SENT, Owner.BACKEND, Identity.ACTOR),
        _spec(EventName.FRIENDSHIP_CREATED, Owner.BACKEND, Identity.ACTOR),
        _spec(EventName.CHALLENGE_SENT, Owner.BACKEND, Identity.ACTOR),
        _spec(EventName.CHALLENGE_RESOLVED, Owner.BACKEND, Identity.ACTOR),
    ]
)


#: What a browser may submit — analytics.md §38.
#:
#: **Derived, and that is the security control.** The collector answers
#: `422` for anything outside this set, so `user_registered` from a request
#: body is rejected by construction rather than by a reviewer noticing.
CLIENT_EMITTABLE: Final[frozenset[EventName]] = frozenset(
    name for name, spec in REGISTRY.items() if spec.owner is Owner.FRONTEND
)


#: Property names that may never appear on an analytics event —
#: analytics.md §11.
#:
#: A denylist rather than a review convention, because the failure it
#: prevents is somebody adding the field that happened to be in scope. The
#: real rule is stricter and lives in the per-event schemas: properties are
#: **closed**, so an unknown key is rejected whether or not it is named
#: here. This catches the case where somebody adds it to a schema.
#:
#: `email_hash` is on the list deliberately: a hashed email is a stable
#: identifier for a person and joins across any system holding the same
#: hash. Hashing is not anonymising.
DENIED_PROPERTY_NAMES: Final[frozenset[str]] = frozenset(
    {
        "email",
        "email_hash",
        "hashed_email",
        "username",
        "display_name",
        "first_name",
        "last_name",
        "full_name",
        "phone",
        "phone_number",
        "password",
        "password_hash",
        "token",
        "access_token",
        "refresh_token",
        "session_token",
        "ip",
        "ip_address",
        "remote_addr",
        "user_agent",
        "avatar_url",
        "thumbnail_url",
        "bio",
        "country",
        "message",
        "message_text",
        "body",
        "content",
        "emoji",
        "notification_title",
        "notification_body",
        "moderation_note",
        "referrer",
        "url",
        "query_string",
        "tournament_name",
        "name",
        "error_message",
    }
)


def spec_for(name: EventName) -> EventSpec:
    """The registry entry for one event.

    Raises `KeyError` for a name the registry does not classify, which is
    the failure worth having at startup: an unclassified event is one no
    collector can decide to accept or reject.
    """
    return REGISTRY[name]


def is_client_emittable(name: str) -> bool:
    """Whether a browser may submit this name.

    Takes `str` rather than `EventName` on purpose — the caller is a
    collector holding a value out of a request body, and the answer for a
    name that is not in the taxonomy at all must be `False` rather than a
    `ValueError` the endpoint has to catch.
    """
    return name in {member.value for member in CLIENT_EMITTABLE}
