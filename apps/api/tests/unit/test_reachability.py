"""Every background entry point is wired — A64-018.1.

The check two audits asked for, and the only one that would have caught the
same defect both of them found by hand:

    A64-016.8   `RedisStreamGatewayBus.consume` was written, tested against
                real Redis, and called by nobody. Every cross-node frame on
                a multi-node deployment was published and lost.
    A64-017.6   `MatchRatingService` was written, tested against every case
                its spec named, and registered as no consumer. No rating on
                this platform would ever have moved.

Both passed every unit test, every type check and every import contract.
A unit test proves a component **works**; nothing here proved a component
was **reachable**, and the failure mode is identical each time: the feature
is silently absent while every part of it is healthy.

## What is asserted

Every class that implements a background entry point — an outbox
`EventHandler`, a `platform.tasks.TaskHandler`, or a bus consumer — is named
by a **composition root**: `app_factory.py`, or a module's
`presentation/dependencies`. Naming is the test, because a composition root
is the only place that can turn a class into something the runtime reaches.

## What it deliberately does not assert

That the wiring is *correct* — that a handler is registered for the right
event type, or that its scheduler has a sensible interval. Those are
behaviour and belong to the suites that test them. This asserts the one
thing no behaviour test can: that the class is mentioned at all.

It is a **structural** check by design. Building the object graph and
inspecting it would be stronger and would need a database, a Redis and a
settings object — so it would be a contract test that a developer runs
rarely, when the whole value here is that it runs on every commit.

## Adding an entry point

Wire it, or add it to `_UNREACHABLE_BY_DESIGN` with a reason. There is no
third option, and that is the point.
"""

import ast
import re
from pathlib import Path
from typing import Final

_APP = Path(__file__).resolve().parents[2] / "app"

#: The method signatures that make a class a background entry point.
#:
#: Matched on the *signature* rather than on a base class, because none of
#: these are inherited — every one is a structural `Protocol`
#: (`outbox.ports.EventHandler`, `tasks.ports.TaskHandler`), which is what
#: lets a plain object satisfy them and is exactly why a class can implement
#: one without anything noticing it is not wired.
_ENTRY_POINTS: Final = {
    ("handle", "entries"),
    ("run", "payload"),
    ("consume", "node_id"),
}

#: Where a class becomes something the runtime can reach.
_COMPOSITION_ROOTS: Final = ("app_factory.py", "dependencies")

#: Entry points that are deliberately not wired, each with the reason.
#:
#: A short list is healthy; a growing one is this check being worked around.
_UNREACHABLE_BY_DESIGN: Final = {
    # The protocols themselves — definitions, not implementations.
    "EventHandler": "the Protocol that defines the shape",
    "TaskHandler": "the Protocol that defines the shape",
    "GatewayBus": "the Protocol that defines the shape",
    # A64-016.5 §9. The single-node adapter, selected by configuration in
    # `get_gateway_bus_for` rather than named directly — its production
    # counterpart `RedisStreamGatewayBus` is what the composition root
    # names, and this is what a test harness uses.
    "InProcessGatewayBus": "test and single-node adapter, selected by configuration",
}


def _entry_point_classes() -> dict[str, Path]:
    """Every class in `app/` that implements a background entry point.

    Parsed rather than imported: importing `app` pulls in settings, a
    database engine and a Redis pool, and a reachability check that needed
    infrastructure to run is one that stops running.
    """
    found: dict[str, Path] = {}

    for path in _APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if any(_is_entry_point(item) for item in node.body):
                found[node.name] = path.relative_to(_APP)

    return found


def _is_entry_point(node: ast.stmt) -> bool:
    if not isinstance(node, ast.AsyncFunctionDef):
        return False
    parameters = [argument.arg for argument in node.args.args]
    return any(node.name == name and parameter in parameters for name, parameter in _ENTRY_POINTS)


def _composition_root_text() -> str:
    """Every composition root, concatenated.

    One string rather than a set of names, because a root may name a class
    through a factory function or an alias, and a substring search finds
    both. False *negatives* are what this check exists to prevent; a false
    positive would need somebody to write a class's name in a composition
    root without wiring it, which is a stranger mistake than the one being
    guarded against.
    """
    sources = [
        path.read_text(encoding="utf-8")
        for path in _APP.rglob("*.py")
        if any(marker in str(path) for marker in _COMPOSITION_ROOTS)
    ]
    return "\n".join(sources)


class TestEveryEntryPointIsWired:
    def test_no_background_component_is_unreachable(self) -> None:
        """The assertion both audits asked for.

        A class that handles outbox entries, runs a task or drains a bus is
        useless unless a composition root names it — and both defects this
        check exists for were exactly that: complete, correct, tested code
        the runtime could not reach.

        The failure message names the class and its file, because "one
        component is unwired" sends the reader back to search for it, which
        is the step that was skipped when both defects shipped.
        """
        roots = _composition_root_text()

        unwired = {
            name: path
            for name, path in _entry_point_classes().items()
            if name not in _UNREACHABLE_BY_DESIGN
            and not re.search(rf"\b{re.escape(name)}\b", roots)
        }

        assert unwired == {}, (
            "these background entry points are not named by any composition root, "
            f"so nothing can reach them: {unwired}"
        )

    def test_the_exemption_list_only_covers_classes_that_exist(self) -> None:
        """An exemption for a deleted class is a hole nobody notices.

        The list is how this check is legitimately worked around, so it has
        to shrink when the code does — otherwise a future class that happens
        to reuse a name inherits an exemption somebody granted for a
        different reason.
        """
        classes = _entry_point_classes()

        assert set(_UNREACHABLE_BY_DESIGN) <= set(classes), (
            f"exempted but no longer present: {set(_UNREACHABLE_BY_DESIGN) - set(classes)}"
        )
