"""`users`' published answer to "who may we announce to" — A64-027A §14.

A **second** directory beside `EmailRecipientDirectory`, and not a method on
it, because they answer different questions for different channels. The
email port resolves a known set of ids into addresses; this one enumerates
an audience nobody has named yet, and returns ids and nothing else.

## Why it returns ids and no other field

A broadcast expander needs to know *who*, then hands each id to the
notification writer. It has no use for a username, a display name or a
locale, and a port that supplied them would be a bulk export of the user
table reachable from a worker — the sort of convenience that is invisible
until somebody logs it.

## Eligibility is this module's rule

`is_active` and `is_verified`, the same two predicates the email directory
applies for the same reasons: a deactivated account is not somebody to
write to, and an unverified one may not belong to the person who typed the
address. The consumer does not get to relax them, which is what makes the
console's recipient count a number the platform stands behind rather than
one a caller composed.
"""

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID


class NotificationAudienceDirectory(Protocol):
    """Counting and paging the accounts an announcement may reach."""

    async def count_eligible(self) -> int:
        """How many accounts an announcement would reach.

        Computed here rather than estimated by a caller: §14 forbids a
        recipient count the console invented, because it is the number an
        administrator reads before deciding to send to everybody.

        It is a count at *this instant*, not a promise. Accounts are created
        and deactivated while a broadcast is delivering, and the delivered
        total is what the history reports afterwards.
        """
        ...

    async def page_eligible(self, *, after: UUID | None, limit: int) -> Sequence[UUID]:
        """The next page of eligible ids, ordered by id.

        A keyset (`after`) rather than an offset, because the audience
        changes underneath a long delivery: an offset would skip accounts
        when one ahead of the cursor is deactivated, and repeat them when
        one is created.

        Ordered by `id` rather than by creation time because `id` is the
        primary key and therefore both unique and indexed — a keyset over a
        non-unique column needs a tiebreaker and gives nothing back.
        """
        ...


__all__ = ["NotificationAudienceDirectory"]
