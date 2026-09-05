"""Notification preferences — A64-021.3, and the rules that decide what a
player may switch off.

Framework-free (architecture.md §8). This file holds the **policy**: which
channels exist, which of them work, what a player gets when they have never
touched a setting, and which switches are not theirs to flip.

## Where this lives, and why it is not in `users`

`database.md` §4.9 placed `notification_preference` in the `users` schema
and `domain-model.md` §9.3 said `notifications` "owns no preference data".
It lives here instead, and both documents are corrected in the same change
(CLAUDE.md §3.11). Three reasons, in the order they decided it:

  **The vocabulary is this module's.** §4.9's own column types are named
  `notifications.notification_category` and `notifications.delivery_channel`
  — the document had to reach into this module to describe a table it placed
  in another one.

  **The alternative is a cycle.** `notifications.application` already
  imports `users.public` for presence. Putting the table in `users` would
  make `users` — the base module every context depends on — import
  `notifications.public` for the enums, so two bounded contexts would depend
  on each other.

  **§9.3's actual rule survives.** Its concern is a *second copy*: "a player
  mutes a category in settings and keeps receiving it because a second copy
  was never updated." One owning table cannot produce that. What §9.3 also
  requires — that preferences are read at **delivery** time, not baked into
  an event — is unchanged and is `NotificationDeliveryPolicy`'s job.

## Sparse storage, explicit defaults

A row exists only where a player has **overridden** the default. A new
account has none, and

    effective = stored override, or the default below

That is not a storage optimisation. Materialising a dozen rows per account
would make every future category or channel a data migration over every
user, and it would make "has this player chosen, or are they simply on the
default" unanswerable — which is the question a later change of default has
to ask.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from app.core.error_codes import ErrorCode
from app.core.exceptions import RuleViolationError
from app.modules.notifications.domain.record import NotificationCategory


class DeliveryChannel(StrEnum):
    """Where a notification can reach a player.

    Three, and `sms` is deliberately absent: this product has no phone
    number for anybody and a channel with no address is a switch that
    cannot do anything.

    Only `IN_APP` is **implemented**. The other two are here because a
    settings screen that showed one channel would teach players that
    Arena64 has one, and because the preference table's vocabulary should
    not change on the day email ships — see `CHANNEL_AVAILABILITY`.
    """

    IN_APP = "in_app"
    """The notification list and its badge. Delivered by A64-021.1's durable
    writer and A64-021.2's gateway frame."""

    EMAIL = "email"
    """Deferred to A64-021.5. No provider, no template, no sender."""

    PUSH = "push"
    """A browser notification, delivered through Web Push — A64-021.6.

    Available only where a process holds a VAPID key pair *and* the player's
    browser has an active subscription. The first is `ChannelAvailability`'s
    question; the second is not a preference at all and is deliberately not
    modelled here — a player with push enabled and no subscribed device has
    asked to be pushed and has nowhere to be pushed to, which is a state the
    settings screen shows and this enum has no business encoding."""


#: Which channels this **build** can deliver on, before configuration.
#:
#: **A backend fact, and separate from what a browser supports.** A player
#: on a browser with `PushManager` still cannot receive a push from Arena64,
#: because Arena64 does not send one. The frontend shows browser capability
#: as *context*; this decides whether a switch may be turned on at all, and
#: the API refuses an attempt either way.
#:
#: ## Why `EMAIL` is `True` here and still frequently unavailable
#:
#: A64-021.5 built the whole email channel — the delivery table, the retry
#: policy, the templates, the worker — and did **not** choose a vendor,
#: because none has been selected and picking one silently is the decision
#: this codebase must not make on its own.
#:
#: So the question split in two. This constant answers *"has this build got
#: an email channel at all"* — it has. `ChannelAvailability` below answers
#: *"can this **process**, as configured, actually send one"*, and that is
#: what a settings screen is told. A deployment with only
#: `ConsoleEmailProvider` reports email unavailable and keeps saying "coming
#: soon", which is true.
#:
#: Splitting them rather than flipping this one is what stops the settings
#: screen lying (§26). A constant cannot know whether a provider was
#: configured, and a build flag that claimed it could would be a promise
#: made at compile time about a runtime fact.
CHANNEL_AVAILABILITY: Final[Mapping[DeliveryChannel, bool]] = {
    DeliveryChannel.IN_APP: True,
    DeliveryChannel.EMAIL: True,
    # A64-021.6. The channel is built: subscriptions, VAPID, the transport,
    # the delivery queue and the service worker handler all exist. Whether a
    # *process* can send one is `ChannelAvailability`'s question, and it is
    # answered by whether a VAPID key pair is configured — see
    # `platform/push/provider.py` on why the key pair is the switch and not
    # a second boolean beside it.
    DeliveryChannel.PUSH: True,
}


