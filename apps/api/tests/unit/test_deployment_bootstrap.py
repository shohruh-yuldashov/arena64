"""A first boot must reach a serving edge — A64-029.

Two defects in `infrastructure/production/` made the very first `docker
compose up` on a clean host impossible, and neither was reachable by any
test that existed: one is an exit code in a shell script, the other is a
base image, and the only thing that reads either is Docker.

**The certificate deadlock.** `nginx` waits on `certbot-init` with
`condition: service_completed_successfully`. `certbot-init` requests a
certificate over HTTP-01, and the challenge is served *by nginx*. On a first
boot nginx has not started, so the challenge cannot be answered, so issuance
fails, so `certbot-init` exits non-zero, so nginx never starts. Permanently.
`issue.sh` writes a self-signed stopgap precisely to break that deadlock and
its own exit code put it straight back. Measured on a clean host: eleven of
fifteen services stayed in `created` and nothing listened on 80 or 443.

**The shell that was not there.** `web` and `admin` carry `dist/` into a
shared volume, and `compose.yml` performs that copy with
`entrypoint: ["/bin/sh", "-c", "cp -r /dist/. /srv/..."]`. Both images ended
`FROM scratch`, which has neither `/bin/sh` nor `cp`, so neither container
could be created — `exec: "/bin/sh": stat /bin/sh: no such file or
directory` — and nginx, which mounts those volumes, never started either.

These are static and behavioural checks over the deployment definition. They
do not prove a deployment works; the task's report carries that. What they
prove is that these two cannot silently come back.

**A64-030.2 added the second half of this file.** Its pre-deployment audit
of the same definition found four more things that no test could see and
that all four failed *silently*: a backup loop calling a flag the CLI does
not define and discarding the error, a media route that dropped the bucket
out of every object path, an unbounded Redis and unbounded containers on a
7.75 GiB host, and a scheduler flag written into the compose file and read
by nothing. Each has a class below.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.config.settings import (
    AnalyticsSettings,
    GameSettings,
    GatewaySettings,
    MatchmakingSettings,
    OutboxSettings,
    PostgresSettings,
    PresenceSettings,
    SectionSettings,
)
from app.gateway.node import resolve_node_id

_REPO = Path(__file__).resolve().parents[4]
PRODUCTION = _REPO / "infrastructure" / "production"
COMPOSE = PRODUCTION / "compose.yml"
ISSUE_SH = PRODUCTION / "certbot" / "issue.sh"
RENEW_SH = PRODUCTION / "certbot" / "renew.sh"

#: The compose services whose entrypoint is a shell command, mapped to the
#: Dockerfile that builds them. Listed rather than derived: a new carrier
#: should have to appear here deliberately.
CARRIERS = {
    "web": _REPO / "apps" / "web" / "Dockerfile",
    "admin": _REPO / "apps" / "admin" / "Dockerfile",
}


def _compose() -> dict[str, Any]:
    # `${VAR}` interpolation is irrelevant to every assertion here, and
    # `yaml.safe_load` does not perform it, so the raw document is read.
    document: dict[str, Any] = yaml.safe_load(COMPOSE.read_text())
    return document


def _final_stage(dockerfile: Path) -> str:
    """The base image of the last `FROM` — the stage that is actually run."""
    stages: list[str] = re.findall(r"^FROM\s+(\S+)", dockerfile.read_text(), re.MULTILINE)
    assert stages, f"{dockerfile} declares no FROM"
    return stages[-1]


class TestTheCarrierImagesCanRunTheirEntrypoint:
    @pytest.mark.parametrize("service", sorted(CARRIERS))
    def test_the_entrypoint_is_a_shell_command(self, service: str) -> None:
        """Guards the assumption the next test depends on."""
        entrypoint = _compose()["services"][service]["entrypoint"]
        assert entrypoint[0] == "/bin/sh", (
            f"{service} no longer starts with /bin/sh: {entrypoint}. If the copy is "
            "done another way, this file's second assertion is checking nothing."
        )

    @pytest.mark.parametrize("service", sorted(CARRIERS))
    def test_the_image_supplies_that_shell(self, service: str) -> None:
        base = _final_stage(CARRIERS[service])
        assert base != "scratch", (
            f"{CARRIERS[service].relative_to(_REPO)} ends FROM scratch, but compose "
            f"starts {service} with /bin/sh. A scratch image has no shell and no cp: "
            "the container cannot be created, the volume stays empty, and nginx — "
            "which mounts it — never starts."
        )

    @pytest.mark.parametrize("service", sorted(CARRIERS))
    def test_the_base_image_is_pinned_to_an_exact_version(self, service: str) -> None:
        base = _final_stage(CARRIERS[service])
        assert re.search(r":\d+\.\d+\.\d+", base), (
            f"{base} is not pinned to an exact version — CLAUDE.md §2.6."
        )


# `TestIssuanceFailureDoesNotBlockTheEdge` used to live here — A64-029's
# proof that `issue.sh` exits zero on a failed issuance so that nginx, which
# waits on it with `service_completed_successfully`, is released.
#
# A64-030.2 moved it to `tests/unit/test_acme_bootstrap.py`, where the same
# property is asserted alongside the rest of the bootstrap's contract:
# `test_a_failed_issuance_exits_zero_so_nginx_is_released` keeps the exit
# code, and `test_nginx_has_something_to_start_on` keeps the certificate the
# edge needs. Both now drive the script with its `lineage.sh` dependency, and
# against the layout the bootstrap actually writes.
#
# Kept as a pointer rather than duplicated: two copies of a rule is how the
# copies drift.


class TestTheLoopFinishesWhatInitStarted:
    def test_renew_retries_issuance_while_the_stopgap_is_in_place(self) -> None:
        body = RENEW_SH.read_text()
        assert ".self-signed" in body, (
            "renew.sh does not look at the stopgap marker. Nothing then completes the "
            "issuance that certbot-init could not, and the site serves an untrusted "
            "certificate until an operator intervenes."
        )
        assert "issue.sh" in body, "renew.sh never invokes the issuance script"

    def test_the_renewal_container_can_reach_that_script(self) -> None:
        certbot = _compose()["services"]["certbot"]
        mounts = [str(volume) for volume in certbot["volumes"]]
        assert any("issue.sh" in mount for mount in mounts), (
            "renew.sh calls issue.sh, but the certbot service does not mount it"
        )
        assert "ARENA64_DOMAIN" in certbot.get("environment", {}), (
            "issue.sh requires ARENA64_DOMAIN and refuses to run without it"
        )


# ---------------------------------------------------------------------------
# A64-030.2 — the pre-deployment audit's compose findings.
#
# Everything below is a static assertion over the same production definition,
# added because the audit found four things in it that no test could see and
# that all four failed **silently**: a backup that never ran, a media route
# that never resolved, an unbounded Redis, and a scheduler flag honoured by
# nothing.
# ---------------------------------------------------------------------------

#: The services that are expected to be running when the tier is idle, and
#: therefore the ones whose memory and CPU have to be bounded. Listed rather
#: than derived from `restart: unless-stopped`, so that a new resident
#: service has to be given an envelope deliberately.
RESIDENT = (
    "nginx",
    "api-1",
    "api-2",
    "worker",
    "postgres",
    "redis",
    "minio",
    "prometheus",
    "node-exporter",
    "certbot",
    "backup",
)

#: The two API replicas, which must differ in nothing that matters.
REPLICAS = ("api-1", "api-2")

#: Every scheduler flag the compose file writes out on both roles, mapped to
#: the settings section and field that has to receive it.
#:
#: The mapping is the point. A flag whose name matches no section prefix is
#: read by nobody and fails silently, which is exactly what
#: `ANALYTICS_RETENTION_ENABLED` did until A64-030.2 (F-3): the compose file
#: set it to `"false"` on both replicas, `AnalyticsSettings` was a plain
#: `BaseModel` with no environment source, and the retention sweep ran on
#: all three processes.
SCHEDULER_FLAGS: dict[str, tuple[type[SectionSettings], str]] = {
    "OUTBOX_WORKER_ENABLED": (OutboxSettings, "worker_enabled"),
    "OUTBOX_RETENTION_ENABLED": (OutboxSettings, "retention_enabled"),
    "MATCHMAKING_PAIRING_ENABLED": (MatchmakingSettings, "pairing_enabled"),
    "MATCHMAKING_EXPIRY_ENABLED": (MatchmakingSettings, "expiry_enabled"),
    "MATCHMAKING_RECONCILIATION_ENABLED": (MatchmakingSettings, "reconciliation_enabled"),
    "MATCHMAKING_RETENTION_ENABLED": (MatchmakingSettings, "retention_enabled"),
    "MATCHMAKING_CHALLENGE_EXPIRY_ENABLED": (MatchmakingSettings, "challenge_expiry_enabled"),
    "ANALYTICS_RETENTION_ENABLED": (AnalyticsSettings, "retention_enabled"),
    "PRESENCE_SWEEPER_ENABLED": (PresenceSettings, "sweeper_enabled"),
    "GAME_CLOCK_ENABLED": (GameSettings, "clock_enabled"),
    "GATEWAY_FORWARDING_ENABLED": (GatewaySettings, "forwarding_enabled"),
}


def _service(name: str) -> dict[str, Any]:
    services: dict[str, dict[str, Any]] = _compose()["services"]
    assert name in services, f"compose.yml declares no {name!r} service"
    return services[name]


def _environment(name: str) -> dict[str, str]:
    """One service's environment, with the merge keys already resolved.

    `yaml.safe_load` expands `<<:` for us, so this is the same mapping
    Compose would build — before `${VAR}` interpolation, which none of these
    assertions needs.
    """
    return {key: str(value) for key, value in _service(name).get("environment", {}).items()}


def _shell_body(name: str) -> str:
    """The script a service's `sh -c` entrypoint runs."""
    entrypoint = _service(name)["entrypoint"]
    assert entrypoint[:2] == ["/bin/sh", "-c"], (
        f"{name}'s entrypoint is no longer a shell script: {entrypoint}"
    )
    return str(entrypoint[2])


