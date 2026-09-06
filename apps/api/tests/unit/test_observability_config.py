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
import shutil
import subprocess
from pathlib import Path
from typing import Any

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

    @pytest.mark.parametrize(
        "series",
        [
            "arena64_certificate_expiry_timestamp_seconds",
            "arena64_backup_last_success_timestamp_seconds",
            "arena64_outbox_exhausted_total",
            "arena64_rate_limit_unavailable_total",
        ],
    )
    def test_the_signals_that_exist_to_be_alerted_on_are_alerted_on(self, series: str) -> None:
        """A metric published for an alert, with no alert, is a metric
        nobody reads.

        Every series here was added **because** something needed to page on
        it — a certificate that stops renewing works perfectly for
        eighty-nine days, a backup that stops is discovered on the day it is
        needed, an abandoned event is already lost, and a fail-open limiter
        is an abuse window nobody is told about. Removing the rule leaves
        the metric, the dashboard and this file's other checks all passing.
        """
        rules = "\n".join(
            rule["expr"]
            for group in yaml.safe_load(ALERTS.read_text())["groups"]
            for rule in group["rules"]
        )

        assert series in rules, f"nothing alerts on {series}"

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


PRODUCTION = _REPO / "infrastructure" / "production"
COMPOSE = PRODUCTION / "compose.yml"


def _compose_service(name: str) -> dict[str, Any]:
    document = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    service: dict[str, Any] = document["services"][name]
    return service


def _docker_available() -> bool:
    return (
        shutil.which("docker") is not None
        and subprocess.run(["docker", "info"], capture_output=True).returncode == 0
    )


needs_docker = pytest.mark.skipif(
    not _docker_available(), reason="needs a Docker daemon to run the real exporter image"
)


class TestTheHostExporterCollectsWhatTheAlertsRead:
    """`HostRebooted` was armed and could never fire — A64-030.4B.1 (B-2).

    Its expression is `(time() - node_boot_time_seconds) < 600`, and that
    series comes from node-exporter's `stat` collector.
    `--collector.disable-defaults` had switched every default off, and the
    allowlist that replaced them did not name `stat`. The rule evaluated
    against an empty vector on every cycle, which is indistinguishable from a
    host that has not rebooted — the failure mode this whole module exists
    to make impossible, arriving through the exporter's flags rather than
    through a misspelling.
    """

    @pytest.fixture(scope="class")
    def flags(self) -> list[str]:
        return [str(flag) for flag in _compose_service("node-exporter")["command"]]

    def test_the_allowlist_is_still_explicit(self, flags: list[str]) -> None:
        """The fix is one collector, not `--collector.disable-defaults`
        deleted: an exporter that publishes everything is a scrape nobody
        sized for, on a host with four cores."""
        assert "--collector.disable-defaults" in flags

    def test_the_collector_the_reboot_alert_needs_is_enabled(self, flags: list[str]) -> None:
        assert "--collector.stat" in flags

    def test_every_alerted_host_series_has_a_collector(self, flags: list[str]) -> None:
        """The general property, so the next alert to name a `node_` series
        cannot be armed against a collector nobody switched on."""
        needed = {
            "node_boot_time_seconds": "--collector.stat",
            "node_cpu_seconds_total": "--collector.cpu",
            "node_filesystem_avail_bytes": "--collector.filesystem",
            "node_filesystem_files": "--collector.filesystem",
            "node_filesystem_files_free": "--collector.filesystem",
            "node_filesystem_size_bytes": "--collector.filesystem",
            "node_memory_MemAvailable_bytes": "--collector.meminfo",
            "node_memory_MemTotal_bytes": "--collector.meminfo",
        }
        alerted = set(re.findall(r"\bnode_[a-zA-Z0-9_]+", ALERTS.read_text(encoding="utf-8")))
        for series in sorted(alerted):
            collector = needed.get(series)
            assert collector is not None, (
                f"{series} is alerted on and this test does not know which collector "
                "publishes it; add it to the table rather than deleting the assertion"
            )
            assert collector in flags, f"{series} is alerted on but {collector} is not enabled"

    def test_the_exporter_still_publishes_no_host_port(self) -> None:
        """A collector was added, not a listener."""
        assert not _compose_service("node-exporter").get("ports")

    @needs_docker
    def test_the_real_exporter_publishes_the_reboot_series(self, flags: list[str]) -> None:
        """Against the pinned image, with production's own flags.

        The flag table above is a reading of the configuration; this is the
        exporter agreeing with it. `$$` is compose's escape for a literal
        `$`, which the daemon never sees.
        """
        import uuid

        image = str(_compose_service("node-exporter")["image"])
        name = f"arena64-nodeexporter-test-{uuid.uuid4().hex[:8]}"
        command = [flag.replace("$$", "$") for flag in flags]
        started = subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--name",
                name,
                "-v",
                "/proc:/host/proc:ro",
                "-v",
                "/sys:/host/sys:ro",
                "-v",
                "/:/rootfs:ro",
                image,
                *command,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert started.returncode == 0, started.stderr
        try:
            # Shares the exporter's network namespace, so nothing is
            # published to the host to read it.
            scraped = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    f"container:{name}",
                    "curlimages/curl:8.11.1",
                    "-sS",
                    "--retry",
                    "5",
                    "--retry-all-errors",
                    "-m",
                    "20",
                    "http://127.0.0.1:9100/metrics",
                ],
                capture_output=True,
                text=True,
                timeout=180,
            )
        finally:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)

        assert scraped.returncode == 0, scraped.stderr
        series = {
            line.split()[0].split("{")[0]
            for line in scraped.stdout.splitlines()
            if line[:1].isalpha()
        }
        assert "node_boot_time_seconds" in series, (
            "the exporter production runs does not publish the series HostRebooted reads"
        )
        # The allowlist is still an allowlist: a default that was off stays off.
        assert "node_systemd_unit_state" not in series


