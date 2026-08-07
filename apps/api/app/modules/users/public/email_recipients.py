"""Who may receive email, and at what address — A64-021.5 §5.

`notifications` needs one thing from `users` that nothing else on this
platform needed: a **verified** address for a batch of players, plus the
locale to write to them in.

## Why a port of its own rather than a field on an existing one

`UserProfileReader` returns `UserRead`, which carries an address. Handing
`notifications` that would mean handing it every account field to get one,
and `PublicUserProfile` — the shape a stranger sees — deliberately has no
address at all. Neither is the right answer.

So this is a seventh narrow port, and what makes it safe is what it will not
do:

  it takes **ids**, never an address, so a caller cannot ask "send to this
    address" and cannot discover whether one exists by supplying it

  it returns nothing for a player it will not vouch for — no account, no
    address, unverified, deactivated — so the *absence* is the policy rather
    than a flag a consumer must remember to check

  it has no write of any kind. `EmailVerifier` marks an address verified and
    is a separate port; a consumer that could both read and confirm an
    address is a consumer that could confirm one

## Batch, and the fan-out that makes it matter

A tournament round in a full field is 128 recipients. A per-player read
there is 128 round trips on a worker pass — the N+1 CLAUDE.md §10.4 names,
on the one path where it is invisible in every test with two players.

The method takes a sequence and cannot be called with a single id, which is
what stops a loop being written by accident.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.core.enums import Locale


@dataclass(frozen=True, slots=True)
class EmailRecipient:
    """One player this platform is willing to email.

    Its existence *is* the eligibility check — see the module docstring.
    There is no `is_verified` field to inspect, because a caller holding one
    of these has already been told the address is verified, the account is
    active, and the address exists.

    A boolean would invite the branch this type removes: the first consumer
    to forget it would email an unconfirmed address, and the code would look
    exactly like working code.
    """

    user_id: UUID
    email: str
    locale: Locale
    """Which language to write in — the account's stored preference, never
    inferred from the address domain or from geography (§16)."""

    display_name: str
    """What to call them in a greeting. Falls back to the username, so this
    is never empty and a template never renders a bare comma."""


class EmailRecipientDirectory(Protocol):
    """`users`' published answer to "may we email these people, and where"."""

    async def recipients_for(self, user_ids: Sequence[UUID]) -> Mapping[UUID, EmailRecipient]:
        """The subset that may be emailed, keyed by id. One read.

        **Players who may not are absent rather than present and flagged.**
        An unknown id, a deactivated account, a missing address and an
        unverified one all produce the same absence, and the caller decides
        what to record about it — which it can, because it knows which ids it
        asked for.

        Collapsing four causes into one absence is deliberate. A consumer
        that could tell "no such account" from "unverified" would be an
        account-existence oracle for anybody who could reach it, and none of
        the four changes what a delivery does: it does not send.
        """
        ...


__all__ = ["EmailRecipient", "EmailRecipientDirectory"]
