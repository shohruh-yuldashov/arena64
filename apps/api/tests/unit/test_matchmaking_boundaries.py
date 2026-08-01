"""Where `matchmaking` is allowed to reach — A64-015.2.

`lint-imports` already enforces these edges and
`test_import_contracts.py` runs it, so why a second file: import-linter
reads `.importlinter`, and a contract deleted from that file takes its own
enforcement with it. These tests state the two rules in code, so removing a
contract fails a test that names the rule rather than silently reducing the
number of contracts checked.

They are also faster to read when they fail. `lint-imports` reports "chain
found", which is the right diagnostic for a graph and the wrong one for a
reviewer asking whether `matchmaking` may import `Match`.

The scan is by AST rather than by regex over text: an import is a statement,
and a docstring that mentions a module path is not one.
"""

import ast
from pathlib import Path

_APP = Path(__file__).resolve().parents[2] / "app"

_MATCHMAKING = _APP / "modules" / "matchmaking"


def _imported_modules(source: Path) -> set[str]:
    """Every dotted module name `source` imports, absolute and relative alike.

    A relative import is resolved against the file's own package, because
    `from ..domain import X` inside `matchmaking.application` is the same
    edge as the absolute form and must not escape the check by spelling.
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


def _modules_under(package: Path) -> list[Path]:
    return sorted(package.rglob("*.py"))


class TestMatchmakingReachesGameThroughItsPublicSurface:
    def test_no_module_imports_a_game_internal(self) -> None:
        """R-1: a module's `public/` package is the whole of what other
        modules may depend on. `Match`, `MoveRecord` and the draw-rule set
        are `game`'s to change without consulting anybody."""
        offenders = {
            str(module.relative_to(_APP)): sorted(
                name
                for name in _imported_modules(module)
                if name.startswith("app.modules.game")
                and not name.startswith("app.modules.game.public")
            )
            for module in _modules_under(_MATCHMAKING)
        }
        offenders = {path: names for path, names in offenders.items() if names}

        assert offenders == {}

    def test_the_public_surface_is_actually_used(self) -> None:
        """Otherwise the rule above is vacuous — a boundary nobody crosses
        is not a boundary that has been tested."""
        importers = [
            module
            for module in _modules_under(_MATCHMAKING)
            if any(name.startswith("app.modules.game.public") for name in _imported_modules(module))
        ]

        assert importers != []

    def test_the_pairing_service_reaches_game_only_through_the_command(self) -> None:
        """A64-015.3 §9 and §15.12. The scan's whole dependency on `game` is
        the published command port — no `Match`, no aggregate, no
        repository."""
        service = _MATCHMAKING / "application" / "services" / "pairing_service.py"
        from_game = sorted(
            name for name in _imported_modules(service) if name.startswith("app.modules.game")
        )

        assert all(name.startswith("app.modules.game.public") for name in from_game)
        assert "app.modules.game.public.CreateMatchRequest" in from_game

    def test_no_module_imports_the_engine(self) -> None:
        """R-2 names three permitted consumers — `game`, `replay`,
        `fairplay` — and `matchmaking` is not one of them. It reaches the
        rules through `game.public`, which is what makes the engine's
        version stamping and variant catalogue one authority rather than
        two."""
        offenders = {
            str(module.relative_to(_APP)): sorted(
                name for name in _imported_modules(module) if name.startswith("app.modules.engine")
            )
            for module in _modules_under(_MATCHMAKING)
        }
        offenders = {path: names for path, names in offenders.items() if names}

        assert offenders == {}


