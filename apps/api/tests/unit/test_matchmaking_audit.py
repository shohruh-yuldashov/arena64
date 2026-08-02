"""The structural claims A64-015.6's audit rests on.

An audit that says "the documentation is complete" and "the deadline was not
changed on a hunch" is worth exactly as much as the next change that
contradicts it silently. These tests are the tripwires: each one names a
claim from `specs/matchmaking/audit.md` and fails when the codebase stops
supporting it.

    §1   the merged documentation actually describes what shipped
    §2   the acceptance deadline is unchanged, and the evidence path exists
    §10  one factory per service, one recorder per process
    §11  the new modules respect the boundaries the old ones do

Static by design. Every claim here is about *what the repository contains*
rather than about runtime behaviour, and the runtime evidence lives in the
files these sections point at.
"""

import ast
from pathlib import Path

import pytest

from app.config.settings import MatchmakingSettings
from app.modules.game.public.metrics import MATCH_ANSWER_LATENCY

_API = Path(__file__).resolve().parents[2]
_APP = _API / "app"
_REPOSITORY = _API.parents[1]

_MATCHMAKING = _APP / "modules" / "matchmaking"
_SPEC = _REPOSITORY / "specs" / "matchmaking.md"
_DATABASE = _REPOSITORY / "docs" / "01-architecture" / "database.md"
_AUDIT = _REPOSITORY / "specs" / "matchmaking" / "audit.md"

#: The deadline A64-015.4 chose and A64-015.6 §2 refuses to move without
#: production evidence. Restated here rather than imported so that changing
#: the default fails this test rather than silently redefining what it
#: checks.
ACCEPTANCE_DEADLINE_SECONDS = 30


def _modules_under(package: Path) -> list[Path]:
    return sorted(package.rglob("*.py"))


def _imported_modules(source: Path) -> set[str]:
    """Every dotted module name `source` imports — see
    `test_matchmaking_boundaries.py`, whose scanner this mirrors.

    By AST rather than by regex, because an import is a statement and a
    docstring that mentions a module path is not one. These files carry long
    docstrings that name the modules they deliberately do *not* import, so a
    textual scan would report the opposite of the truth.
    """
    tree = ast.parse(source.read_text(), filename=str(source))
    package = ".".join(source.relative_to(_APP.parent).with_suffix("").parts)
    if source.name == "__init__.py":
        package = package.rsplit(".", 1)[0]

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            root = node.module or ""
            if node.level:
                prefix = package.rsplit(".", node.level - 1)[0] if node.level > 1 else package
                root = f"{prefix}.{root}" if root else prefix
            imported.add(root)
            imported.update(f"{root}.{alias.name}" for alias in node.names)
    return imported


class TestTheDocumentationDescribesWhatShipped:
    """§1. The merge audit found A64-015.4's documentation was absent from
    its own commit and arrived one commit later inside A64-015.5's, so the
    *content* is correct today and the process that produced it was not.
    These assertions make the content's absence loud next time."""

    def test_the_spec_exists(self) -> None:
        assert _SPEC.is_file()

    def test_the_audit_document_exists(self) -> None:
        """§14. An audit whose conclusions live only in a pull-request
        comment is an audit nobody can reread."""
        assert _AUDIT.is_file()

    def test_the_audit_states_a_readiness_classification(self) -> None:
        """§14 asks for one of three, stated rather than implied — the whole
        point is that somebody deciding whether to build on this reads a
        verdict instead of inferring one."""
        text = _AUDIT.read_text()

        assert any(
            verdict in text
            for verdict in (
                "READY FOR LIVE GAME INTEGRATION",
                "READY WITH DOCUMENTED LIMITATIONS",
                "NOT READY",
            )
        )

    def test_every_task_in_the_epic_has_a_section_in_the_spec(self) -> None:
        """A64-015.2 through A64-015.6. A task whose behaviour shipped and
        whose section did not is the failure §1 is about."""
        text = _SPEC.read_text()

        for task in ("A64-015.3", "A64-015.4", "A64-015.5", "A64-015.6"):
            assert task in text, f"{task} is undocumented in specs/matchmaking.md"

    def test_every_relation_this_module_owns_is_in_the_database_document(self) -> None:
        """Including the two A64-015.6 added. A relation documented nowhere
        is a relation whose retention, indexes and nullability are only
        discoverable by reading a migration."""
        text = _DATABASE.read_text()

        for relation in (
            "queue_ticket",
            "queue_cooldown",
            "queue_cooldown_audit",
            "pairing_timeline",
        ):
            assert relation in text, f"matchmaking.{relation} is undocumented"

    def test_the_spec_documents_the_audit_trail(self) -> None:
        for concept in ("cooldown_audit", "pairing_timeline"):
            assert concept in _SPEC.read_text()


