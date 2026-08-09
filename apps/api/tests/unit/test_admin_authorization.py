"""The admin authorization boundary — A64-024.1 §13.

Six tests over the **real** `AdminRoleService` and the **real**
`require_admin`, with an in-memory repository standing in for PostgreSQL.

## Why the guard is called directly rather than over HTTP

`require_admin` is a plain async function whose parameters FastAPI
resolves. Driving it through a `TestClient` would additionally exercise
token decoding and session wiring — both already covered by
`tests/unit/test_auth_api_contract.py` — and would need a database. What is
untested anywhere else is the *decision*: who is refused, in what order,
and whether the refusals are distinguishable.

The **401** case is deliberately absent from this file: an unauthenticated
caller never reaches `require_admin` at all, because `CurrentUser` raises
first. That is a property of the dependency chain, and the last test here
asserts the chain rather than re-testing `CurrentUser`.
"""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.core.exceptions import PermissionDeniedError
from app.core.identifiers import generate_uuid7
from app.modules.admin.application.services import AdminRoleService
from app.modules.admin.domain.exceptions import (
    AlreadyGranted,
    LastAdministrator,
    NotGranted,
    SelfGrant,
)
from app.modules.admin.domain.roles import AdminRole, RoleAssignment
from app.modules.admin.presentation.dependencies import require_admin
from tests.fakes.presence_redis import MovableClock

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


class InMemoryRoleAssignments:
    """`RoleAssignmentRepository` as a list.

    Models the one property the service's correctness rests on: **only
    unrevoked grants count**. The partial unique index is not modelled —
    that is PostgreSQL's, and a fake that agreed with itself would prove
    nothing about it.
    """

    def __init__(self) -> None:
        self.rows: list[RoleAssignment] = []

    async def live_roles_for(self, account_id: UUID) -> frozenset[AdminRole]:
        return frozenset(
            row.role for row in self.rows if row.account_id == account_id and row.is_live
        )

    async def live_for(self, account_id: UUID, role: AdminRole) -> RoleAssignment | None:
        for row in self.rows:
            if row.account_id == account_id and row.role == role and row.is_live:
                return row
        return None

    async def add(self, assignment: RoleAssignment) -> RoleAssignment:
        self.rows.append(assignment)
        return assignment

    async def revoke(self, assignment: RoleAssignment) -> RoleAssignment:
        self.rows = [assignment if row.id == assignment.id else row for row in self.rows]
        return assignment

    async def live_holders_of(self, role: AdminRole) -> list[UUID]:
        return [row.account_id for row in self.rows if row.role == role and row.is_live]


class NullUnitOfWork:
    """A transaction boundary that does nothing. The service's writes are
    asserted through the repository, not through a commit."""

    async def __aenter__(self) -> "NullUnitOfWork":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


class StubProfiles:
    """A `UserService` shaped for what the guard asks of it.

    The guard reaches `users` through `UserProfileService`, which calls
    exactly one method — `get_user`. Standing in at that seam rather than at
    the repository keeps the fake to one method and keeps the test about
    the *decision* rather than about `users`' mapping layer.
    """

    def __init__(self, *, is_active: bool = True) -> None:
        self.is_active = is_active

    async def get_user(self, user_id: UUID) -> object:
        from app.modules.users.domain.entities import User
        from app.modules.users.domain.value_objects import Email, Username

        return User(
            id=user_id,
            username=Username("operator"),
            email=Email("operator@example.com"),
            password_hash="x",
            is_active=self.is_active,
            is_verified=True,
            created_at=NOW,
        )


def _service(assignments: InMemoryRoleAssignments) -> AdminRoleService:
    return AdminRoleService(
        assignments=assignments,
        unit_of_work=NullUnitOfWork(),  # type: ignore[arg-type]
        clock=MovableClock(NOW),
    )


class _Identity:
    """Structurally `auth.public.AuthenticatedUser` — the guard reads `id`."""

    def __init__(self, account_id: UUID) -> None:
        self.id = account_id


class TestTheAuthorizationDecision:
    @pytest.mark.asyncio
    async def test_an_authenticated_player_without_a_grant_is_refused(self) -> None:
        """§13.2 — the case that would let the whole panel through.

        An ordinary signed-in player is authenticated, enabled and holds no
        grant. Nothing about their request differs from an administrator's
        except the row, which is exactly why the row has to be read.
        """
        assignments = InMemoryRoleAssignments()
        player = generate_uuid7()

        with pytest.raises(PermissionDeniedError):
            await require_admin(
                _Identity(player),  # type: ignore[arg-type]
                StubProfiles(),  # type: ignore[arg-type]
                _service(assignments),
            )

    @pytest.mark.asyncio
    async def test_a_granted_account_is_admitted_and_a_revoked_one_is_not(self) -> None:
        """§13.3 and §10's staleness question, in one test.

        The same identity is admitted, then refused, with **no token
        change** in between — because the guard reads storage on every call
        rather than a claim minted once. That is the whole answer to "what
        happens when an admin is demoted while their access token is still
        valid": nothing survives the revocation.
        """
        assignments = InMemoryRoleAssignments()
        service = _service(assignments)
        admin, granter = generate_uuid7(), generate_uuid7()
        await assignments.add(
            RoleAssignment(
                id=generate_uuid7(),
                account_id=granter,
                role=AdminRole.ADMIN,
                granted_by=None,
                granted_at=NOW,
            )
        )
        await service.grant(account_id=admin, role=AdminRole.ADMIN, granted_by=granter)

        identity = _Identity(admin)
        admitted = await require_admin(identity, StubProfiles(), service)  # type: ignore[arg-type]
        assert admitted.id == admin

        await service.revoke(account_id=admin, role=AdminRole.ADMIN)

        with pytest.raises(PermissionDeniedError):
            await require_admin(identity, StubProfiles(), service)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_a_disabled_account_is_refused_even_while_it_holds_the_role(self) -> None:
        """§13.5 — account state outranks the grant.

        A grant outlives a disabled account, and checking the role first
        would admit one on the strength of a row that survived the
        account's ability to sign in. The refusal is also **identical** to
        the one an ordinary player gets: a caller cannot tell "disabled"
        from "not an administrator", so neither answer reveals the other.
        """
        assignments = InMemoryRoleAssignments()
        admin = generate_uuid7()
        await assignments.add(
            RoleAssignment(
                id=generate_uuid7(),
                account_id=admin,
                role=AdminRole.ADMIN,
                granted_by=None,
                granted_at=NOW,
            )
        )

        with pytest.raises(PermissionDeniedError) as refused:
            await require_admin(
                _Identity(admin),  # type: ignore[arg-type]
                StubProfiles(is_active=False),  # type: ignore[arg-type]
                _service(assignments),
            )

        assert "administrative access is not available" in str(refused.value)