@dataclass(frozen=True, slots=True)
class ChannelAvailability:
    """What this **process**, as configured, can actually deliver on.

    Constructed once at the composition root from the transports that were
    wired, and threaded through every read and every refusal — see
    `effective` and `ensure_settable`, which take one rather than reaching
    for a module-level constant.

    Threading it is deliberately more work than a global. A global would be
    a second source of truth about whether email works, set at import time,
    in a process that decides at construction time — and the failure mode is
    a settings screen offering a switch nothing honours.
    """

    delivers: frozenset[DeliveryChannel]

    @classmethod
    def of(cls, *channels: DeliveryChannel) -> "ChannelAvailability":
        """The channels this process delivers on.

        Filtered against `CHANNEL_AVAILABILITY`, so a caller cannot enable a
        channel the *build* does not implement — configuring a push provider
        would not make push work, because nothing sends one.
        """
        return cls(frozenset(c for c in channels if CHANNEL_AVAILABILITY[c]))

    def can_deliver(self, channel: DeliveryChannel) -> bool:
        return channel in self.delivers


#: What a caller gets with no configuration read: in-app and nothing else.
#:
#: Not a default parameter anywhere. It is named so that "this code path
#: does not know what is configured" is a visible choice at the call site —
#: the same reason `NullNotificationAnnouncer` is a class rather than a
#: `None` check.
IN_APP_ONLY: Final[ChannelAvailability] = ChannelAvailability.of(DeliveryChannel.IN_APP)


class LockedReason(StrEnum):
    """Why a preference cannot be changed, as a stable code.

    A code rather than a sentence, for the reason every refusal on this
    platform carries one: the client renders the explanation in the
    player's language, and an English string on the wire would be a
    translation the backend chose.
    """

    ESSENTIAL = "essential"
    """A system notification the platform must be able to deliver — an
    account or security matter. See `LOCKED`."""

    CHANNEL_UNAVAILABLE = "channel_unavailable"
    """The channel is not implemented yet, so there is nothing to enable."""


#: The preferences a player may not switch off, as `(category, channel)`.
#:
#: **One entry, and the narrowness is the point.** §5 warns against
#: classifying everything as critical, and the test is concrete: could the
#: platform reasonably need to tell this player *in the product* about their
#: own account? `system` covers security and account matters — a password
#: change, a moderation action, an essential service notice — and a player
#: who muted those would have no way to be told their account had been
#: acted on.
#:
#: Social, game and tournament notifications are **not** locked. Nothing
#: about a friend request is essential, and a player who does not want them
#: is entitled to silence.
#:
#: Only `IN_APP` is locked even for `system`, because it is the only channel
#: that delivers. A locked-on `system` email would be a promise this build
#: cannot keep.
#: `ANNOUNCEMENT` is deliberately **absent**. A64-027A §15: a broadcast an
#: administrator composes is not an account or security matter, so a player
#: who has muted the category receives nothing. Adding it here would turn
#: one dropdown value into a way of reaching every muted inbox at once.
LOCKED: Final[frozenset[tuple[NotificationCategory, DeliveryChannel]]] = frozenset(
    {(NotificationCategory.SYSTEM, DeliveryChannel.IN_APP)}
)


def _default_for(category: NotificationCategory, channel: DeliveryChannel) -> bool:
    """What a player gets before they have chosen anything.

    **In-app on, everything else off**, and each half is a decision:

    *In-app defaults to on* because a notification list a player has to
    switch on is a list nobody discovers. The cost of a wrong default here
    is a row in a list they can mute in two clicks.

    *Email and push default to off* because they do not work. A default of
    "on" for an unimplemented channel would silently begin delivering on the
    day it ships, to every account that never asked — which is the "no
    hidden default should surprise users later" §4 names, and the reason it
    is worth being explicit that a channel arriving is not consent.

    **A64-021.5 built the email channel and did not change this.** That
    sentence above is exactly what would have happened: every account that
    had been told email was unavailable would have begun receiving it the
    day a provider was configured. So notification email is **opt-in** — a
    player who wants a tournament confirmation in their inbox turns it on,
    and everybody else is where they were left.

    The cost is that the channel ships quiet, and that is the correct
    direction to be wrong in. A player who wanted email and has to enable it
    is mildly inconvenienced; a player who did not and receives it has been
    emailed without consent.
    """
    return channel is DeliveryChannel.IN_APP


@dataclass(frozen=True, slots=True)
class PreferenceSetting:
    """One category on one channel, as a client needs to render it — §7.

    Four facts, and they are genuinely independent:

        enabled     what delivery will do right now
        available   whether this build can deliver on this channel at all
        editable    whether this player may change it
        locked_reason  why not, when they may not

    A client that had only `enabled` would have to guess the other three,
    and §7 is explicit that it must not: browser capability and backend
    capability are different facts, and a greyed-out switch with no
    explanation is worse than an absent one.
    """

    category: NotificationCategory
    channel: DeliveryChannel
    enabled: bool
    available: bool
    editable: bool
    locked_reason: LockedReason | None


def is_locked(category: NotificationCategory, channel: DeliveryChannel) -> bool:
    return (category, channel) in LOCKED