class TestTheAcceptanceDeadlineIsUnchanged:
    """§2: "do not change the current acceptance deadline based on
    intuition"."""

    def test_it_is_still_thirty_seconds(self) -> None:
        assert MatchmakingSettings().reservation_ttl_seconds == ACCEPTANCE_DEADLINE_SECONDS

    def test_one_number_still_serves_both_the_reservation_and_the_handshake(self) -> None:
        """A64-015.4 §5's "model reservation and acceptance timeout
        coherently instead of creating two unrelated timers". A second
        setting appearing here is the drift that rule exists to prevent."""
        fields = MatchmakingSettings.model_fields

        assert "reservation_ttl_seconds" in fields
        assert not [
            name
            for name in fields
            if "acceptance" in name and name.endswith(("_ttl_seconds", "_deadline_seconds"))
        ]

    def test_the_reservation_cannot_outlive_its_own_ticket(self) -> None:
        """The invariant that makes the deadline safe to tune at all: any
        future value is still bounded by the ticket's lifetime, so a change
        cannot leave the two deadlines racing."""
        with pytest.raises(ValueError, match="MATCHMAKING_RESERVATION_TTL_SECONDS"):
            MatchmakingSettings(reservation_ttl_seconds=300, ticket_ttl_seconds=60)

    def test_the_evidence_a_change_would_need_is_instrumented(self) -> None:
        """§2 forbids tuning on intuition, which is only actionable if the
        data exists. A64-015.5 §7 added the histogram; this asserts it is
        still there, still an observation, and therefore still carries the
        distribution a `p99` is read from."""
        assert MATCH_ANSWER_LATENCY == "game.match_answer_latency_seconds"

    def test_the_latency_measurement_is_never_aggregated_away(self) -> None:
        """`AggregatingMetrics` sums counters and passes observations
        through. If the histogram were ever emitted through `increment`, §2's
        evidence would become a mean and a count — neither of which can
        answer "how long does the slowest tenth of players take"."""
        from app.modules.game.application.services import match_acceptance_service

        source = Path(match_acceptance_service.__file__).read_text()

        assert "observe(" in source
        assert "increment(\n            MATCH_ANSWER_LATENCY" not in source
        assert f"increment({MATCH_ANSWER_LATENCY!r}" not in source


