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
"""

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

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


class TestIssuanceFailureDoesNotBlockTheEdge:
    """`issue.sh` must leave a certificate behind and exit zero.

    Driven for real rather than grepped: the property is the exit code of a
    script whose control flow is three branches deep, and a regex over it
    would pass on any rearrangement that reintroduced the abort.
    """

    @pytest.fixture
    def stub_path(self, tmp_path: Path) -> Path:
        """A `certbot` on PATH that always fails, as it does on a first boot."""
        binaries = tmp_path / "bin"
        binaries.mkdir()
        failing = binaries / "certbot"
        failing.write_text("#!/bin/sh\necho 'stub certbot: challenge unanswerable' >&2\nexit 1\n")
        failing.chmod(0o755)
        return binaries

    def _run(self, stub_path: Path, letsencrypt: Path) -> subprocess.CompletedProcess[str]:
        openssl = shutil.which("openssl")
        assert openssl is not None, "openssl is needed to write the stopgap"
        script = ISSUE_SH.read_text().replace("/etc/letsencrypt", str(letsencrypt))
        runnable = letsencrypt.parent / "issue.sh"
        runnable.write_text(script)
        return subprocess.run(
            ["sh", str(runnable)],
            env={
                "PATH": f"{stub_path}:{Path(openssl).parent}:/usr/bin:/bin",
                "ARENA64_DOMAIN": "arena64.example",
                "ARENA64_ACME_EMAIL": "ops@arena64.example",
            },
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_it_exits_zero_so_nginx_is_released(self, stub_path: Path, tmp_path: Path) -> None:
        result = self._run(stub_path, tmp_path / "letsencrypt")
        assert result.returncode == 0, (
            "issue.sh aborted on a failed issuance. nginx waits on this container with "
            f"`service_completed_successfully`, so the edge never starts.\n{result.stderr}"
        )

    def test_it_leaves_a_certificate_nginx_can_start_on(
        self, stub_path: Path, tmp_path: Path
    ) -> None:
        letsencrypt = tmp_path / "letsencrypt"
        self._run(stub_path, letsencrypt)

        live = letsencrypt / "live" / "arena64.example"
        for name in ("fullchain.pem", "privkey.pem", "chain.pem"):
            assert (live / name).is_file(), f"{name} is missing; nginx will not start"

    def test_the_failure_is_visible_rather_than_swallowed(
        self, stub_path: Path, tmp_path: Path
    ) -> None:
        """Exiting zero is only defensible because the state stays readable."""
        letsencrypt = tmp_path / "letsencrypt"
        result = self._run(stub_path, letsencrypt)

        marker = letsencrypt / "live" / "arena64.example" / ".self-signed"
        assert marker.is_file(), (
            "the .self-signed marker is gone, so nothing records that this host is "
            "serving a certificate nobody trusts — and renew.sh retries on it"
        )
        assert "FAILED" in result.stderr, f"the failure was not reported: {result.stderr!r}"


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