def default_enabled(
    category: NotificationCategory,
    channel: DeliveryChannel,
    availability: ChannelAvailability,
) -> bool:
    """The effective value with no override stored.

    A locked preference is always on: `LOCKED` means "cannot be switched
    off", which is only meaningful if it starts on. A channel this process
    cannot deliver on is always off, whatever the default would otherwise
    say — a channel that cannot deliver must never report `enabled`.
    """
    if not availability.can_deliver(channel):
        return False
    if is_locked(category, channel):
        return True
    return _default_for(category, channel)


def effective(
    overrides: Mapping[tuple[NotificationCategory, DeliveryChannel], bool],
    availability: ChannelAvailability,
) -> tuple[PreferenceSetting, ...]:
    """Every category on every channel, resolved.

    **The whole matrix, always.** A response that omitted the defaults would
    make the client reimplement `default_enabled`, and the two would drift
    the first time a default changed — which is the divergence §7's "the API
    should not force the frontend to guess defaults" exists to prevent.

    Ordered by the enum declarations, so two reads render identically and a
    diff of two responses is about their content.
    """
    return tuple(
        _resolve(category, channel, overrides, availability)
        for category in NotificationCategory
        for channel in DeliveryChannel
    )


def _resolve(
    category: NotificationCategory,
    channel: DeliveryChannel,
    overrides: Mapping[tuple[NotificationCategory, DeliveryChannel], bool],
    availability: ChannelAvailability,
) -> PreferenceSetting:
    available = availability.can_deliver(channel)
    locked = is_locked(category, channel)

    if not available:
        # An override may exist — a channel could be withdrawn after players
        # had chosen — and it is deliberately ignored rather than deleted.
        # The row is their answer for the day the channel returns.
        return PreferenceSetting(
            category=category,
            channel=channel,
            enabled=False,
            available=False,
            editable=False,
            locked_reason=LockedReason.CHANNEL_UNAVAILABLE,
        )

    if locked:
        return PreferenceSetting(
            category=category,
            channel=channel,
            enabled=True,
            available=True,
            editable=False,
            locked_reason=LockedReason.ESSENTIAL,
        )

    stored = overrides.get((category, channel))
    return PreferenceSetting(
        category=category,
        channel=channel,
        enabled=default_enabled(category, channel, availability) if stored is None else stored,
        available=True,
        editable=True,
        locked_reason=None,
    )


#: The wire code each refusal carries — §20.
#:
#: Declared as a mapping rather than branched on at the raise site, so a
#: `LockedReason` added without a code fails here rather than reaching a
#: client as a generic `rule_violation`.
_CODE_OF: Final[Mapping[LockedReason, ErrorCode]] = {
    LockedReason.ESSENTIAL: ErrorCode.NOTIFICATION_PREFERENCE_LOCKED,
    LockedReason.CHANNEL_UNAVAILABLE: ErrorCode.NOTIFICATION_CHANNEL_UNAVAILABLE,
}


class PreferenceRefused(RuleViolationError):
    """A requested change the policy does not permit — `422`.

    A **rule violation** rather than a permission error, and the distinction
    is a client behaviour rather than a taxonomy preference: nothing about
    the caller's authority is in question, and a `403` would send a client
    into its re-authentication path over a switch that nobody may flip.

    Carries the offending pair so the presentation layer can name it without
    re-deriving which of a batch failed. The *message* is never shown to a
    client; the code is.
    """

    def __init__(
        self,
        *,
        category: NotificationCategory,
        channel: DeliveryChannel,
        reason: LockedReason,
    ) -> None:
        super().__init__(
            f"{category.value}/{channel.value} refused: {reason.value}",
            code=_CODE_OF[reason],
        )
        self.category = category
        self.channel = channel
        self.reason = reason


def ensure_settable(
    category: NotificationCategory,
    channel: DeliveryChannel,
    enabled: bool,
    availability: ChannelAvailability,
) -> None:
    """Raises `PreferenceRefused` unless this change is legal.

    **The backend's own check, not a mirror of the UI's.** §5 is explicit
    that the frontend must not be the thing enforcing a lock, and this is
    where the enforcement lives: a hand-written request reaches the same
    function a form does.

    Enabling an unavailable channel is refused rather than stored, because a
    stored `true` on a dead channel is a value that begins delivering the
    day the channel ships, to somebody who was told it did not work.
    """
    if not availability.can_deliver(channel):
        raise PreferenceRefused(
            category=category, channel=channel, reason=LockedReason.CHANNEL_UNAVAILABLE
        )
    if is_locked(category, channel) and not enabled:
        raise PreferenceRefused(category=category, channel=channel, reason=LockedReason.ESSENTIAL)


__all__ = [
    "CHANNEL_AVAILABILITY",
    "IN_APP_ONLY",
    "LOCKED",
    "ChannelAvailability",
    "DeliveryChannel",
    "LockedReason",
    "PreferenceRefused",
    "PreferenceSetting",
    "default_enabled",
    "effective",
    "ensure_settable",
    "is_locked",
]