class TestOneFactoryPerService:
    """§10: "the same service is not constructed in two different ways"."""

    ROOT = _MATCHMAKING / "presentation" / "dependencies" / "__init__.py"

    @pytest.mark.parametrize(
        "service",
        [
            "MatchOutcomeService",
            "PairingService",
            "PairingReconciliationService",
            "QueueRetentionService",
            "ReconciliationTimelineProjector",
            "PendingMatchNotifier",
        ],
    )
    def test_the_composition_root_is_the_only_construction_site(self, service: str) -> None:
        """A service constructed in two places drifts on the first
        collaborator either site gains — which is precisely what A64-015.6
        added to three of these."""
        defining_module = f"{_camel_to_snake(service)}.py"
        constructors = sorted(
            str(module.relative_to(_APP))
            for module in _modules_under(_APP)
            if f"{service}(" in module.read_text() and module.name != defining_module
        )

        assert constructors == ["modules/matchmaking/presentation/dependencies/__init__.py"]

    @pytest.mark.parametrize(
        "factory",
        [
            "build_cooldown_audit",
            "build_reconciliation_timeline",
            "build_timeline_projector",
            "build_match_outcome_service",
            "build_queue_retention_service",
            "build_pairing_service",
        ],
    )
    def test_each_factory_is_defined_once(self, factory: str) -> None:
        assert self.ROOT.read_text().count(f"def {factory}(") == 1

    def test_the_worker_paths_build_their_graphs_from_the_same_factories(self) -> None:
        """`app_factory` has no request and no `Depends`, so a factory
        reachable only through `Depends` would mean the background path
        assembling its own copy."""
        source = (_APP / "app_factory.py").read_text()

        for factory in (
            "build_match_outcome_service",
            "build_queue_retention_service",
            "build_pairing_service",
            "build_timeline_projector",
        ):
            assert f"{factory}(" in source

    def test_one_metrics_recorder_serves_the_whole_process(self) -> None:
        """The defect §10 found: two accessors built two recorders, and once
        A64-015.6 made the recorder stateful that stopped being redundancy
        and started being counters nothing drained."""
        constructors = sorted(
            str(module.relative_to(_APP))
            for module in _modules_under(_APP)
            if "AggregatingMetrics(" in module.read_text()
        )

        assert constructors == ["platform/metrics/runtime.py"]


class TestTheNewModulesRespectTheOldBoundaries:
    """§11. Three relations, two consumers and a metrics accumulator were
    added; none of them may be the first thing to reach across an edge."""

    AUDIT_MODULES = (
        _MATCHMAKING / "domain" / "cooldown_audit.py",
        _MATCHMAKING / "domain" / "reconciliation_timeline.py",
        _MATCHMAKING / "application" / "services" / "reconciliation_timeline_service.py",
        _MATCHMAKING / "infrastructure" / "repositories" / "audit_repositories.py",
    )

    def test_none_of_them_imports_a_game_internal(self) -> None:
        """§11 is explicit: "do not import Game internals"."""
        for module in self.AUDIT_MODULES:
            cross_module = {
                name for name in _imported_modules(module) if name.startswith("app.modules.game")
            }
            assert all(name.startswith("app.modules.game.public") for name in cross_module), (
                f"{module.name} reaches into game"
            )

    def test_the_new_domain_modules_import_no_framework(self) -> None:
        """Business rules must not know about HTTP, an ORM or a session. If
        the framework changed tomorrow, an audit record should not."""
        forbidden = ("fastapi", "sqlalchemy", "pydantic", "redis")

        for module in self.AUDIT_MODULES[:2]:
            for name in _imported_modules(module):
                assert not name.startswith(forbidden), f"{module.name} imports {name}"

    def test_the_projector_holds_ports_rather_than_adapters(self) -> None:
        """An application service that named `SqlAlchemyReconciliation...`
        would be a service that cannot be tested without a database, and the
        composition root would have nothing left to compose."""
        imported = _imported_modules(self.AUDIT_MODULES[2])

        assert not [name for name in imported if "infrastructure" in name]

    def test_the_platform_metrics_package_imports_no_module(self) -> None:
        """`app/platform` is below every module, and the accumulator being
        shared by all of them makes that edge load-bearing rather than
        tidy."""
        for module in _modules_under(_APP / "platform" / "metrics"):
            assert not [
                name for name in _imported_modules(module) if name.startswith("app.modules")
            ]

    def test_the_audit_trail_is_not_reachable_from_a_route(self) -> None:
        """§3 and §4: the trail is for operations and support. A router that
        imported either repository or either record would be the public
        surface both documents say this is not."""
        for module in _modules_under(_MATCHMAKING / "presentation" / "routers"):
            source = module.read_text()
            assert "cooldown_audit" not in source
            assert "reconciliation_timeline" not in source
            assert "CooldownRecord" not in source
            assert "ReconciliationEntry" not in source


def _camel_to_snake(name: str) -> str:
    """`MatchOutcomeService` -> `match_outcome_service`, so a construction
    check can skip the module that defines the class."""
    return "".join(f"_{char.lower()}" if char.isupper() else char for char in name).lstrip("_")
