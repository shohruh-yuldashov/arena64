"""The administrative audit trail — A64-024.8.

Sixteen tests over the **real** `AuditRecorder`, the **real** `AdminRoleService`
and the **real** route handler, with storage in memory. What is asserted is
the trail's contract: that a privileged action cannot happen without an
entry, that the entry names an actor nothing outside the server chose, that
what it carries is a typed slice and never a request, and that no route can
write one.

The append-only guarantee is **not** here. It is a PostgreSQL trigger, and
`tests/contract/test_admin_audit_entry.py` asserts it against a real
database — a fake refusing an update it never offers would prove nothing.
"""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi import HTTPException

from app.common.context import bind_context
from app.core.identifiers import generate_uuid7
from app.modules.admin.application.services import AdminRoleService, AuditLog, AuditRecorder
from app.modules.admin.domain.audit import (
    AuditAction,
    AuditActorType,
    AuditEntry,
    AuditOutcome,
    AuditSubjectType,
)
from app.modules.admin.domain.exceptions import LastAdministrator
from app.modules.admin.domain.roles import AdminRole
from app.modules.admin.presentation.routers.audit import (
    MAX_PAGE_SIZE,
    admin_audit_router,
    list_audit_entries,
)
from app.modules.admin.presentation.schemas.audit import (
    AuditActor,
    AuditEntryResponse,
    AuditPageResponse,
    AuditSubject,
)
from app.modules.users.public import AdminUserRecord
from tests.fakes.admin_audit import InMemoryAuditEntries
from tests.fakes.presence_redis import MovableClock
from tests.unit.test_admin_authorization import InMemoryRoleAssignments, NullUnitOfWork

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def _recorder(entries: InMemoryAuditEntries) -> AuditRecorder:
    return AuditRecorder(entries=entries, clock=MovableClock(NOW))


def _roles(assignments: InMemoryRoleAssignments, entries: InMemoryAuditEntries) -> AdminRoleService:
    return AdminRoleService(
        assignments=assignments,
        audit=_recorder(entries),
        unit_of_work=NullUnitOfWork(),  # type: ignore[arg-type]
        clock=MovableClock(NOW),
    )


class TestTheDomainRefusesADishonestActor:
    def test_an_administrator_entry_must_name_an_account(self) -> None:
        """The invariant that keeps `actor_type` meaningful.

        An `administrator` entry with no account would read as "somebody
        with an admin session did this, but we did not record who" — which
        is indistinguishable from an operator action while being a
        completely different fact about the deployment.
        """
        with pytest.raises(ValueError, match="must name an account"):
            AuditEntry(
                id=generate_uuid7(),
                actor_type=AuditActorType.ADMINISTRATOR,
                actor_id=None,
                action=AuditAction.ROLE_GRANTED,
                subject_type=AuditSubjectType.ACCOUNT,
                subject_ref=str(generate_uuid7()),
                outcome=AuditOutcome.SUCCEEDED,
                created_at=NOW,
            )

    def test_an_operator_entry_must_not_name_one(self) -> None:
        """The same invariant from the other side.

        An operator action has no account behind it by definition, so
        naming one would be a fabricated attribution — the one lie an audit
        trail cannot afford, because a reader could not tell it from a real
        administrator's action.
        """
        with pytest.raises(ValueError, match="no account to name"):
            AuditEntry(
                id=generate_uuid7(),
                actor_type=AuditActorType.OPERATOR,
                actor_id=generate_uuid7(),
                action=AuditAction.ROLE_GRANTED,
                subject_type=AuditSubjectType.ACCOUNT,
                subject_ref=str(generate_uuid7()),
                outcome=AuditOutcome.SUCCEEDED,
                created_at=NOW,
            )