class TestMatchmakingReachesFriendsThroughItsPublicSurface:
    """A64-015.3 §5. BL-2's pairwise block filter is `friends`' rule, read
    through `friends.public.PairingExclusions` and nothing else.

    `friends-internals-are-private` in `.importlinter` already listed
    `matchmaking` as a source before this task had an edge to it, so the
    fence was built before the first import arrived. This states the rule in
    code as well — see this module's docstring on why both.
    """

    def test_no_module_imports_a_friends_internal(self) -> None:
        offenders = {
            str(module.relative_to(_APP)): sorted(
                name
                for name in _imported_modules(module)
                if name.startswith("app.modules.friends")
                and not name.startswith("app.modules.friends.public")
            )
            for module in _modules_under(_MATCHMAKING)
            if "dependencies" not in module.parts
        }
        offenders = {path: names for path, names in offenders.items() if names}

        assert offenders == {}

    def test_the_pairing_service_holds_only_the_exclusion_port(self) -> None:
        """Not `SocialGraphReader`, which could answer who is friends with
        whom, and not `PresenceAudience`. The port a module does not hold is
        what guarantees the capability it does not have."""
        service = _MATCHMAKING / "application" / "services" / "pairing_service.py"
        from_friends = sorted(
            name for name in _imported_modules(service) if name.startswith("app.modules.friends")
        )

        assert from_friends == [
            "app.modules.friends.public",
            "app.modules.friends.public.PairingExclusions",
        ]

    def test_the_composition_root_is_where_the_adapter_is_named(self) -> None:
        """`SqlAlchemyBlockedPlayerRepository` and `PairingExclusionService`
        are concrete `friends` classes, and naming them is exactly what a
        composition root is for — which is why it sits outside every privacy
        contract's source list."""
        root = _MATCHMAKING / "presentation" / "dependencies" / "__init__.py"
        imported = _imported_modules(root)

        assert "app.modules.friends.infrastructure.repositories" in imported


class TestRouteHandlersGoThroughApplicationServices:
    """DI-01 and the layering rule: transport calls the application layer,
    never a repository or a session.

    A route that opened its own session would put transaction boundaries in
    two places, and the one in the handler would not be the one the outbox
    row is written in — which is the failure AD-16 exists to prevent.
    """

    PRESENTATION = _MATCHMAKING / "presentation"

    def test_the_router_imports_no_infrastructure(self) -> None:
        imported = _imported_modules(self.PRESENTATION / "router.py")

        assert not any("infrastructure" in name for name in imported)

    def test_the_router_imports_no_database_session(self) -> None:
        imported = _imported_modules(self.PRESENTATION / "router.py")

        assert not any(name.startswith("sqlalchemy") for name in imported)
        assert not any("database" in name for name in imported)

    def test_the_router_reaches_the_service_through_a_dependency(self) -> None:
        """Constructed by the composition root in
        `presentation/dependencies/`, not by the handler. The handler names
        the type it needs and receives it."""
        imported = _imported_modules(self.PRESENTATION / "router.py")

        assert "app.modules.matchmaking.presentation.dependencies.QueueServiceDep" in imported

    def test_only_the_composition_root_builds_repositories(self) -> None:
        """Every other file in `presentation/` — the router, the schemas,
        the rate limits — is transport translation. If a second file starts
        importing a repository, the composition root has stopped being one.
        """
        builders = [
            str(module.relative_to(_APP))
            for module in _modules_under(self.PRESENTATION)
            if any("infrastructure" in name for name in _imported_modules(module))
        ]

        assert builders == ["modules/matchmaking/presentation/dependencies/__init__.py"]


class TestTheDomainStaysFrameworkFree:
    """Architecture rule 2. Asserted here rather than left to
    `.importlinter` because `queue_pool.py` is new in A64-015.2 and its
    `require_offered` call is the first time a `matchmaking` domain type
    depended on another module at all."""

    def test_no_domain_module_imports_a_framework(self) -> None:
        forbidden = ("fastapi", "sqlalchemy", "pydantic", "redis", "starlette")
        offenders = {
            str(module.relative_to(_APP)): sorted(
                name for name in _imported_modules(module) if name.startswith(forbidden)
            )
            for module in _modules_under(_MATCHMAKING / "domain")
        }
        offenders = {path: names for path, names in offenders.items() if names}

        assert offenders == {}

    def test_the_domain_s_only_cross_module_dependency_is_game_public(self) -> None:
        cross_module = {
            name
            for module in _modules_under(_MATCHMAKING / "domain")
            for name in _imported_modules(module)
            if name.startswith("app.modules.") and not name.startswith("app.modules.matchmaking")
        }

        assert all(name.startswith("app.modules.game.public") for name in cross_module)