class TestTheBackupLoopCanActuallyTakeABackup:
    """A64-030.2, B-1 — the finding with the largest blast radius.

    The loop invoked `create --destination`, and the CLI defines `--into`.
    `--destination` is the name of an internal Python parameter and has never
    been an argument, so every run died in argparse with exit 2, `|| true`
    discarded it, and the container slept for a day and did it again. On a
    container reporting `Up`, for ever.

    Nothing was written, so `arena64_backup_last_success_timestamp_seconds`
    was never published, so the alerts that fire on its absence had nothing
    to fire on — in a Prometheus the same audit found was not deployed.

    The first test is the one that matters: rather than grepping for a flag
    name, it feeds the compose invocation to the **real parser**. A rename on
    either side fails it.
    """

    def _invocation(self) -> list[str]:
        """The `app.operator.backup` arguments the loop actually passes."""
        body = _shell_body("backup")
        found = re.search(r"python -m app\.operator\.backup (?P<args>[^\n|&;]+)", body)
        assert found is not None, f"the backup loop no longer invokes the CLI:\n{body}"
        return found.group("args").split()

    def test_the_compose_invocation_is_accepted_by_the_real_parser(self) -> None:
        from app.operator.backup import _parser

        try:
            parsed = _parser().parse_args(self._invocation())
        except SystemExit as exit_code:  # argparse exits 2 on a bad argument
            raise AssertionError(
                "infrastructure/production/compose.yml invokes `app.operator.backup` with "
                f"arguments its own parser refuses: {self._invocation()}. Every run of the "
                "resident backup loop would exit non-zero and no backup would ever exist."
            ) from exit_code

        assert parsed.command == "create"
        assert str(parsed.into) == "/var/backups/arena64"

    def test_a_failure_is_reported_rather_than_discarded(self) -> None:
        """`|| true` is silence, not survival.

        Surviving a failed run is right — this is a resident service and the
        next interval is a real chance — but the status has to reach the log,
        or a permanently broken backup is indistinguishable from a working
        one.
        """
        body = _shell_body("backup")

        assert "|| true" not in body, (
            "the backup loop discards its exit status again. A permanent failure would "
            "then be invisible: the container reports Up, nothing is written, and the "
            "only signal left is the age of a metric that was never published."
        )
        assert "FAILED" in body, "a failed run writes nothing an operator would notice"
        assert ">&2" in body, "the failure is reported on stdout, where it reads as progress"

    def test_the_loop_does_not_exit_on_a_failure(self) -> None:
        """The other half: a container that exited here would restart-loop
        against a database that is merely busy."""
        body = _shell_body("backup")
        assert "while true" in body, "the backup loop no longer loops"
        assert "set -e" not in body, (
            "`set -e` would end the loop on the first failed run, turning a transient "
            "pg_dump error into a service that never runs again"
        )

    def test_its_healthcheck_is_disabled_rather_than_permanently_red(self) -> None:
        """The image's healthcheck curls `/api/v1/health`; this container is a
        backup loop and serves nothing. Left in place it reports `unhealthy`
        for ever on a container that is working, which is how an operator
        learns to ignore the column."""
        assert _service("backup")["healthcheck"] == {"disable": True}


