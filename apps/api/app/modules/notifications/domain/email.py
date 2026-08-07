"""Which notifications may become email, and what happened when one did.
A64-021.5 §4, §6.

Framework-free (architecture.md §8). Two closed sets and one function, and
both sets are policy rather than mechanism: the transport can send any
notification, and this decides which ones it *should*.

## A preference is necessary and not sufficient

A64-021.3 gave every player a switch per `(category, channel)`. That answers
*"does this person want email about tournaments"*. It does not answer *"is
this **type** worth an email at all"*, and conflating the two would mean
that enabling tournament email signed a player up for one message per round
per player per bracket — which is how a transactional channel becomes the
thing people filter into a folder.

So a notification is emailed when **both** hold: the type is email-capable
here, and the recipient has not muted its category on this channel.

## What email is for, and the test each type had to pass

Email reaches somebody who is **not looking at Arena64**. That is its whole
value over the in-app list, and it is also its whole cost: an email is an
interruption in a place the player did not choose to be interrupted.

The test is therefore *"would this still matter to somebody who will not
open the site today?"* — and it is a high bar on purpose.
"""

from dataclasses import dataclass
from typing import Final

from app.modules.notifications.domain.record import NotificationType


@dataclass(frozen=True, slots=True)
class RenderedEmail:
    """One message, ready for a provider. Both parts, never one — §17.

    A value rather than a presentation type, so `application` can hold the
    *result* of rendering without importing the templates that produced it —
    `notifications layers point inward`, and the renderer arrives as a port.
    """

    subject: str
    text_body: str
    html_body: str


#: The types this platform will send as email.
#:
#: Three, all tournament, and the omissions are the decision:
#:
#:   `friend_request_received`  the answer to it is a button in the app, and
#:                              a request waiting is not time-critical. It is
#:                              also the type an abuser controls the rate of —
#:                              an email per request would make the inbox a
#:                              harassment surface the block list does not
#:                              reach
#:   `friend_request_accepted`  pleasant, and nothing follows from it
#:   `game_completed`           the player was at the board. The cases it
#:                              exists for — an adjudication, a flag on a
#:                              closed tab — are real but rare, and one email
#:                              per game is the wrong price for them
#:
#: A tournament is different in kind: entering one is a commitment to be
#: somewhere at a time the platform chooses, and a round published while
#: somebody is away is the one notification whose value *decays*. Those are
#: exactly the messages worth an interruption.
EMAIL_CAPABLE_TYPES: Final[frozenset[NotificationType]] = frozenset(
    {
        NotificationType.TOURNAMENT_REGISTRATION_CONFIRMED,
        NotificationType.TOURNAMENT_ROUND_PUBLISHED,
        NotificationType.TOURNAMENT_COMPLETED,
    }
)


def supports_email(type_: NotificationType) -> bool:
    """Whether this platform sends this type as email at all."""
    return type_ in EMAIL_CAPABLE_TYPES


__all__ = ["EMAIL_CAPABLE_TYPES", "RenderedEmail", "supports_email"]
