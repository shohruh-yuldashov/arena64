"""The dashboards and alerts must name metrics that exist — A64-028.6 §6, §7.

A panel querying a metric nobody emits draws a flat line, and a flat line is
indistinguishable from "nothing is happening" — which is the reading an
operator would most like to trust and least should. An alert rule on a
misspelled metric never fires, and never firing is exactly what a working
alert looks like until the day it was needed.

Neither failure is visible in Grafana, in Prometheus, or in review. Both are
one string comparison away from being impossible, so this is that comparison.

The check runs against the **naming rule** rather than a hand-kept list:
`prometheus_name` is the single function that maps a platform metric name to
a series name, so a metric declared anywhere in `app/` is derivable and one
invented in a dashboard is not.
"""

import json
import re
from pathlib import Path

import pytest
import yaml

from app.platform.metrics.prometheus import NAMESPACE, prometheus_name

_REPO = Path(__file__).resolve().parents[4]
OBSERVABILITY = _REPO / "infrastructure" / "observability"
ALERTS = OBSERVABILITY / "alerts.yml"
DASHBOARDS = sorted((OBSERVABILITY / "dashboards").glob("*.json"))
APP = _REPO / "apps" / "api" / "app"

#: Series prometheus_client derives from a metric the platform declares.
#: A histogram becomes three, and every counter gains a `_created` gauge.
_DERIVED = ("_bucket", "_sum", "_count", "_created")


def _declared() -> set[str]:
    """Every series name the application could emit.

    Read from the source rather than from a list kept beside it, because a
    list beside it is a list that drifts. Any string constant that looks
    like a metric name — `"outbox.exhausted_total"` — is taken as declared,
    which over-approximates slightly and is the safe direction: a test that
    invented false positives would be turned off.
    """
    names: set[str] = set()
    for path in APP.rglob("*.py"):
        for literal in re.findall(r'"([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)"', path.read_text()):
            if literal.endswith((".py", ".json", ".md")):
                continue
            names.add(prometheus_name(literal))
    return names


def _series_in(expression: str) -> set[str]:
    return {name for name in re.findall(rf"\b{NAMESPACE}_[a-z0-9_]+", expression)}


def _base(series: str) -> str:
    for suffix in _DERIVED:
        if series.endswith(suffix):
            return series[: -len(suffix)]
    return series


@pytest.fixture(scope="module")
def declared() -> set[str]:
    return _declared()


class TestAlertRules:
    def test_every_series_is_one_the_platform_emits(self, declared: set[str]) -> None:
        unknown: set[str] = set()
        for group in yaml.safe_load(ALERTS.read_text())["groups"]:
            for rule in group["rules"]:
                unknown |= {s for s in _series_in(rule["expr"]) if _base(s) not in declared}

        assert not unknown, (
            f"alert rules query series nothing emits: {sorted(unknown)}. "
            "A rule on a metric that does not exist never fires, which is "
            "indistinguishable from a rule that is working."
        )

    def test_every_rule_says_why_and_what_to_do(self) -> None:
        """A rule that fires and leaves its reader guessing is a rule its
        reader learns to close."""
        for group in yaml.safe_load(ALERTS.read_text())["groups"]:
            for rule in group["rules"]:
                annotations = rule["annotations"]
                assert {"summary", "why", "action", "runbook"} <= set(annotations), rule["alert"]
                assert annotations["runbook"].startswith("docs/"), rule["alert"]

    def test_severity_is_one_of_two_words(self) -> None:
        """`page` wakes somebody; `ticket` does not. A third level is a level
        nobody agrees on."""
        for group in yaml.safe_load(ALERTS.read_text())["groups"]:
            for rule in group["rules"]:
                assert rule["labels"]["severity"] in {"page", "ticket"}, rule["alert"]


class TestDashboards:
    @pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.name)
    def test_every_panel_queries_a_series_the_platform_emits(
        self, path: Path, declared: set[str]
    ) -> None:
        document = json.loads(path.read_text())
        unknown: set[str] = set()
        for panel in document["panels"]:
            for target in panel["targets"]:
                unknown |= {s for s in _series_in(target["expr"]) if _base(s) not in declared}

        assert not unknown, f"{path.name} draws series nothing emits: {sorted(unknown)}"

    @pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.name)
    def test_the_uid_is_stable_and_the_title_is_not_empty(self, path: Path) -> None:
        """A dashboard's uid is what a link and a runbook reference. A
        generated one changes on every import and breaks both."""
        document = json.loads(path.read_text())
        assert document["uid"].startswith("arena64-")
        assert document["title"]

    def test_the_uids_are_distinct(self) -> None:
        uids = [json.loads(path.read_text())["uid"] for path in DASHBOARDS]
        assert len(uids) == len(set(uids))


class TestTheRunbookLinksResolve:
    def test_every_referenced_anchor_exists(self) -> None:
        """An alert whose runbook link 404s is an alert that arrives at 3am
        with nowhere to go."""
        missing: set[str] = set()
        for group in yaml.safe_load(ALERTS.read_text())["groups"]:
            for rule in group["rules"]:
                target = rule["annotations"]["runbook"]
                path, _, anchor = target.partition("#")
                document = _REPO / path
                if not document.exists():
                    missing.add(target)
                    continue
                headings = {
                    re.sub(r"[^a-z0-9]+", "-", line.lstrip("#").strip().lower()).strip("-")
                    for line in document.read_text().splitlines()
                    if line.startswith("#")
                }
                if anchor and anchor not in headings:
                    missing.add(target)

        assert not missing, f"runbook links that do not resolve: {sorted(missing)}"