class TestTheEdgeIsTheOnlyPublishedService:
    def test_nothing_but_nginx_publishes_a_host_port(self) -> None:
        """`deployment.md` §8.8: PostgreSQL, Redis, MinIO, Prometheus and the
        API's 8000 must be unreachable from outside the host. The compose
        file is the enforcement and the firewall is the second boundary."""
        published = {
            name: service["ports"]
            for name, service in _compose()["services"].items()
            if service.get("ports")
        }
        assert set(published) == {"nginx"}, f"services publishing host ports: {sorted(published)}"

    def test_the_edge_publishes_udp_443_for_http3(self) -> None:
        """QUIC is a different protocol on the same port number, so it needs
        its own mapping. Without it the `Alt-Svc` advertisement points at a
        port nothing listens on and every client silently stays on HTTP/2."""
        assert "443:443/udp" in _service("nginx")["ports"]


class TestEveryResidentServiceIsBounded:
    """A64-030.2, B-4 — `deployment.md` §9.2's "limits: **unset**".

    Unset limits on a 7.75 GiB host mean the first thing to grow takes the
    machine, and the kernel's OOM killer chooses the victim.
    """

    @pytest.mark.parametrize("service", RESIDENT)
    def test_it_declares_a_memory_ceiling_and_a_reservation(self, service: str) -> None:
        resources = _service(service).get("deploy", {}).get("resources", {})
        assert "memory" in resources.get("limits", {}), (
            f"{service} has no memory ceiling: it can take the whole host"
        )
        assert "memory" in resources.get("reservations", {}), (
            f"{service} declares no memory reservation, so nothing records what it is "
            "expected to occupy"
        )

    @pytest.mark.parametrize("service", RESIDENT)
    def test_it_declares_a_cpu_weight(self, service: str) -> None:
        """**Not tidiness.** An unset `cpu_shares` is the Docker default of
        1024, so a service left out of the scheme would outweigh PostgreSQL
        at 768 under exactly the contention the scheme exists to arbitrate.

        `cpu_shares` rather than `deploy.resources.reservations.cpus` because
        the latter is a Swarm scheduling hint that the local runtime silently
        ignores — verified on the production host against Compose v5.5.1.
        """
        assert "cpu_shares" in _service(service), f"{service} has no CPU weight"

    def test_no_service_asks_for_a_cpu_reservation_compose_would_ignore(self) -> None:
        ignored = [
            name
            for name, service in _compose()["services"].items()
            if "cpus" in service.get("deploy", {}).get("resources", {}).get("reservations", {})
        ]
        assert not ignored, (
            f"{ignored} declare deploy.resources.reservations.cpus, which the non-Swarm "
            "runtime accepts and does not apply. A setting that looks applied and is not "
            "is worse than an absent one — use cpu_shares."
        )

    def test_the_one_shot_schema_step_still_has_a_ceiling(self) -> None:
        """`migrate` is transient, but it runs *during a deploy*, alongside
        two replicas still serving on their own reservations."""
        limits = _service("migrate")["deploy"]["resources"]["limits"]
        assert "memory" in limits


