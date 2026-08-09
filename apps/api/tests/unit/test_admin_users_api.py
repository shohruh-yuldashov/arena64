"""The admin Users read surface — A64-024.3 §19.

Six tests over the **real** router handlers, the **real** `AdminRoleService`
and an in-memory directory. What is asserted is the contract an operator and
an attacker each see: which fields leave the server, how many reads a page
costs, and that the guard is on every route.

The `403` and `401` paths are not repeated here — `require_admin` is the
same guard `tests/unit/test_admin_authorization.py` already covers, and the
last test asserts that these routes depend on it rather than re-testing what
it does.
"""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.core.identifiers import generate_uuid7
from app.modules.admin.application.services import AdminRoleService, AuditRecorder
from app.modules.admin.domain.roles import AdminRole
from app.modules.admin.presentation.routers.users import (
    MAX_PAGE_SIZE,
    admin_users_router,
    list_users,
    read_user,
)
from app.modules.admin.presentation.schemas.users import AdminUserDetail, AdminUserSummary
from app.modules.users.public import AdminUserFilters, AdminUserPage, AdminUserRecord
from tests.fakes.admin_audit import InMemoryAuditEntries
from tests.fakes.presence_redis import MovableClock
from tests.unit.test_admin_authorization import InMemoryRoleAssignments, NullUnitOfWork

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


class InMemoryDirectory:
    """`AdministrativeUserDirectory` over a list.

    Models the three behaviours the routes depend on — prefix matching over
    **both** identifiers, the two filters, and a bounded page — and models
    nothing about SQL. The index plan is PostgreSQL's and a fake agreeing
    with itself would prove nothing about it.
    """

    def __init__(self, records: list[AdminUserRecord]) -> None:
        self.records = records
        self.calls = 0

    async def list_accounts(
        self, *, term: str | None, filters: AdminUserFilters, limit: int, cursor: str | None
    ) -> AdminUserPage:
        self.calls += 1
        rows = list(self.records)
        if term is not None:
            lowered = term.lower()
            rows = [
                row
                for row in rows
                if row.username.lower().startswith(lowered) or row.email.lower().startswith(lowered)
            ]
        if filters.is_active is not None:
            rows = [row for row in rows if row.is_active is filters.is_active]
        if filters.is_verified is not None:
            rows = [row for row in rows if row.is_verified is filters.is_verified]
        return AdminUserPage(
            records=rows[:limit], next_cursor="next" if len(rows) > limit else None
        )

    async def find_account(self, user_id: UUID) -> AdminUserRecord | None:
        self.calls += 1
        return next((row for row in self.records if row.id == user_id), None)


class Headers:
    """A `Response` stand-in that records the headers a route sets."""

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}


def _record(username: str, *, active: bool = True, verified: bool = True) -> AdminUserRecord:
    return AdminUserRecord(
        id=generate_uuid7(),
        username=username,
        email=f"{username}@example.com",
        display_name=None,
        is_active=active,
        is_verified=verified,
        created_at=NOW,
    )


def _roles(assignments: InMemoryRoleAssignments) -> AdminRoleService:
    return AdminRoleService(
        assignments=assignments,
        audit=AuditRecorder(entries=InMemoryAuditEntries(), clock=MovableClock(NOW)),
        unit_of_work=NullUnitOfWork(),  # type: ignore[arg-type]
        clock=MovableClock(NOW),
    )


class _Identity:
    def __init__(self, account_id: UUID) -> None:
        self.id = account_id


async def _list(directory: InMemoryDirectory, roles: AdminRoleService, **kwargs: object):
    return await list_users(
        _Identity(generate_uuid7()),  # type: ignore[arg-type]
        directory,  # type: ignore[arg-type]
        roles,
        Headers(),  # type: ignore[arg-type]
        q=kwargs.get("q"),  # type: ignore[arg-type]
        is_active=kwargs.get("is_active"),  # type: ignore[arg-type]
        is_verified=kwargs.get("is_verified"),  # type: ignore[arg-type]
        limit=kwargs.get("limit", 25),  # type: ignore[arg-type]
        cursor=kwargs.get("cursor"),  # type: ignore[arg-type]
    )


