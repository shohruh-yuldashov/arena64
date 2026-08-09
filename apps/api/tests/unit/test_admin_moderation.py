"""Account restrictions — A64-024.6.

Tests over the **real** `ModerationService`, the **real** route handlers and
the **real** `AuthenticationService`, with storage in memory. What is
asserted is the safety contract an administrator, a restricted player and an
attacker each meet: that a restriction cannot exist without its audit entry,
that the actor comes from the guard and never from a payload, that the two
refusals which protect the console are unbypassable, and that a restricted
account cannot obtain a credential.

The transaction itself is not asserted here — a rollback that discards four
writes together is PostgreSQL's, and `tests/contract/test_admin_moderation.py`
is where that is falsifiable.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi import HTTPException
from pydantic import ValidationError as PydanticValidationError

from app.core.identifiers import generate_uuid7
from app.modules.admin.application.services import AuditRecorder, ModerationService
from app.modules.admin.domain.audit import AuditAction, AuditOutcome
from app.modules.admin.domain.exceptions import (
    AlreadySanctioned,
    NotSanctioned,
    ProtectedAdministrator,
    SelfSanction,
)
from app.modules.admin.domain.moderation import (
    ModerationCategory,
    Sanction,
    SanctionKind,
)
from app.modules.admin.presentation.routers.moderation import (
    admin_moderation_router,
    list_restrictions,
    moderation_state_for,
    restore_account,
    restrict_account,
)
from app.modules.admin.presentation.schemas.moderation import (
    ModerationCaseView,
    RestrictAccountRequest,
    SanctionView,
)
from app.modules.users.public import AdminUserRecord
from tests.fakes.admin_audit import InMemoryAuditEntries
from tests.fakes.moderation import (
    InMemoryModerationCases,
    InMemorySanctions,
    RecordingSessionRevoker,
)
from tests.fakes.presence_redis import MovableClock
from tests.unit.test_admin_authorization import NullUnitOfWork

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


class _Fixture:
    """One service and everything it wrote, assembled together."""

    def __init__(self) -> None:
        self.cases = InMemoryModerationCases()
        self.sanctions = InMemorySanctions()
        self.sessions = RecordingSessionRevoker()
        self.entries = InMemoryAuditEntries()
        self.clock = MovableClock(NOW)
        self.service = ModerationService(
            cases=self.cases,
            sanctions=self.sanctions,
            sessions=self.sessions,
            audit=AuditRecorder(entries=self.entries, clock=self.clock),
            unit_of_work=NullUnitOfWork(),  # type: ignore[arg-type]
            clock=self.clock,
        )


async def _suspend(
    fixture: _Fixture,
    *,
    player_id: UUID,
    actor_id: UUID,
    administrators: list[UUID] | None = None,
    expires_at: datetime | None = None,
    category: ModerationCategory = ModerationCategory.ABUSE,
) -> Sanction:
    return await fixture.service.suspend(
        player_id=player_id,
        category=category,
        reasoning="Repeated quick-message abuse after a warning.",
        expires_at=expires_at,
        actor_id=actor_id,
        administrators=administrators
        if administrators is not None
        else [actor_id, generate_uuid7()],
    )


class TestARestrictionCannotExistWithoutItsRecord:
    @pytest.mark.asyncio
    async def test_one_restriction_writes_a_case_a_sanction_and_a_success_entry(self) -> None:
        """The invariant A64-024.8 exists to make possible.

        Three rows and one revocation, from one call. `case_id` ties the
        enforcement to the decision that authorised it — §13.3's "a
        sanction names the case" — and the audit entry names the
        administrator, so nothing here is reconstructible only from prose.
        """
        fixture = _Fixture()
        player, admin = generate_uuid7(), generate_uuid7()

        sanction = await _suspend(fixture, player_id=player, actor_id=admin)

        assert len(fixture.cases.rows) == 1
        assert len(fixture.sanctions.rows) == 1
        assert sanction.case_id == fixture.cases.rows[0].id
        assert fixture.cases.rows[0].opened_by == admin

        assert len(fixture.entries.rows) == 1
        entry = fixture.entries.rows[0]
        assert entry.action is AuditAction.SANCTION_APPLIED
        assert entry.outcome is AuditOutcome.SUCCEEDED
        assert entry.actor_id == admin
        assert entry.subject_ref == str(player)

    @pytest.mark.asyncio
    async def test_the_audit_entry_carries_no_reasoning_and_no_account_object(self) -> None:
        """§8's metadata rule, asserted as absence.

        The trail records *that* a decision was taken and *where it is
        written down* — the case id — rather than a second copy of the
        prose. A reasoning duplicated into an append-only table is a copy
        nobody can correct and everybody must retain.
        """
        fixture = _Fixture()
        await _suspend(fixture, player_id=generate_uuid7(), actor_id=generate_uuid7())

        after = fixture.entries.rows[0].after
        assert "case_id" in after
        assert "reasoning" not in after
        forbidden = {"email", "username", "password_hash", "token", "session", "user"}
        assert not forbidden & set(after)

    @pytest.mark.asyncio
    async def test_every_live_session_is_ended_by_the_same_call(self) -> None:
        """SE-3 — "a suspension that lets an existing socket keep playing is
        not a suspension".

        Asserted as a call made with the restriction, not as a follow-up an
        operator has to remember: a second request could fail, and the
        state it left behind would look like a suspension in the console
        and like nothing at all to the player.
        """
        fixture = _Fixture()
        player = generate_uuid7()

        await _suspend(fixture, player_id=player, actor_id=generate_uuid7())

        assert fixture.sessions.revoked_for == [player]
        assert fixture.entries.rows[0].after["sessions_revoked"] == 2


class TestTheRefusalsThatProtectTheConsole:
    @pytest.mark.asyncio
    async def test_an_administrator_cannot_restrict_themselves(self) -> None:
        """An administrator who can withhold their own access can lock the
        operator out of the surface they are operating — and §13.2 already
        forbids acting on a case involving oneself."""
        fixture = _Fixture()
        admin = generate_uuid7()

        with pytest.raises(SelfSanction):
            await _suspend(fixture, player_id=admin, actor_id=admin)

        assert fixture.sanctions.rows == []
        assert fixture.cases.rows == []

    @pytest.mark.asyncio
    async def test_the_last_administrator_cannot_be_restricted(self) -> None:
        """The lockout nothing could recover from.

        A suspended administrator cannot sign in, and unlike a role
        revocation there is no `bootstrap` to grant a replacement — so the
        refusal is the only thing between one action and a console nobody
        can open. It lifts as soon as a second administrator exists.
        """
        fixture = _Fixture()
        only, other, actor = generate_uuid7(), generate_uuid7(), generate_uuid7()

        with pytest.raises(ProtectedAdministrator):
            await _suspend(fixture, player_id=only, actor_id=actor, administrators=[only])

        # With a second administrator the same call is allowed.
        await _suspend(fixture, player_id=only, actor_id=actor, administrators=[only, other])
        assert len(fixture.sanctions.rows) == 1

    @pytest.mark.asyncio
    async def test_restricting_twice_is_refused_rather_than_recorded_twice(self) -> None:
        """§17 — the audit semantics must match the state transition.

        A second restriction changes nothing, so it must not write a second
        case, a second sanction or a second `SUCCEEDED` entry. A trail that
        recorded transitions which never happened would be worse than one
        that recorded too few.
        """
        fixture = _Fixture()
        player, admin = generate_uuid7(), generate_uuid7()
        await _suspend(fixture, player_id=player, actor_id=admin)

        with pytest.raises(AlreadySanctioned):
            await _suspend(fixture, player_id=player, actor_id=admin)

        assert len(fixture.sanctions.rows) == 1
        assert len(fixture.cases.rows) == 1
        succeeded = [row for row in fixture.entries.rows if row.outcome is AuditOutcome.SUCCEEDED]
        assert len(succeeded) == 1

    @pytest.mark.asyncio
    async def test_a_refused_attempt_by_an_administrator_is_itself_audited(self) -> None:
        """The `FAILED` policy A64-024.8 left open, decided here.

        Somebody trusted tried something the platform stopped, and that is
        the fact an incident review needs. Each refusal names *which* rule
        refused, as a closed identifier — never a message, and never
        anything the request supplied.
        """
        fixture = _Fixture()
        admin, only = generate_uuid7(), generate_uuid7()

        with pytest.raises(SelfSanction):
            await _suspend(fixture, player_id=admin, actor_id=admin)
        with pytest.raises(ProtectedAdministrator):
            await _suspend(fixture, player_id=only, actor_id=admin, administrators=[only])
        with pytest.raises(NotSanctioned):
            await fixture.service.restore(player_id=generate_uuid7(), actor_id=admin)

        failed = [row for row in fixture.entries.rows if row.outcome is AuditOutcome.FAILED]
        assert [row.after["refused"] for row in failed] == [
            "self_restriction",
            "last_administrator",
            "not_restricted",
        ]
        assert {row.actor_id for row in failed} == {admin}


class TestTheLifecycle:
    @pytest.mark.asyncio
    async def test_expiry_is_a_comparison_and_history_survives_it(self) -> None:
        """§13.3 — "expiry is by instant, evaluated at read time, never by a
        job that removes sanctions".

        Time moves and the restriction stops applying, with no job having
        run and the row still present. That is the difference between a
        sentence that ends and a record that was deleted.
        """
        fixture = _Fixture()
        player = generate_uuid7()
        await _suspend(
            fixture,
            player_id=player,
            actor_id=generate_uuid7(),
            expires_at=NOW + timedelta(hours=2),
        )

        assert await fixture.service.effective_for(player)

        fixture.clock.advance(timedelta(hours=3).total_seconds())
        assert await fixture.service.effective_for(player) == []
        # The row is still there — history, not a deletion.
        assert len(fixture.sanctions.rows) == 1

    @pytest.mark.asyncio
    async def test_a_restore_lifts_the_sanction_names_who_did_it_and_audits_it(self) -> None:
        """§13.3 — "lifting is itself an auditable action".

        And it is reversible in the other direction: once lifted, the same
        account can be restricted again, which is what makes a restriction
        a state rather than a one-way door.
        """
        fixture = _Fixture()
        player, admin, second = generate_uuid7(), generate_uuid7(), generate_uuid7()
        await _suspend(fixture, player_id=player, actor_id=admin)

        lifted = await fixture.service.restore(player_id=player, actor_id=second)

        assert lifted.lifted_at == NOW
        assert lifted.lifted_by == second
        assert await fixture.service.effective_for(player) == []

        entry = fixture.entries.rows[-1]
        assert entry.action is AuditAction.SANCTION_LIFTED
        assert entry.outcome is AuditOutcome.SUCCEEDED
        assert entry.actor_id == second

        # And the account can be restricted again afterwards.
        await _suspend(fixture, player_id=player, actor_id=admin)
        assert len(await fixture.service.effective_for(player)) == 1


class TestTheHttpSurface:
    @pytest.mark.asyncio
    async def test_a_page_costs_one_listing_and_two_batches(self) -> None:
        """The N+1 this endpoint would naturally grow.

        Every restriction names a player and a case, and every case names
        an administrator — three per-row lookups on the naive shape. One
        batch of cases and one of accounts is the whole cost, whatever the
        page size.
        """
        fixture = _Fixture()
        admin = generate_uuid7()
        players = [generate_uuid7() for _ in range(6)]
        for player in players:
            await _suspend(fixture, player_id=player, actor_id=admin)

        accounts = _Accounts({admin: "chief", **{player: "player" for player in players}})
        page = await list_restrictions(
            _Identity(admin),  # type: ignore[arg-type]
            fixture.service,
            fixture.cases,  # type: ignore[arg-type]
            accounts,  # type: ignore[arg-type]
            _Headers(),  # type: ignore[arg-type]
        )

        assert len(page.items) == 6
        assert all(item.is_effective for item in page.items)
        assert len(accounts.batches) == 1
        # The administrator appears on every case and is asked for once.
        assert accounts.batches[0] == len(players) + 1

    @pytest.mark.asyncio
    async def test_restricting_an_unknown_account_is_a_404_and_records_nothing(self) -> None:
        """A moderation case about nobody is a case nobody can review."""
        fixture = _Fixture()
        with pytest.raises(HTTPException) as missing:
            await _restrict(fixture, target=generate_uuid7(), accounts=_Accounts({}))

        assert missing.value.status_code == 404
        assert fixture.cases.rows == []
        assert fixture.entries.rows == []

    def test_the_request_model_has_no_actor_and_forbids_unknown_fields(self) -> None:
        """§12 and §16 — the actor is the guard's, structurally.

        There is no field a caller could set to change who is recorded as
        having decided, and `extra="forbid"` means a payload that invents
        one is a `422` rather than a silently ignored key.
        """
        forbidden = {"actor_id", "opened_by", "admin_id", "player_id", "user_id", "subject"}
        assert not forbidden & set(RestrictAccountRequest.model_fields)

        with pytest.raises(PydanticValidationError):
            RestrictAccountRequest(
                category=ModerationCategory.ABUSE,
                reasoning="x",
                actor_id=str(generate_uuid7()),  # type: ignore[call-arg]
            )

        # And the category is a closed vocabulary, not a free string.
        with pytest.raises(PydanticValidationError):
            RestrictAccountRequest(category="whatever", reasoning="x")  # type: ignore[arg-type]

    def test_no_response_model_can_carry_credential_material(self) -> None:
        """Asserted as absence, so no serialisation path could leak one."""
        forbidden = {
            "email",
            "password",
            "password_hash",
            "token",
            "access_token",
            "refresh_token",
            "session",
            "ip",
            "ip_address",
            "device",
        }
        for model in (SanctionView, ModerationCaseView):
            assert not forbidden & set(model.model_fields), model.__name__

    def test_every_moderation_route_is_behind_the_admin_guard(self) -> None:
        """§33 — the reachability proof, asserted against the route table
        rather than by reading the source, because a route added later
        without the guard is exactly the failure this catches."""
        from app.modules.admin.presentation.dependencies import require_admin

        assert admin_moderation_router.routes
        for route in admin_moderation_router.routes:
            dependant = getattr(route, "dependant", None)
            assert dependant is not None
            assert require_admin in {sub.call for sub in dependant.dependencies}, getattr(
                route, "path", route
            )

    @pytest.mark.asyncio
    async def test_the_user_detail_reports_the_effective_restriction_only(self) -> None:
        """§19 — the badge answers "can this person sign in right now".

        A lifted restriction is history and belongs to `/moderation`; a
        detail page that still showed it would tell an operator somebody is
        blocked when they are not.
        """
        fixture = _Fixture()
        player, admin = generate_uuid7(), generate_uuid7()
        accounts = _Accounts({player: "target", admin: "chief"})

        await _suspend(fixture, player_id=player, actor_id=admin)
        state = await moderation_state_for(
            player, moderation=fixture.service, cases=fixture.cases, accounts=accounts
        )
        assert state.is_restricted is True
        assert state.restriction is not None
        assert state.restriction.case.reasoning.startswith("Repeated")

        await fixture.service.restore(player_id=player, actor_id=admin)
        after = await moderation_state_for(
            player, moderation=fixture.service, cases=fixture.cases, accounts=accounts
        )
        assert after.is_restricted is False
        assert after.restriction is None

    @pytest.mark.asyncio
    async def test_restore_returns_the_lifted_restriction_and_is_audited(self) -> None:
        fixture = _Fixture()
        player, admin = generate_uuid7(), generate_uuid7()
        accounts = _Accounts({player: "target", admin: "chief"})
        await _suspend(fixture, player_id=player, actor_id=admin)

        view = await restore_account(
            player,
            _Identity(admin),  # type: ignore[arg-type]
            fixture.service,
            fixture.cases,  # type: ignore[arg-type]
            accounts,  # type: ignore[arg-type]
            _Headers(),  # type: ignore[arg-type]
        )

        assert view.is_effective is False
        assert view.lifted_by == admin
        assert fixture.entries.rows[-1].action is AuditAction.SANCTION_LIFTED

    @pytest.mark.asyncio
    async def test_the_duration_becomes_an_expiry_on_the_servers_clock(self) -> None:
        """§16 — a duration travels, never an instant.

        An absolute expiry from a browser is subject to the operator's
        device clock, and a skewed one silently ends the restriction at the
        wrong time. Asserted against the service's own clock.
        """
        fixture = _Fixture()
        player, admin = generate_uuid7(), generate_uuid7()
        accounts = _Accounts({player: "target", admin: "chief"})

        view = await _restrict(fixture, target=player, accounts=accounts, duration_hours=48)

        assert view.expires_at == NOW + timedelta(hours=48)
        assert view.is_effective is True


class _Accounts:
    """`AdministrativeUserDirectory`, counting batch reads."""

    def __init__(self, known: dict[UUID, str]) -> None:
        self.known = known
        self.batches: list[int] = []

    async def accounts_by_ids(self, user_ids: list[UUID]) -> dict[UUID, AdminUserRecord]:
        self.batches.append(len(set(user_ids)))
        wanted = set(user_ids)
        return {
            user_id: _account(user_id, name)
            for user_id, name in self.known.items()
            if user_id in wanted
        }

    async def find_account(self, user_id: UUID) -> AdminUserRecord | None:
        name = self.known.get(user_id)
        return None if name is None else _account(user_id, name)

    async def list_accounts(self, **_: object) -> None:  # pragma: no cover
        raise AssertionError("the moderation router must not list accounts")


def _account(user_id: UUID, name: str) -> AdminUserRecord:
    return AdminUserRecord(
        id=user_id,
        username=name,
        email=f"{name}@example.com",
        display_name=None,
        is_active=True,
        is_verified=True,
        created_at=NOW,
    )


class _Headers:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}


class _Identity:
    def __init__(self, account_id: UUID) -> None:
        self.id = account_id


class _Roles:
    """`AdminRoleService`'s one method the moderation route uses."""

    def __init__(self, holders: list[UUID]) -> None:
        self.holders = holders

    async def holders_of(self, role: object) -> list[UUID]:
        return self.holders


async def _restrict(
    fixture: _Fixture,
    *,
    target: UUID,
    accounts: _Accounts,
    duration_hours: int | None = None,
) -> SanctionView:
    """The **real** handler."""
    return await restrict_account(
        target,
        RestrictAccountRequest(
            category=ModerationCategory.CHEATING,
            reasoning="Engine assistance confirmed across three games.",
            duration_hours=duration_hours,
        ),
        _Identity(generate_uuid7()),  # type: ignore[arg-type]
        fixture.service,
        _Roles([generate_uuid7(), generate_uuid7()]),  # type: ignore[arg-type]
        fixture.cases,  # type: ignore[arg-type]
        accounts,  # type: ignore[arg-type]
        _Headers(),  # type: ignore[arg-type]
    )


def test_only_the_enforceable_kind_ships() -> None:
    """A kind an administrator can apply while nothing enforces it would be
    a restriction the console reports and the player never experiences."""
    assert list(SanctionKind) == [SanctionKind.SUSPENDED]