class TestPostgresIsSizedForThisHostAndStillDurable:
    def _flags(self) -> dict[str, str]:
        command = [str(item) for item in _service("postgres")["command"]]
        return dict(
            item.split("=", 1) for item in command if "=" in item and not item.startswith("-")
        )

    @pytest.mark.parametrize(
        ("flag", "value"),
        [
            ("shared_buffers", "256MB"),
            ("effective_cache_size", "2GB"),
            ("work_mem", "4MB"),
            ("maintenance_work_mem", "96MB"),
            ("max_connections", "100"),
            ("random_page_cost", "1.1"),
            ("effective_io_concurrency", "200"),
        ],
    )
    def test_the_tuning_is_present(self, flag: str, value: str) -> None:
        assert self._flags().get(flag) == value

    def test_shared_buffers_is_sized_against_the_container_not_the_host(self) -> None:
        """A quarter of RAM is the usual rule and would be 2 GiB here, inside
        a container capped well below that: PostgreSQL would be killed before
        it filled its own cache."""
        ceiling = str(_service("postgres")["deploy"]["resources"]["limits"]["memory"])
        megabytes = int(re.sub(r"[^0-9]", "", ceiling))
        shared = int(re.sub(r"[^0-9]", "", self._flags()["shared_buffers"]))
        assert shared <= megabytes // 4, (
            f"shared_buffers {shared}MB is more than a quarter of the {megabytes}M ceiling"
        )

    @pytest.mark.parametrize("setting", ["fsync", "synchronous_commit", "full_page_writes"])
    def test_durability_is_not_weakened(self, setting: str) -> None:
        """These keep their defaults and are deliberately unlisted, so no
        later edit can turn one off by looking like the others."""
        command = " ".join(str(item) for item in _service("postgres")["command"])
        assert setting not in command, (
            f"{setting} is being set in the production command. Nothing about sizing this "
            "host is a reason to weaken WAL durability — CLAUDE.md §1 rule 1."
        )