class TestPrivilegedActionsAreRecorded:
    @pytest.mark.asyncio
    async def test_the_first_grant_is_an_operator_action_with_no_account(self) -> None:
        """The bootstrap case, and why `actor_type` exists at all.

        A deployment's first administrator is granted from a shell before
        any administrator exists. The entry records that honestly — actor
        type `operator`, no account — rather than inventing a placeholder
        id that a later reader would mistake for a person.
        """
        entries = InMemoryAuditEntries()
        roles = _roles(InMemoryRoleAssignments(), entries)
        first = generate_uuid7()

        await roles.bootstrap(account_id=first, role=AdminRole.ADMIN)

        assert len(entries.rows) == 1
        entry = entries.rows[0]
        assert entry.action is AuditAction.ROLE_GRANTED
        assert entry.actor_type is AuditActorType.OPERATOR
        assert entry.actor_id is None
        assert entry.subject_ref == str(first)
        assert entry.after == {"role": "admin", "granted_at": NOW.isoformat()}

    @pytest.mark.asyncio
    async def test_an_ordinary_grant_names_the_administrator_who_made_it(self) -> None:
        """§7 — the actor comes from the service's argument, never a payload.

        `granted_by` is the account the guard resolved, so there is no path
        by which the party being audited chooses what the entry says about
        them.
        """
        entries = InMemoryAuditEntries()
        assignments = InMemoryRoleAssignments()
        roles = _roles(assignments, entries)
        granter, subject = generate_uuid7(), generate_uuid7()
        await roles.bootstrap(account_id=granter, role=AdminRole.ADMIN)

        await roles.grant(account_id=subject, role=AdminRole.ADMIN, granted_by=granter)

        entry = entries.rows[-1]
        assert entry.actor_type is AuditActorType.ADMINISTRATOR
        assert entry.actor_id == granter
        assert entry.subject_ref == str(subject)
        # Nothing was held before, and an empty object says so more plainly
        # than a fabricated `{"role": null}` would.
        assert entry.before == {}

    @pytest.mark.asyncio
    async def test_a_revocation_records_what_ended_and_when_it_had_begun(self) -> None:
        """`before` and `after` are the two halves of one change.

        A reader reconstructing an incident needs to know the grant existed
        and since when — not merely that a revocation happened.
        """
        entries = InMemoryAuditEntries()
        roles = _roles(InMemoryRoleAssignments(), entries)
        first, second = generate_uuid7(), generate_uuid7()
        await roles.bootstrap(account_id=first, role=AdminRole.ADMIN)
        await roles.grant(account_id=second, role=AdminRole.ADMIN, granted_by=first)

        await roles.revoke(account_id=first, role=AdminRole.ADMIN, revoked_by=second)

        entry = entries.rows[-1]
        assert entry.action is AuditAction.ROLE_REVOKED
        assert entry.actor_id == second
        assert entry.subject_ref == str(first)
        assert entry.before == {"role": "admin", "granted_at": NOW.isoformat()}
        assert entry.after == {"role": "admin", "revoked_at": NOW.isoformat()}

    @pytest.mark.asyncio
    async def test_a_refused_action_writes_no_entry(self) -> None:
        """The trail records what happened, not what was attempted.

        `LastAdministrator` is raised **before** anything is written, so
        there is no revocation row and no entry claiming one. An entry for
        an action that did not occur is as damaging as a missing one — it
        would send an incident review after a change nobody made.
        """
        entries = InMemoryAuditEntries()
        roles = _roles(InMemoryRoleAssignments(), entries)
        only = generate_uuid7()
        await roles.bootstrap(account_id=only, role=AdminRole.ADMIN)
        appended_by_bootstrap = len(entries.rows)

        with pytest.raises(LastAdministrator):
            await roles.revoke(account_id=only, role=AdminRole.ADMIN, revoked_by=None)

        assert len(entries.rows) == appended_by_bootstrap

    @pytest.mark.asyncio
    async def test_the_entry_carries_the_correlation_id_of_its_request(self) -> None:
        """What joins an entry to the logs of the same request.

        Taken from the ambient context rather than passed in, so no call
        site can forget it — and `None` outside a request, which is honest
        rather than invented.
        """
        entries = InMemoryAuditEntries()
        recorder = _recorder(entries)

        with bind_context(correlation_id="corr-1"):
            await recorder.record_operator(
                action=AuditAction.ROLE_GRANTED,
                subject_type=AuditSubjectType.ACCOUNT,
                subject_ref=str(generate_uuid7()),
            )
        await recorder.record_operator(
            action=AuditAction.ROLE_GRANTED,
            subject_type=AuditSubjectType.ACCOUNT,
            subject_ref=str(generate_uuid7()),
        )

        assert [entry.correlation_id for entry in entries.rows] == ["corr-1", None]