class TestTheListSurface:
    @pytest.mark.asyncio
    async def test_it_matches_a_prefix_of_either_identifier(self) -> None:
        """§19.3 — an operator's two starting points.

        A support request carries an address; a report carries a handle.
        Both have to find the account, and neither should find anybody
        else's — so a term that is a prefix of one account's email must not
        return the account whose *username* merely contains it.
        """
        directory = InMemoryDirectory([_record("alice"), _record("bob"), _record("alicia")])
        roles = _roles(InMemoryRoleAssignments())

        by_handle = await _list(directory, roles, q="ali")
        assert {item.username for item in by_handle.items} == {"alice", "alicia"}

        by_email = await _list(directory, roles, q="bob@example")
        assert [item.username for item in by_email.items] == ["bob"]

        assert (await _list(directory, roles, q="nobody")).items == []

    @pytest.mark.asyncio
    async def test_the_two_filters_narrow_independently(self) -> None:
        """§19.4. Both default to "either", so an unfiltered list is the
        default and a caller states only what it narrows."""
        directory = InMemoryDirectory(
            [
                _record("live", active=True, verified=True),
                _record("disabled", active=False, verified=True),
                _record("unconfirmed", active=True, verified=False),
            ]
        )
        roles = _roles(InMemoryRoleAssignments())

        assert len((await _list(directory, roles)).items) == 3
        assert [
            item.username for item in (await _list(directory, roles, is_active=False)).items
        ] == ["disabled"]
        assert [
            item.username for item in (await _list(directory, roles, is_verified=False)).items
        ] == ["unconfirmed"]

    @pytest.mark.asyncio
    async def test_a_page_costs_one_directory_read_and_one_role_read(self) -> None:
        """§19.8 and §10 — no N+1, whatever the page size.

        The role annotation is the tempting place to loop: "is this row an
        admin" is a per-row question with a per-row answer available. It is
        answered instead by one whole-set read, because administrators are
        a handful of accounts and the set is smaller than any page it
        annotates.

        Asserted by counting reads across a fifty-row page — a per-row
        implementation would make this fifty-one.
        """
        directory = InMemoryDirectory([_record(f"player{index}") for index in range(MAX_PAGE_SIZE)])
        assignments = InMemoryRoleAssignments()
        roles = _roles(assignments)
        await roles.bootstrap(account_id=directory.records[0].id, role=AdminRole.ADMIN)
        # Setup is not what is being counted — `bootstrap` reads the holder
        # set to refuse a second unattributed grant.
        assignments.reads_of_holders.clear()

        page = await _list(directory, roles, limit=MAX_PAGE_SIZE)

        assert len(page.items) == MAX_PAGE_SIZE
        assert directory.calls == 1
        assert len(assignments.reads_of_holders) == 1
        # And the annotation is correct: exactly the bootstrapped account.
        assert [item.username for item in page.items if item.is_admin] == ["player0"]


class TestWhatLeavesTheServer:
    def test_the_response_models_carry_no_credential_material(self) -> None:
        """§19.7 — the assertion that matters most, and it is about absence.

        Neither response type has a field that could carry a password hash,
        a token, an OTP secret or a session. There is therefore no
        serialisation path that could leak one, whatever a future
        `UserModel` gains — and `_to_admin_record` maps field by field
        rather than by reflection, so a new column does not silently widen
        this.
        """
        forbidden = {
            "password",
            "password_hash",
            "refresh_token",
            "access_token",
            "token",
            "otp",
            "otp_hash",
            "secret",
            "session",
            "sessions",
        }

        for model in (AdminUserSummary, AdminUserDetail):
            assert not set(model.model_fields) & forbidden, model.__name__

        # `email` **is** exposed, deliberately — an operator's starting
        # point is a support request. Asserted so its removal is a decision
        # rather than an accident.
        assert "email" in AdminUserSummary.model_fields

    @pytest.mark.asyncio
    async def test_detail_reports_the_admin_grant_and_answers_404_for_nobody(self) -> None:
        """§19.6. The detail adds one fact a list row does not carry —
        *when* authority was granted — and answers plainly for an id that
        matches nothing."""
        from fastapi import HTTPException

        subject = _record("operator")
        directory = InMemoryDirectory([subject])
        assignments = InMemoryRoleAssignments()
        roles = _roles(assignments)
        await roles.bootstrap(account_id=subject.id, role=AdminRole.ADMIN)

        detail = await read_user(
            subject.id,
            _Identity(generate_uuid7()),  # type: ignore[arg-type]
            directory,  # type: ignore[arg-type]
            roles,
            Headers(),  # type: ignore[arg-type]
        )
        assert detail.is_admin is True
        assert detail.admin_role_granted_at == NOW

        with pytest.raises(HTTPException) as missing:
            await read_user(
                generate_uuid7(),
                _Identity(generate_uuid7()),  # type: ignore[arg-type]
                directory,  # type: ignore[arg-type]
                roles,
                Headers(),  # type: ignore[arg-type]
            )
        assert missing.value.status_code == 404


class TestTheGuardIsOnEveryRoute:
    def test_no_users_route_can_be_reached_without_the_admin_dependency(self) -> None:
        """§19.1 and §15 — the reachability proof.

        Both handlers name `CurrentAdmin`, so FastAPI resolves
        `require_admin` before either body runs. Asserted against the
        **route table** rather than by reading the source, because a route
        added later without it is the failure this catches.
        """
        from app.modules.admin.presentation.dependencies import require_admin

        assert admin_users_router.routes, "the router must carry routes"
        for route in admin_users_router.routes:
            dependencies = getattr(route, "dependant", None)
            assert dependencies is not None
            named = {
                sub.call
                for sub in dependencies.dependencies  # type: ignore[attr-defined]
            }
            assert require_admin in named, getattr(route, "path", route)