#: Scenarios for the rules this change touched, plus the one it must not have
#: disturbed. Each drives the **shipped expression**, read out of
#: `alerts.yml`, against a synthetic series — so a threshold edited to make a
#: test pass changes the assertion here rather than hiding in prose. Values
#: are absolute because promtool starts its clock at the epoch: at
#: `eval_time: 2h`, `time()` is 7200.
_RULE_SCENARIOS: list[tuple[str, str, str, str, bool]] = [
    # alert, why, series, values, expected to fire
    (
        "CertificateMissing",
        "a readable projection means the gauge exists",
        "arena64_certificate_expiry_timestamp_seconds",
        "5191200+0x180",
        False,
    ),
    (
        "CertificateMissing",
        "no gauge at all is the state B-1 left production in",
        "up",
        "1+0x180",
        True,
    ),
    (
        "CertificateExpiringSoon",
        "sixty days out is not soon",
        "arena64_certificate_expiry_timestamp_seconds",
        "5191200+0x180",
        False,
    ),
    (
        "CertificateExpiringSoon",
        "inside the fourteen-day threshold",
        "arena64_certificate_expiry_timestamp_seconds",
        "1216700+0x180",
        True,
    ),
    (
        "CertificateExpired",
        "still valid",
        "arena64_certificate_expiry_timestamp_seconds",
        "5191200+0x180",
        False,
    ),
    (
        "CertificateExpired",
        "expired ten seconds ago",
        "arena64_certificate_expiry_timestamp_seconds",
        "7190+0x180",
        True,
    ),
    (
        "HostRebooted",
        "booted two hours ago",
        "node_boot_time_seconds",
        "0+0x180",
        False,
    ),
    (
        "HostRebooted",
        "booted five minutes ago — the series B-2 was missing",
        "node_boot_time_seconds",
        "6900+0x180",
        True,
    ),
    (
        "OutboxEventsAbandoned",
        "nothing abandoned",
        "arena64_outbox_exhausted_total",
        "5+0x180",
        False,
    ),
    (
        "OutboxEventsAbandoned",
        "one entry exhausted per minute — B-3's symptom",
        "arena64_outbox_exhausted_total",
        "0+1x180",
        True,
    ),
]


@needs_docker
class TestTheRulesEvaluateAgainstSyntheticData:
    """The alerts this change repaired, evaluated rather than read.

    `test_every_series_is_one_the_platform_emits` proves a rule names a
    series the platform *could* emit. Neither B-1 nor B-2 was a naming
    error: `arena64_certificate_expiry_timestamp_seconds` was spelled
    correctly and never published, and `node_boot_time_seconds` was spelled
    correctly and never collected. The missing half of the contract is
    whether a rule, given the data it names, produces the verdict it claims
    — which is what promtool answers.

    Expressions are read from `alerts.yml` rather than restated, so a
    threshold that moves moves here too instead of quietly agreeing with
    itself. Each is wrapped in `count(...)`, which turns "did this fire" into
    one deterministic sample and keeps the assertion independent of the
    arithmetic behind the verdict and of whatever labels the input carried.
    """

    @pytest.fixture(scope="class")
    def verdicts(self) -> dict[str, bool]:
        document = yaml.safe_load(ALERTS.read_text(encoding="utf-8"))
        expressions = {
            rule["alert"]: str(rule["expr"]).strip()
            for group in document["groups"]
            for rule in group["rules"]
            if "alert" in rule
        }
        cases = [
            {
                "name": f"{alert}: {why}",
                "interval": "1m",
                "input_series": [{"series": series, "values": values}],
                "promql_expr_test": [
                    {
                        "expr": f"count({expressions[alert]})",
                        "eval_time": "2h",
                        "exp_samples": ([{"labels": "{}", "value": 1}] if fires else []),
                    }
                ],
            }
            for alert, why, series, values, fires in _RULE_SCENARIOS
        ]
        result = _run_promtool(cases)
        assert result.returncode == 0, (
            "a shipped alert expression disagreed with its scenario:\n"
            f"{result.stdout}\n{result.stderr}"
        )
        return {f"{alert}: {why}": fires for alert, why, _, _, fires in _RULE_SCENARIOS}

    @pytest.mark.parametrize(
        "scenario", [f"{a}: {w}" for a, w, _, _, _ in _RULE_SCENARIOS], ids=lambda s: s
    )
    def test_the_rule_reaches_the_verdict_the_scenario_expects(
        self, verdicts: dict[str, bool], scenario: str
    ) -> None:
        """One case per row. The fixture is where promtool runs — a single
        invocation for all of them — and this is where a failure names which
        scenario disagreed."""
        assert scenario in verdicts


def _run_promtool(cases: list[dict[str, Any]]) -> subprocess.CompletedProcess[str]:
    """`promtool test rules` inside the pinned Prometheus image.

    The unit file is written in there rather than bind-mounted: the daemon
    resolves a mount source on the host, and this test may itself be running
    inside a container where the repository sits at another path.
    """
    document = {"evaluation_interval": "1m", "tests": cases}
    script = (
        "cat > /tmp/unit.yml <<'ARENA64_UNIT_EOF'\n"
        + yaml.safe_dump(document, sort_keys=False)
        + "\nARENA64_UNIT_EOF\n"
        "promtool test rules /tmp/unit.yml\n"
    )
    image = str(
        yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["prometheus"]["image"]
    )
    return subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "--entrypoint", "sh", image, "-c", script],
        capture_output=True,
        text=True,
        timeout=180,
    )