class TestGrantingAndRevoking:
    @pytest.mark.asyncio
    async def test_the_bootstrap_path_closes_behind_itself(self) -> None:
        """§13.6 — the back door is single-use.

        `bootstrap` is the only way to create a grant with no granter, and
        it refuses the moment the role has any holder. Without that refusal
        it would remain a permanent unattributed promotion path on a
        running deployment, reachable by anybody who can run the command.
        """
        assignments = InMemoryRoleAssignments()
        service = _service(assignments)

        await service.bootstrap(account_id=generate_uuid7(), role=AdminRole.ADMIN)

        with pytest.raises(AlreadyGranted):
            await service.bootstrap(account_id=generate_uuid7(), role=AdminRole.ADMIN)

    @pytest.mark.asyncio
    async def test_an_administrator_cannot_grant_the_role_to_themselves(self) -> None:
        """§10 — the escalation the whole module exists to prevent.

        If holding the ability to call `grant` were enough to acquire the
        role, the guard would be decoration. Also asserts the ordinary
        duplicate refusal, which is the same method's other guard.
        """
        assignments = InMemoryRoleAssignments()
        service = _service(assignments)
        first, second = generate_uuid7(), generate_uuid7()
        await service.bootstrap(account_id=first, role=AdminRole.ADMIN)

        with pytest.raises(SelfGrant):
            await service.grant(account_id=first, role=AdminRole.ADMIN, granted_by=first)

        await service.grant(account_id=second, role=AdminRole.ADMIN, granted_by=first)
        with pytest.raises(AlreadyGranted):
            await service.grant(account_id=second, role=AdminRole.ADMIN, granted_by=first)

    @pytest.mark.asyncio
    async def test_the_last_administrator_cannot_be_revoked(self) -> None:
        """The lockout this platform could not recover from.

        Granting requires an administrator and `bootstrap` refuses while
        one exists, so a deployment that revokes its only administrator has
        no route back — the database would have to be edited by hand. The
        refusal lifts as soon as a second administrator exists.
        """
        assignments = InMemoryRoleAssignments()
        service = _service(assignments)
        first, second = generate_uuid7(), generate_uuid7()
        await service.bootstrap(account_id=first, role=AdminRole.ADMIN)

        with pytest.raises(LastAdministrator):
            await service.revoke(account_id=first, role=AdminRole.ADMIN)

        await service.grant(account_id=second, role=AdminRole.ADMIN, granted_by=first)
        revoked = await service.revoke(account_id=first, role=AdminRole.ADMIN)
        assert not revoked.is_live

        # And revoking what is not held is refused rather than silently
        # succeeding — a no-op here would hide a typo'd account id.
        with pytest.raises(NotGranted):
            await service.revoke(account_id=first, role=AdminRole.ADMIN)


class TestTheBoundaryIsStructural:
    def test_no_registration_or_profile_field_can_confer_a_role(self) -> None:
        """§13.4 — mass assignment, closed by construction.

        The strongest available assertion is about **absence**: authority
        is a row in `admin.role_assignment`, and `users.User` — the entity
        every registration and profile update writes — has no field that
        could carry one. There is therefore no payload, no query parameter
        and no update path through which a player can supply it.
        """
        from app.modules.users.domain.entities import User

        fields = set(User.__dataclass_fields__)
        assert not fields & {"role", "roles", "is_admin", "is_staff", "permissions"}

    def test_every_admin_route_is_guarded_by_the_router_itself(self) -> None:
        """§13.1 and §4 — a route cannot be added unguarded.

        The guard is a router-level dependency, so a handler added by a
        later A64-024.x task is administrative by existing rather than by
        its author remembering to annotate it. This is also where the
        `401` lives: `require_admin` depends on `CurrentUser`, which raises
        before any of this file's logic runs.
        """
        from app.modules.admin.presentation.router import admin_router

        guards = {dependency.dependency for dependency in admin_router.dependencies}
        assert require_admin in guards
        assert admin_router.routes, "the router must actually carry a route"