class TestRedisIsBoundedAndAuthenticated:
    def _command(self) -> str:
        return " ".join(str(item) for item in _service("redis")["command"])

    def test_it_has_a_memory_bound_at_all(self) -> None:
        """It was unset, which is unbounded, on a host with 7.75 GiB."""
        assert "--maxmemory" in self._command()

    def test_the_eviction_policy_is_the_loud_one(self) -> None:
        """One instance has one instance-wide policy, so the choice is between
        a write that fails and says so and a rate-limit counter that
        disappears during the spike the limiter exists for.
        `data-reliability.md` §3 names the second as unacceptable."""
        assert "--maxmemory-policy noeviction" in self._command(), (
            "an LRU policy here evicts `rl:` counters under memory pressure — a limit "
            "that silently stops applying, which is the one outcome §3 refuses"
        )

    def test_it_requires_a_password(self) -> None:
        assert "--requirepass" in self._command()

    def test_the_healthcheck_authenticates_without_putting_the_password_in_ps(self) -> None:
        """`redis-cli -a` warns on stderr every invocation and puts the secret
        in the container's own process list twice a second. `REDISCLI_AUTH`
        is the documented alternative."""
        redis = _service("redis")
        assert "REDISCLI_AUTH" in redis["environment"]
        assert "-a" not in redis["healthcheck"]["test"]

    def test_no_password_is_written_literally(self) -> None:
        """Both places it appears come from `production.env`, so there is one
        source and nothing to keep in step."""
        redis = _service("redis")
        command = [str(item) for item in redis["command"]]

        assert command[command.index("--requirepass") + 1] == "${REDIS_PASSWORD}"
        assert redis["environment"]["REDISCLI_AUTH"] == "${REDIS_PASSWORD}"

    @pytest.mark.parametrize("role", ["LIVE", "BUS", "BROKER", "CACHE", "LIMITS"])
    def test_every_role_url_carries_the_credential(self, role: str) -> None:
        """All five, or the ones that were missed fail at startup in a tier
        that has already been declared healthy."""
        url = _environment("worker")[f"REDIS_{role}_URL"]
        assert url.startswith("redis://:${REDIS_PASSWORD}@"), url


