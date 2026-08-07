"""What this platform pushes, and what a push message may say — A64-021.6 §11, §15.

Framework-free. Two decisions live here: **which** notification types are
worth interrupting somebody for, and **what** may travel in a payload a push
service can see the size and timing of.

## Why the type set is closed and small

A push is an interruption. Email arrives in a list somebody chooses to open;
a push lights up a lock screen, and the cost of getting it wrong is that the
whole channel is switched off — not one category, the channel.

`GAME_COMPLETED` is deliberately absent: a player who just finished a game
is looking at the result screen, and pushing it to the phone in their pocket
notifies them of something they are already reading.

## The friend types, and the concern that deferred them — A64-021.6A

A64-021.6 left `FRIEND_REQUEST_RECEIVED` and `FRIEND_REQUEST_ACCEPTED` out
on the grounds that a friend request is attacker-controllable: anybody can
send one, so a type on this list is a type a stranger can use to make
somebody's phone buzz.

That concern was real and is **already answered**, which is what A64-021.6A
established — the bound it asked for exists three times over, and none of it
is push-specific:

    friend_request_send_user   20 per hour, per *sender*. A stranger gets
                               twenty buzzes an hour across the whole
                               platform, not twenty per victim
    blocking                   a blocked player cannot send a request at
                               all, so the recipient's own control removes
                               the sender entirely
    duplicates                 a second pending request to the same person
                               is refused, so a sender cannot repeat

And the one that decides it: a push exists **only** where a durable
notification does. Every rule above runs before the notification is written,
so a request that is rate limited, blocked, duplicate or malformed produces
no notification and therefore no push. There is no path from a refused
request to a delivery row (§3).

What is left is a stranger's twenty-an-hour, which is the same volume the
in-app badge and the realtime frame already carry — and a person who does
not want it mutes the `social` category, which the preference matrix has
offered since A64-021.3.
"""

from dataclasses import dataclass
from typing import Final
from uuid import UUID

from app.modules.notifications.domain.record import NotificationType

#: The notification types this platform sends as a push notification.
#:
#: Three, and the same three the email channel sends — which is not a
#: coincidence and is not laziness. These are the notifications a player
#: cannot see coming: a round is published while they are away from the
#: tab, a registration is confirmed after they closed it, a tournament ends
#: overnight. Everything else on this platform happens while they are
#: looking at it.
PUSH_CAPABLE_TYPES: Final[frozenset[NotificationType]] = frozenset(
    {
        # The tournament three — A64-021.6. Notifications a player cannot
        # see coming: a round published while they are away from the tab, a
        # registration confirmed after they closed it, a tournament that
        # ends overnight.
        NotificationType.TOURNAMENT_REGISTRATION_CONFIRMED,
        NotificationType.TOURNAMENT_ROUND_PUBLISHED,
        NotificationType.TOURNAMENT_COMPLETED,
        # The social two — A64-021.6A. Both are somebody *waiting* on the
        # other person: a request nobody sees is a request that expires, and
        # an acceptance is the moment two people can actually play. See the
        # module docstring on why the abuse concern that deferred these is
        # already answered by the rules that run before a notification
        # exists at all.
        NotificationType.FRIEND_REQUEST_RECEIVED,
        NotificationType.FRIEND_REQUEST_ACCEPTED,
    }
)


def supports_push(type_: NotificationType) -> bool:
    """Whether this platform pushes this type at all."""
    return type_ in PUSH_CAPABLE_TYPES


@dataclass(frozen=True, slots=True)
class PushPayload:
    """Everything a push message carries — A64-021.6 §11.

    ## Two identifiers, and nothing a push service could read to somebody

    A push payload is encrypted (RFC 8291), so a push service cannot read
    it. That is a reason to keep it small, not a reason to relax about
    contents: the encryption protects it in transit, and what it lands in is
    a browser's notification store, on a device that may be shared, showing
    on a lock screen.

    So this carries the notification's id and its type. From those the
    service worker renders a fixed, translated sentence from a table
    compiled into it (§12, approach B), and the app fetches the real
    notification when the person opens it — behind their session, which is
    the only place a private detail belongs.

    ## What is deliberately absent

        the body          "Round 3 is live in the Tashkent Open" names a
                          tournament the person is in. On a lock screen in
                          public, that is a disclosure they did not choose
        a display name    the same argument, and worse: it names a *person*
        an email          never, anywhere near this channel
        a URL            §13. The service worker maps a type to a route from
                          a closed table; a URL in a payload is a navigation
                          primitive handed to whatever wrote the payload
        any token         a push payload is stored by the browser and
                          survives the tab

    ## Why the type is here at all

    It is the one field that lets the service worker say something more
    useful than "Arena64". It is a value from a closed enum this platform
    defines, it names a *category of event* rather than an instance, and it
    is what §13's click mapping keys on. A payload with only an id would
    force either a generic string or an authenticated fetch from a worker
    that may have no session — see `presentation/push_payload.py` on why
    that fetch is not attempted.
    """

    notification_id: UUID
    type: NotificationType

    def as_dict(self) -> dict[str, str]:
        """The wire form — short keys, because 4 KB is the budget.

        `n` and `t` rather than `notification_id` and `type`: the encrypted
        envelope has a fixed 86-byte overhead and every push service caps
        the result, so the key names are pure cost. The service worker's
        reader is the only consumer and it is compiled from the same
        decision.
        """
        return {"n": str(self.notification_id), "t": self.type.value}


__all__ = ["PUSH_CAPABLE_TYPES", "PushPayload", "supports_push"]