class TestTheReadSurface:
    @pytest.mark.asyncio
    async def test_a_page_costs_one_trail_read_and_one_batch_of_names(self) -> None:
        """The N+1 this endpoint would naturally grow.

        Every entry names an actor and an account subject, so the obvious
        implementation resolves two accounts per row — a hundred lookups
        for a fifty-row page. Asserted by counting: **one** batch, holding
        the deduplicated set.
        """
        entries = InMemoryAuditEntries()
        roles = _roles(InMemoryRoleAssignments(), entries)
        granter = generate_uuid7()
        await roles.bootstrap(account_id=granter, role=AdminRole.ADMIN)
        subjects = [generate_uuid7() for _ in range(MAX_PAGE_SIZE - 1)]
        for subject in subjects:
            await roles.grant(account_id=subject, role=AdminRole.ADMIN, granted_by=granter)

        accounts = _Accounts({granter: "chief"})
        page = await _list(entries, accounts, limit=MAX_PAGE_SIZE)

        assert len(page.items) == MAX_PAGE_SIZE
        assert len(accounts.batches) == 1
        # The granter appears in every row and is asked for once, alongside
        # each distinct subject and the bootstrapped account.
        assert accounts.batches[0] == len(subjects) + 1

    @pytest.mark.asyncio
    async def test_filters_reach_the_port_as_typed_values(self) -> None:
        """Only index-backed filters exist, so nothing is post-filtered in
        the router and no free-text predicate reaches the database."""
        entries = InMemoryAuditEntries()
        roles = _roles(InMemoryRoleAssignments(), entries)
        first, second = generate_uuid7(), generate_uuid7()
        await roles.bootstrap(account_id=first, role=AdminRole.ADMIN)
        await roles.grant(account_id=second, role=AdminRole.ADMIN, granted_by=first)
        await roles.revoke(account_id=first, role=AdminRole.ADMIN, revoked_by=second)

        accounts = _Accounts({})

        revocations = await _list(entries, accounts, action=AuditAction.ROLE_REVOKED)
        assert [item.action for item in revocations.items] == ["admin.role.revoke"]

        theirs = await _list(entries, accounts, actor_id=second)
        assert {item.actor.account_id for item in theirs.items} == {second}

        about_first = await _list(
            entries,
            accounts,
            subject_type=AuditSubjectType.ACCOUNT,
            subject_ref=str(first),
        )
        assert {item.subject.ref for item in about_first.items} == {str(first)}

    @pytest.mark.asyncio
    async def test_the_cursor_continues_the_page_without_repeating_a_row(self) -> None:
        """Keyset, not offset.

        The trail grows at the head, so an `OFFSET` listing would show a
        different page four depending on what was written since page three
        — silently skipping entries in a record whose whole value is that
        nothing is missing from it.
        """
        entries = InMemoryAuditEntries()
        roles = _roles(InMemoryRoleAssignments(), entries)
        granter = generate_uuid7()
        await roles.bootstrap(account_id=granter, role=AdminRole.ADMIN)
        for _ in range(4):
            await roles.grant(account_id=generate_uuid7(), role=AdminRole.ADMIN, granted_by=granter)

        accounts = _Accounts({})
        first = await _list(entries, accounts, limit=2)
        assert first.next_cursor is not None

        second = await _list(entries, accounts, limit=2, cursor=first.next_cursor)
        third = await _list(entries, accounts, limit=2, cursor=second.next_cursor)

        seen = [item.id for page in (first, second, third) for item in page.items]
        assert len(seen) == 5
        assert len(set(seen)) == 5
        assert third.next_cursor is None

    @pytest.mark.asyncio
    async def test_the_response_is_never_cached(self) -> None:
        """This response is the record of who did what, and a copy of it in
        a shared cache is a copy nobody is accounting for."""
        headers = _Headers()
        await list_audit_entries(
            _Identity(),  # type: ignore[arg-type]
            AuditLog(entries=InMemoryAuditEntries()),
            _Accounts({}),  # type: ignore[arg-type]
            headers,  # type: ignore[arg-type]
        )
        assert headers.headers["Cache-Control"] == "no-store"

    @pytest.mark.asyncio
    async def test_a_subject_ref_without_its_type_is_refused(self) -> None:
        """Refused rather than ignored.

        A filter that quietly does nothing would show an operator the whole
        trail while they believe they are reading one account's history —
        and they would draw conclusions from it.
        """
        with pytest.raises(HTTPException) as refused:
            await _list(InMemoryAuditEntries(), _Accounts({}), subject_ref=str(generate_uuid7()))
        assert refused.value.status_code == 400

    @pytest.mark.asyncio
    async def test_an_erased_account_keeps_its_id_and_loses_only_its_name(self) -> None:
        """The trail outlives what it describes, which is the point.

        An entry about an account that has since been erased still renders:
        the id is a fact the entry holds, and `username` is `None` rather
        than a fabricated placeholder.
        """
        entries = InMemoryAuditEntries()
        roles = _roles(InMemoryRoleAssignments(), entries)
        gone = generate_uuid7()
        await roles.bootstrap(account_id=gone, role=AdminRole.ADMIN)

        page = await _list(entries, _Accounts({}))

        subject = page.items[0].subject
        assert subject.ref == str(gone)
        assert subject.username is None
        # And an operator action names nobody, because nobody acted.
        assert page.items[0].actor.type == "operator"
        assert page.items[0].actor.account_id is None