class TestTheConnectionBudgetIsStated:
    """A64-030.2, B-4. One single-threaded uvicorn process with a 5 s
    statement timeout cannot usefully hold fifteen backends, and each idle
    backend costs PostgreSQL 5–10 MB of a now-capped container."""

    @pytest.mark.parametrize("service", [*REPLICAS, "worker"])
    def test_each_long_running_process_is_given_a_pool_size(self, service: str) -> None:
        environment = _environment(service)
        assert environment["POSTGRES_POOL_SIZE"] == "5"
        assert environment["POSTGRES_MAX_OVERFLOW"] == "5"

    def test_the_settings_fields_these_variables_name_exist(self) -> None:
        """Guards the assumption the test above depends on: a variable whose
        field does not exist is read by nothing."""
        assert "pool_size" in PostgresSettings.model_fields
        assert "max_overflow" in PostgresSettings.model_fields

    def test_the_ceiling_fits_inside_max_connections(self) -> None:
        environment = _environment("api-1")
        per_process = int(environment["POSTGRES_POOL_SIZE"]) + int(
            environment["POSTGRES_MAX_OVERFLOW"]
        )
        # Two replicas, one worker, plus one each for `migrate` and the
        # backup's `pg_dump` during a deploy.
        ceiling = per_process * 3 + 2
        command = " ".join(str(item) for item in _service("postgres")["command"])
        declared = re.search(r"max_connections=(\d+)", command)
        assert declared is not None, "the production command sets no max_connections"
        maximum = int(declared.group(1))
        assert ceiling <= maximum - 3, (
            f"the tier can open {ceiling} connections against max_connections {maximum}, "
            "leaving nothing for the three superuser-reserved slots an operator needs to "
            "get in when it is saturated"
        )


