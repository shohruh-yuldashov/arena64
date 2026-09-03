"""In-memory moderation storage — A64-024.6.

What is faked is **storage**, never the thing under test: `ModerationService`,
the route handlers and `Sanction.is_effective_at` all run for real against
these.

## What they model, and what they deliberately do not

They model the one property every caller's correctness rests on: at most one
**unlifted** sanction of a kind per account, which is `uq_sanction__live_kind`
in PostgreSQL. Modelled because it is what makes a duplicate restriction a
refusal rather than two contradictory live rows.

They do **not** model the transaction. A rollback that discards a case, a
sanction, a session revocation and an audit entry together is the database's
job, and a fake that agreed with itself about it would prove nothing — the
atomicity is asserted against a real session in
`tests/contract/test_admin_moderation.py`.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from uuid import UUID

from app.core.exceptions import ConflictError
from app.modules.admin.application.ports import SanctionPage
from app.modules.admin.domain.moderation import ModerationCase, Sanction, SanctionKind
from app.modules.admin.public import AccountRestriction


class InMemoryModerationCases:
    """`admin.moderation_case`, as a list. **No update method** — §13.2."""

    def __init__(self) -> None:
        self.rows: list[ModerationCase] = []

    async def add(self, case: ModerationCase) -> ModerationCase:
        self.rows.append(case)
        return case

    async def cases_by_ids(self, case_ids: Sequence[UUID]) -> Mapping[UUID, ModerationCase]:
        wanted = set(case_ids)
        return {case.id: case for case in self.rows if case.id in wanted}


class InMemorySanctions:
    """`admin.sanction`, as a list."""

    def __init__(self) -> None:
        self.rows: list[Sanction] = []

    async def add(self, sanction: Sanction) -> Sanction:
        if any(
            row.player_id == sanction.player_id
            and row.kind is sanction.kind
            and row.lifted_at is None
            for row in self.rows
        ):
            # `uq_sanction__live_kind`. Raised rather than appended, because
            # the service's refusal is the *readable* form of this and the
            # index is the form that survives two administrators acting at
            # the same instant.
            raise ConflictError("a live sanction of that kind already exists")
        self.rows.append(sanction)
        return sanction

    async def lift(self, sanction: Sanction) -> Sanction:
        self.rows = [sanction if row.id == sanction.id else row for row in self.rows]
        return sanction

    async def effective_for(self, player_id: UUID, *, at: datetime) -> Sequence[Sanction]:
        return [row for row in self.rows if row.player_id == player_id and row.is_effective_at(at)]

    async def count_effective(self, *, at: datetime) -> int:
        """How many restrictions are in force at `at` — the dashboard's
        number, over the same predicate `effective_for` uses."""
        return sum(1 for row in self.rows if row.is_effective_at(at))

    async def live_of_kind(self, player_id: UUID, kind: SanctionKind) -> Sanction | None:
        return next(
            (
                row
                for row in self.rows
                if row.player_id == player_id and row.kind is kind and row.lifted_at is None
            ),
            None,
        )

    async def page(
        self, *, effective_at: datetime | None, limit: int, cursor: str | None
    ) -> SanctionPage:
        rows = sorted(self.rows, key=lambda row: (row.created_at, row.id), reverse=True)
        if effective_at is not None:
            rows = [row for row in rows if row.is_effective_at(effective_at)]

        if cursor is not None:
            after = [index for index, row in enumerate(rows) if str(row.id) == cursor]
            rows = rows[after[0] + 1 :] if after else []

        page = rows[:limit]
        has_more = len(rows) > limit
        return SanctionPage(
            sanctions=page, next_cursor=str(page[-1].id) if has_more and page else None
        )


class RecordingSessionRevoker:
    """`SessionRevoker`, counting what it was asked to end.

    SE-3 is asserted by *whether this was called inside the transaction*,
    not by what `auth` does with the call — that belongs to `auth`'s own
    tests, and duplicating it here would couple a moderation test to a
    session repository.
    """

    def __init__(self, *, live_sessions: int = 2) -> None:
        self.live_sessions = live_sessions
        self.revoked_for: list[UUID] = []

    async def revoke_all_for(self, user_id: UUID, *, at: datetime) -> int:
        self.revoked_for.append(user_id)
        return self.live_sessions


class UnrestrictedAccounts:
    """`admin.public.AccountRestrictionGate` that restricts nobody.

    The default for every `auth` test that is not about moderation. Named
    rather than defaulted on the service, because a security dependency
    with a permissive default is one a future wiring mistake can omit
    silently — the same reason `AdminRoleService` requires its recorder.
    """

    async def restriction_for(self, player_id: UUID, *, at: datetime) -> AccountRestriction | None:
        return None


class RestrictedAccounts:
    """The gate for the accounts it was told about."""

    def __init__(self, *restricted: UUID, until: datetime | None = None) -> None:
        self.restricted = set(restricted)
        self.until = until
        self.asked: list[UUID] = []

    async def restriction_for(self, player_id: UUID, *, at: datetime) -> AccountRestriction | None:
        self.asked.append(player_id)
        if player_id not in self.restricted:
            return None
        return AccountRestriction(until=self.until)


__all__ = [
    "InMemoryModerationCases",
    "InMemorySanctions",
    "RecordingSessionRevoker",
    "RestrictedAccounts",
    "UnrestrictedAccounts",
]