class TestWhatTheSurfaceCannotDo:
    def test_no_audit_route_writes_and_none_is_reachable_without_the_guard(self) -> None:
        """The two structural claims, asserted against the route table.

        There is no `POST /admin/audit`: an endpoint accepting entries
        would let anything holding an admin session write history,
        including history of things that never happened. And every route
        that does exist resolves `require_admin` before its body runs.
        """
        from app.modules.admin.presentation.dependencies import require_admin

        assert admin_audit_router.routes
        for route in admin_audit_router.routes:
            methods: set[str] = getattr(route, "methods", set())
            assert methods <= {"GET", "HEAD"}, methods

            dependant = getattr(route, "dependant", None)
            assert dependant is not None
            assert require_admin in {sub.call for sub in dependant.dependencies}, getattr(
                route, "path", route
            )

    def test_no_response_model_can_carry_credential_material(self) -> None:
        """§8, asserted as absence.

        Nothing in the response shape could carry a token, a session, a
        password hash or an address — so no serialisation path could leak
        one, whatever a future entry stores in `before`/`after`. The real
        enforcement is at the writing end, and this is the copy the
        boundary keeps.
        """
        forbidden = {
            "password",
            "password_hash",
            "token",
            "access_token",
            "refresh_token",
            "otp",
            "secret",
            "session",
            "session_id",
            "email",
            "ip",
            "ip_address",
            "device",
            "authorization",
            "cookie",
        }

        for model in (AuditActor, AuditSubject, AuditEntryResponse, AuditPageResponse):
            assert not set(model.model_fields) & forbidden, model.__name__

    def test_the_reader_service_exposes_no_write(self) -> None:
        """`AuditLog` is what a route holds, and it can only read.

        Appending lives on `AuditRecorder`, which is deliberately not wired
        to any route — so read-only here is structural rather than a
        convention somebody has to keep.
        """
        writes = {"append", "record", "record_administrator", "record_operator", "delete", "update"}
        assert not writes & {name for name in dir(AuditLog) if not name.startswith("_")}


class _Accounts:
    """`AdministrativeUserDirectory`, counting batch reads — that is the
    point of the fake."""

    def __init__(self, known: dict[UUID, str]) -> None:
        self.known = known
        self.batches: list[int] = []

    async def accounts_by_ids(self, user_ids: list[UUID]) -> dict[UUID, AdminUserRecord]:
        self.batches.append(len(user_ids))
        wanted = set(user_ids)
        return {
            user_id: AdminUserRecord(
                id=user_id,
                username=name,
                email=f"{name}@example.com",
                display_name=None,
                is_active=True,
                is_verified=True,
                created_at=NOW,
            )
            for user_id, name in self.known.items()
            if user_id in wanted
        }

    async def list_accounts(self, **_: object) -> None:  # pragma: no cover
        raise AssertionError("the audit router must not list accounts")

    async def find_account(self, user_id: UUID) -> None:  # pragma: no cover
        raise AssertionError("the audit router must not read accounts one at a time")


class _Headers:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}


class _Identity:
    def __init__(self) -> None:
        self.id = generate_uuid7()


async def _list(
    entries: InMemoryAuditEntries, accounts: _Accounts, **kwargs: object
) -> AuditPageResponse:
    """The **real** handler, over in-memory storage."""
    return await list_audit_entries(
        _Identity(),  # type: ignore[arg-type]
        AuditLog(entries=entries),
        accounts,  # type: ignore[arg-type]
        _Headers(),  # type: ignore[arg-type]
        action=kwargs.get("action"),  # type: ignore[arg-type]
        actor_id=kwargs.get("actor_id"),  # type: ignore[arg-type]
        subject_type=kwargs.get("subject_type"),  # type: ignore[arg-type]
        subject_ref=kwargs.get("subject_ref"),  # type: ignore[arg-type]
        limit=kwargs.get("limit", 25),  # type: ignore[arg-type]
        cursor=kwargs.get("cursor"),  # type: ignore[arg-type]
    )