class TestTheSchedulerRoleSplitIsReal:
    """A64-030.2, F-3.

    The compose file writes every scheduler flag out explicitly so the shape
    is visible rather than inferred from defaults — and one of them landed
    nowhere. These tests hold the two halves together: the flags compose sets
    are exactly the ones listed here, and every one of them actually reaches
    a settings field.
    """

    def test_the_flags_compose_sets_are_the_flags_this_test_knows_about(self) -> None:
        declared = {
            key
            for key in _environment("api-1")
            if key.endswith("_ENABLED") and key != "RATE_LIMIT_ENABLED"
        }
        assert declared == set(SCHEDULER_FLAGS), (
            "the compose file's scheduler flags and this test's mapping have diverged. "
            "A flag with no entry here is a flag nothing proves is read."
        )

    @pytest.mark.parametrize("variable", sorted(SCHEDULER_FLAGS))
    def test_setting_the_variable_to_false_reaches_the_field(
        self, variable: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The regression that closes F-3.

        `AnalyticsSettings` was a plain `BaseModel`, so it had no environment
        source at all: `ANALYTICS_RETENTION_ENABLED=false` was read by
        nothing and the retention sweep ran on both API replicas as well as
        the worker.
        """
        section, field = SCHEDULER_FLAGS[variable]
        monkeypatch.setenv(variable, "false")

        assert getattr(section(), field) is False, (
            f"{variable} does not reach {section.__name__}.{field}. The compose file sets "
            "it believing the job is disabled on that role; it is not, and every replica "
            "runs another copy of the sweep."
        )

    @pytest.mark.parametrize("variable", sorted(SCHEDULER_FLAGS))
    def test_the_two_roles_disagree_about_every_flag(self, variable: str) -> None:
        """The split itself: schedulers off on the replicas, on for the one
        worker — except the gateway forwarder, which is on everywhere because
        a node that does not drain its own mailbox receives no frames from
        anywhere else."""
        replica = _environment("api-1")[variable]
        worker = _environment("worker")[variable]

        if variable == "GATEWAY_FORWARDING_ENABLED":
            assert replica == "true" and worker == "false"
        else:
            assert (replica, worker) == ("false", "true"), (
                f"{variable} is {replica!r} on a replica and {worker!r} on the worker"
            )

    def test_the_replicas_differ_in_nothing_but_their_node_id(self) -> None:
        first, second = (_environment(name) for name in REPLICAS)
        differing = {key for key in first | second if first.get(key) != second.get(key)}
        assert differing == {"GATEWAY_NODE_ID"}, (
            f"the two replicas' environments differ in {sorted(differing)}. A difference "
            "between them is one nobody finds until traffic lands on the wrong one."
        )

    @pytest.mark.parametrize("service", [*REPLICAS, "worker"])
    def test_every_long_running_process_is_named(self, service: str) -> None:
        """Unset, `resolve_node_id` draws a random identifier — correct for
        the registry and illegible in a log search."""
        node_id = _environment(service)["GATEWAY_NODE_ID"]
        assert resolve_node_id(GatewaySettings(node_id=node_id)) == service


class TestThePrometheusThatArmsTheAlerts:
    """A64-030.2. `infrastructure/observability/` held a finished Prometheus
    configuration and twenty-nine alert rules, `node-exporter` published into
    the compose network, `runbooks.md` said "Prometheus runs on the same host
    as everything it watches" — and no compose file ran it. Every rule was
    unarmed, including `BackupNeverSucceeded`.
    """

    def test_it_loads_the_alert_rules_that_already_existed(self) -> None:
        mounts = [str(volume) for volume in _service("prometheus")["volumes"]]
        assert any("alerts.yml" in mount for mount in mounts), (
            "prometheus mounts no alert rules, so the twenty-nine in "
            "infrastructure/observability/alerts.yml stay unarmed"
        )
        assert any("prometheus.yml" in mount for mount in mounts)

    def test_the_rule_file_lands_where_prometheus_yml_names_it(self) -> None:
        """`rule_files: [alerts.yml]` is resolved relative to the config
        file's own directory, so this mount path is not a free choice."""
        config = yaml.safe_load(
            (_REPO / "infrastructure" / "observability" / "prometheus.yml").read_text()
        )
        mounts = [str(volume) for volume in _service("prometheus")["volumes"]]
        for rule_file in config["rule_files"]:
            assert any(mount.endswith(f"/etc/prometheus/{rule_file}:ro") for mount in mounts), (
                f"prometheus.yml loads {rule_file}, which is mounted nowhere"
            )

    def test_the_scrape_credential_arrives_as_the_file_prometheus_yml_asks_for(self) -> None:
        config = yaml.safe_load(
            (_REPO / "infrastructure" / "observability" / "prometheus.yml").read_text()
        )
        wanted = {
            job["authorization"]["credentials_file"]
            for job in config["scrape_configs"]
            if "authorization" in job
        }
        delivered = {str(item["target"]) for item in _service("prometheus")["configs"]}
        assert wanted <= delivered, (
            f"prometheus.yml reads {sorted(wanted)} and compose delivers {sorted(delivered)}. "
            "A missing credential file is a scrape that 401s for ever, which looks exactly "
            "like a target that is down."
        )

    def test_the_token_has_one_source(self) -> None:
        """`${OPS_TOKEN}` from `production.env`, not a second copy on the
        host that can fall out of step with the API's."""
        assert _compose()["configs"]["ops_token"]["content"] == "${OPS_TOKEN}"

    def test_retention_is_bounded_by_time_and_by_size(self) -> None:
        """Time answers "how far back does a review look". Size is what stops
        `DiskWillFillSoon` becoming an alert about the alerting."""
        command = " ".join(str(item) for item in _service("prometheus")["command"])
        assert "--storage.tsdb.retention.time=15d" in command
        assert "--storage.tsdb.retention.size=" in command

    def test_it_exposes_no_endpoint_that_reloads_or_deletes(self) -> None:
        command = " ".join(str(item) for item in _service("prometheus")["command"])
        assert "--web.enable-admin-api" not in command, (
            "the admin API can delete series, on a service with no authentication"
        )
        assert "--web.enable-lifecycle" not in command

    def test_its_history_survives_a_restart(self) -> None:
        mounts = [str(volume) for volume in _service("prometheus")["volumes"]]
        assert any(mount.startswith("prometheus_data:") for mount in mounts)

    def test_the_image_is_pinned(self) -> None:
        image = str(_service("prometheus")["image"])
        assert re.search(r":v\d+\.\d+\.\d+$", image), f"{image} is not pinned — CLAUDE.md §2.6"


class TestTheClientVolumesDoNotAccumulate:
    """A64-030.2, N-8. The copy was additive into a persistent volume, so a
    file **deleted** in a later release survived in it for ever.

    That is not cosmetic: a stale `sw.js` is a service worker the browser
    keeps honouring, and a stale hashed asset is a bundle the new
    `index.html` no longer references but a cached old one still does.
    """

    @pytest.mark.parametrize("service", sorted(CARRIERS))
    def test_the_target_is_cleared_before_the_copy(self, service: str) -> None:
        body = _shell_body(service)
        clear, _, copy = body.partition("cp ")
        assert copy, f"{service} no longer copies anything: {body}"
        assert "rm -rf" in clear, (
            f"{service} copies into a persistent volume without clearing it first, so a "
            "file removed in a later release stays served for ever"
        )

    @pytest.mark.parametrize("service", sorted(CARRIERS))
    def test_the_clear_cannot_name_the_mount_point_itself(self, service: str) -> None:
        """`find … -mindepth 1` removes children and can never remove the
        directory the volume is mounted at — there is no shape of this
        command that deletes the volume rather than its contents. It also
        reaches dotfiles, which `rm -rf /srv/web/*` does not."""
        body = _shell_body(service)
        assert "-mindepth 1" in body, (
            f"{service} clears its target with something other than a depth-bounded find; "
            "a bare glob misses dotfiles and a bare path is one typo from the mount point"
        )

    @pytest.mark.parametrize("service", sorted(CARRIERS))
    def test_the_copy_preserves_modes_and_timestamps(self, service: str) -> None:
        """nginx serves these files directly, so their modes are not
        incidental."""
        assert "cp -a " in _shell_body(service)


class TestTheEdgeHasTheDescriptorsItWasSizedFor:
    """A64-030.2, E-2 — a ceiling nginx announced and nobody read.

    `nginx.conf` asks for 4096 connections per worker. Containers on the
    production host inherit a soft `nofile` of 1024, and nginx warned at
    every start:

        [warn] 4096 worker_connections exceed open file resource limit: 1024

    A proxied request holds two descriptors — client and upstream — so the
    real ceiling was roughly five hundred concurrent requests per worker on
    the only process facing the internet, and reaching it does not fail
    loudly: accepts stall and clients hang.

    Raised rather than capped: 4096 is the number the edge was sized for and
    1024 is a runtime default that happened to be smaller.
    """

    def _nofile(self) -> dict[str, int]:
        limits = _service("nginx").get("ulimits", {})
        assert "nofile" in limits, (
            "the nginx service declares no nofile ulimit, so it inherits the daemon "
            "default of 1024 and cannot reach its configured worker_connections"
        )
        nofile = limits["nofile"]
        assert isinstance(nofile, dict), (
            f"nofile must state soft and hard explicitly, got {nofile!r} — a bare integer "
            "sets both and hides which one was meant"
        )
        return nofile

    def test_soft_and_hard_are_both_stated(self) -> None:
        nofile = self._nofile()
        assert "soft" in nofile and "hard" in nofile

    def test_the_soft_limit_covers_worker_connections(self) -> None:
        """nginx's own check: it compares `worker_connections` against the
        soft limit and warns when the limit is smaller."""
        configured = re.search(
            r"worker_connections\s+(\d+)", (PRODUCTION / "nginx" / "nginx.conf").read_text()
        )
        assert configured is not None, "nginx.conf declares no worker_connections"
        wanted = int(configured.group(1))
        soft = int(self._nofile()["soft"])
        assert soft >= wanted, (
            f"nofile soft {soft} is below worker_connections {wanted}; nginx will warn at "
            "every start and silently cap below its configured capacity"
        )

    def test_hard_is_at_least_soft(self) -> None:
        nofile = self._nofile()
        assert int(nofile["hard"]) >= int(nofile["soft"]), (
            "a soft limit above the hard limit is refused by the kernel and the container "
            "will not start"
        )

    def test_the_limit_stays_inside_the_hosts_hard_ceiling(self) -> None:
        """524288 is what this host's daemon allows a container to raise to.
        A value above it is a container that cannot start, which is a worse
        outcome than the warning this replaces."""
        assert int(self._nofile()["hard"]) <= 524288

    def test_only_the_edge_needs_this(self) -> None:
        """Stated so the next reader does not copy it everywhere. The API
        holds WebSockets but is bounded by its own pool sizes and by the
        edge in front of it; nothing else here multiplies descriptors per
        connection the way a proxy does."""
        elsewhere = [
            name
            for name, service in _compose()["services"].items()
            if name != "nginx" and "nofile" in (service.get("ulimits") or {})
        ]
        assert not elsewhere, f"{elsewhere} also raise nofile; document why or remove it"
