"""Moderation — the decision and the restriction it authorised. A64-024.6.

`domain-model.md` §13.2 and §13.3 designed these two before anything needed
them, and DM-12 is the rule that keeps them apart:

> the sanction is read on **every sign-in, every message send and every
> queue entry** — it is a hot authorization input. The case is read by
> moderators, rarely, and is large.

This module implements that separation. It does not redesign it.

## Why a case exists at all when there is no report queue

`database.md` §10.4 makes `sanction.case_id` a `NOT NULL` foreign key and
§13.3 states the rule in words: *"A sanction names the case that authorised
it."* An administrator acting directly **is** a decision-maker, so the case
is created closed, in the same transaction, naming them and their reasoning.

What is deliberately **not** built: reports, evidence, a case inbox, case
assignment, review, appeals. A case here is the authority for one
restriction and nothing else — but it is a real one, so the day reports
arrive they attach to a table that already exists rather than to a
`NOT NULL` column that has to be backfilled with fabricated rows.

## Why `is_active` is not reused

§6's account lifecycle draws the two transitions separately:

    Active --> Suspended:    sanction applied
    Active --> Deactivated:  player-initiated

and `User.deactivate` says the same in its own docstring — deactivation is
"a reversible state **a player can choose**". Overloading it would make
"did they leave or were they removed" unanswerable from the data, and §6's
ownership rule forbids it outright: *"`admin` may request suspension
through a published port; it never writes account rows."*

## Expiry is a comparison, never a job

§13.3: *"Expiry is by instant, evaluated at read time, never by a job that
'removes' sanctions — because a job that fails leaves players banned."*
`is_effective_at` is that comparison, and it is the only definition of
"currently restricted" on the platform.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class SanctionKind(StrEnum):
    """What a sanction withholds.

    **One member, and that is the honest count.** §13.3 names four kinds —
    muted, matchmaking-restricted, suspended, banned — and only one of them
    has an enforcement seam on this platform today.

    `MUTED` (quick messages withheld) and `MATCHMAKING_RESTRICTED` would
    each need a guard on a surface that currently has none, and a kind an
    administrator can apply while nothing enforces it is worse than an
    absent one: the console would report a restriction the player does not
    experience. `BANNED` is deferred for a different reason — §6's
    lifecycle ties it to erasure ("Suspended → Erased: permanent ban plus
    erasure request"), which is DM-13's obligation and not this task's.

    An indefinite suspension is already expressible: `expires_at` is null.
    """

    SUSPENDED = "suspended"
    """Authentication is withheld. See `specs/admin.md` for the exact
    enforcement points and the bounded window on already-issued tokens."""


class ModerationCategory(StrEnum):
    """Why the decision was taken — a **closed** vocabulary.

    Machine-readable identifiers; the console localises them (uz/ru/en).
    Free text is not the authoritative reason: a taxonomy nobody can filter
    or count is a taxonomy that cannot answer "how often does this happen",
    which is the first question asked of any moderation surface.

    Chosen from surfaces Arena64 **actually has**, not from a generic
    trust-and-safety list:

    - `CHEATING` — `fairplay` collects integrity signals (§13.1), and IS-1
      is explicit that a signal never sanctions automatically. A human
      deciding on that evidence is exactly this category.
    - `ABUSE` — the quick-message surface (ADR-004) and the block graph.
      **`harassment` and `spam` are not separate members**: there is no
      free-text channel to distinguish them on, quick messages are already
      rate-limited (A64-023.3), and three categories that no evidence can
      tell apart would be three categories filled in at random.
    - `ACCOUNT_COMPROMISE` — a real operational case: `auth` already has
      lockout and password reset, and withholding access to a stolen
      account is a protective act rather than a punitive one.
    - `POLICY_VIOLATION` — the bounded catch-all for a rule that exists but
      has no dedicated evidence surface.
    - `OTHER` — the honest escape hatch. `reasoning` is required on every
      case, so this one is not a hole in the record.
    """

    CHEATING = "cheating"
    ABUSE = "abuse"
    ACCOUNT_COMPROMISE = "account_compromise"
    POLICY_VIOLATION = "policy_violation"
    OTHER = "other"


class CaseStatus(StrEnum):
    """Where a case is in its life.

    One member today: a case created by a direct administrative action is
    decided at the moment it is opened, so it is born `CLOSED`. An `OPEN`
    member would describe a queue this platform does not have, and §13.2's
    immutability rule — "a case is immutable once closed" — is trivially
    kept when nothing is ever open.
    """

    CLOSED = "closed"


#: The longest reasoning a case may carry.
#:
#: Bounded because it is stored forever in a record nobody may delete from,
#: and because an unbounded text column on an administrative form is where
#: pasted logs, tokens and whole request bodies end up. Long enough for a
#: paragraph explaining a decision; too short to paste a stack trace into.
MAX_REASONING_LENGTH = 500


@dataclass(frozen=True, slots=True)
class ModerationCase:
    """The decision record — §13.2.

    Frozen. §13.2: *"an editable moderation record cannot be trusted in an
    appeal, which is the only situation in which anybody reads it."* There
    is no mutator here and the repository offers no update.

    `reasoning` is **required**, not optional. A decision record whose
    reasoning is blank answers nothing at the only moment it is read, and
    making it conditional on `OTHER` would mean the routine cases — the
    ones a reviewer most often has to reconstruct — are the empty ones.
    """

    id: UUID
    subject_player_id: UUID
    category: ModerationCategory
    status: CaseStatus
    opened_by: UUID
    """The administrator who decided. **Never from a payload** — the
    identity the guard resolved, and §13.2's "every case names a human
    decision-maker"."""

    opened_at: datetime
    closed_at: datetime
    decision: str
    """What was decided, as a machine-readable identifier — the sanction
    kind applied, or `no_action`. Not prose: prose belongs in `reasoning`."""

    reasoning: str

    def __post_init__(self) -> None:
        if not self.reasoning.strip():
            raise ValueError("a moderation case must state its reasoning")
        if len(self.reasoning) > MAX_REASONING_LENGTH:
            raise ValueError(f"reasoning is limited to {MAX_REASONING_LENGTH} characters")
        if self.opened_by == self.subject_player_id:
            # §13.2: "a moderator may not act on a case involving
            # themselves." Enforced here as well as in the service, so a
            # future caller that skipped the service cannot construct one.
            raise ValueError("a moderator may not open a case about themselves")


@dataclass(frozen=True, slots=True)
class Sanction:
    """The enforced restriction — §13.3.

    Frozen, and lifting returns a new value rather than mutating: the row
    is updated in place by the repository, but nothing above it can end a
    restriction by assignment.
    """

    id: UUID
    player_id: UUID
    case_id: UUID
    """The case that authorised it. Never null — see the module docstring."""

    kind: SanctionKind
    starts_at: datetime
    expires_at: datetime | None
    """`None` means indefinite. Not "forever": a lift ends it, and that is
    what makes every restriction reversible."""

    created_at: datetime
    lifted_at: datetime | None = None
    lifted_by: UUID | None = None

    def __post_init__(self) -> None:
        if self.expires_at is not None and self.expires_at <= self.starts_at:
            raise ValueError("a sanction cannot expire before it begins")
        if (self.lifted_at is None) != (self.lifted_by is None):
            # Half a lift is not a state: "ended, by nobody" and "ended by
            # somebody, at no time" are both unreadable in an appeal.
            raise ValueError("a lifted sanction records both when and by whom")

    def is_effective_at(self, instant: datetime) -> bool:
        """Whether this restriction is in force at `instant`.

        **The platform's only definition of "currently restricted."** Two
        comparisons and no query: expiry is by instant (§13.3), so a
        sanction that has run out simply stops answering `True` — no job
        has to run, and a job that failed to run cannot leave anybody
        restricted past their sentence.

        A lifted sanction is never effective, whatever its expiry, because
        lifting is a decision by a named person and outranks the clock.
        """
        if self.lifted_at is not None:
            return False
        if instant < self.starts_at:
            return False
        return self.expires_at is None or instant < self.expires_at

    def lift(self, *, at: datetime, by: UUID) -> "Sanction":
        """Ends the restriction, naming who ended it and when.

        Returns a new value; the caller persists it. Raises when there is
        nothing to lift, because "lifted twice" would overwrite the first
        lifter's name with the second's — losing exactly the attribution
        §13.3 requires.
        """
        if self.lifted_at is not None:
            raise ValueError("this sanction has already been lifted")
        return Sanction(
            id=self.id,
            player_id=self.player_id,
            case_id=self.case_id,
            kind=self.kind,
            starts_at=self.starts_at,
            expires_at=self.expires_at,
            created_at=self.created_at,
            lifted_at=at,
            lifted_by=by,
        )


__all__ = [
    "MAX_REASONING_LENGTH",
    "CaseStatus",
    "ModerationCase",
    "ModerationCategory",
    "Sanction",
    "SanctionKind",
]
