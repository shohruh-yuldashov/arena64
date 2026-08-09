"""What another module may ask about account restrictions — A64-024.6.

`domain-model.md` §6 draws the arrow this module exists to serve:

    ADMIN -->|"sanctions gate"| AUTH

`admin` owns the restriction; `auth` enforces it at the credential
boundary. This is the published face of that — one read, no write, and
deliberately almost no information.

## Why the DTO carries so little

`auth` needs to answer one question: may this account obtain a credential
right now. It does not need the category, the reasoning, the case, the
administrator who decided, or the sanction's id — and a port that offered
them would put moderation reasoning one call away from the sign-in path,
which is where it would eventually reach a response body.

`until` is the single field, and it is there because "temporarily
unavailable" and "unavailable" are different things to say to somebody who
has just been refused. Whether the platform *does* say the difference is a
product decision `specs/admin.md` records; this port is what makes it
possible without a second call.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class AccountRestriction:
    """An account's access is withheld, and possibly until when.

    Its existence is the fact. `until` is `None` for an indefinite
    restriction, which is not the same as permanent: every restriction is
    reversible by a lift, and `None` says only that no clock will end it.
    """

    until: datetime | None


class AccountRestrictionGate(Protocol):
    """Whether an account is restricted, right now.

    **One read and no write.** A consumer can learn that access is withheld
    and cannot withhold it — the same narrowing `AdminAuthority` makes, and
    for the same reason: a capability published to the platform is a
    capability reachable from anywhere that already has a session.

    Answered from storage rather than from a token claim. A restriction
    baked into a fifteen-minute access token would be a restriction that
    outlives its own lifting, and the direction of that error is somebody
    locked out after being reinstated.
    """

    async def restriction_for(self, player_id: UUID, *, at: datetime) -> AccountRestriction | None:
        """The restriction in force at `at`, or `None`.

        `None` for an account that has never been restricted and for one
        whose restrictions have all expired or been lifted —
        indistinguishably, because the answer to "may they sign in" is the
        same and the difference is not the caller's business.

        Never raises for an unknown account: an id that matches nothing is
        restricted by nothing.
        """
        ...


__all__ = ["AccountRestriction", "AccountRestrictionGate"]
