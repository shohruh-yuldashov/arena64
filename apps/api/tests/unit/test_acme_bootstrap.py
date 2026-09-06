"""The ACME bootstrap must never occupy Certbot's lineage namespace — A64-030.2.

## The incident this file exists for

Arena64's first real production issuance reached Let's Encrypt, validated
`arena64.gg`, `www.arena64.gg` and `admin.arena64.gg`, finalised the order
and **received a certificate**. Certbot then threw it away:

    certbot.errors.CertStorageError: live directory exists for arena64.gg

`RenewableCert.new_lineage` refuses to create a lineage when
`live/<name>` exists and is non-empty, and the bootstrap wrote its
self-signed stopgap directly into that directory. The stopgap that existed
to let nginx start was simultaneously what made issuance impossible — on
every retry, for ever, because the stopgap is only removed on success.

## Why these tests drive real scripts and a real filesystem

Every previous test over this area read the shell source. That is exactly
the class of check that could not have caught this: the defect was not in
what `issue.sh` says, it was in where the file it creates lands relative to
a directory another program owns. So these run `issue.sh` for real against a
temporary `/etc/letsencrypt`, with a stub `certbot` on `PATH`, and assert on
the resulting tree.

`TestCertbotStorageContract` goes further and calls Certbot's own
`RenewableCert.new_lineage` inside the pinned Certbot image, so the guard
being relied upon is the guard that actually ships.

## The second incident — A64-030.3

The recovery above was run without `--no-deps`. Compose honoured
`certbot`'s `depends_on: certbot-init`, so `issue.sh` ran first and asked
Let's Encrypt for a certificate while the orphan renewal config was still in
place. `unique_lineage_name` returned `arena64.gg-0001.conf`, and Certbot
stored a real, trusted, correctly-laid-out certificate at
**`live/arena64.gg-0001`**.

Nothing was wrong with that certificate. What was wrong was the question
every script asked about it — *does `live/$ARENA64_DOMAIN` exist?* — which
Certbot had never promised to answer yes. The renewal loop would have read
"no lineage", called `issue.sh` every five minutes, and bought a duplicate
certificate on every pass until the weekly limit stopped it.

So `TestLineageDiscovery` drives the real `arena64_discover_lineage` over
every shape a host can be in, including the exact shape production was left
in, and `TestTheOperationalContract` pins the `--no-deps` that would have
prevented the whole thing.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[4]
PRODUCTION = _REPO / "infrastructure" / "production"
CERTBOT = PRODUCTION / "certbot"
ISSUE_SH = CERTBOT / "issue.sh"
RENEW_SH = CERTBOT / "renew.sh"
LINEAGE_SH = CERTBOT / "lineage.sh"
RECOVER_SH = CERTBOT / "recover-legacy-stopgap.sh"
COMPOSE = PRODUCTION / "compose.yml"
NGINX = PRODUCTION / "nginx"

DOMAIN = "arena64.example"

#: `arena64_discover_lineage`'s four answers, mirrored from `lineage.sh`.
FOUND = 0
NONE = 1
AMBIGUOUS = 2
MALFORMED = 3

_OPENSSL = shutil.which("openssl")
assert _OPENSSL is not None, "openssl is needed to write certificates these tests read"
OPENSSL = _OPENSSL


#: The image the production `certbot` service runs, read from the compose
#: file so this cannot drift from what is deployed.
def _certbot_image() -> str:
    document = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    return str(document["services"]["certbot"]["image"])


def _docker_available() -> bool:
    return (
        shutil.which("docker") is not None
        and subprocess.run(["docker", "info"], capture_output=True).returncode == 0
    )


needs_docker = pytest.mark.skipif(
    not _docker_available(), reason="needs a Docker daemon to run the real Certbot image"
)


@pytest.fixture
def letsencrypt(tmp_path: Path) -> Path:
    """A throwaway `/etc/letsencrypt`, laid out as the volume would be."""
    root = tmp_path / "letsencrypt"
    for sub in ("live", "archive", "renewal"):
        (root / sub).mkdir(parents=True)
    return root


def certificate_names(domain: str = DOMAIN) -> list[str]:
    """The three names one Arena64 certificate carries — deployment.md §8.10."""
    return [domain, f"www.{domain}", f"admin.{domain}"]


def _lineage_shell(letsencrypt: Path, name: str, domain: str) -> str:
    """The shell a stub `certbot` runs to store a lineage the way Certbot does.

    A *real* certificate, because discovery reads the SAN to decide whether a
    lineage is the one this deployment asked for — a placeholder string would
    make every test agree with a check that never ran.
    """
    archive = letsencrypt / "archive" / name
    live = letsencrypt / "live" / name
    san = ",".join(f"DNS:{n}" for n in certificate_names(domain))
    return f"""
mkdir -p {archive} {live}
openssl req -x509 -newkey rsa:2048 -nodes -days 90 \\
  -subj "/CN={domain}" -addext "subjectAltName={san}" \\
  -keyout {archive}/privkey1.pem -out {archive}/fullchain1.pem 2>/dev/null
cp {archive}/fullchain1.pem {archive}/chain1.pem
cp {archive}/fullchain1.pem {archive}/cert1.pem
for kind in fullchain privkey chain cert; do
  ln -sf ../../archive/{name}/${{kind}}1.pem {live}/${{kind}}.pem
done
printf 'archive_dir = %s\\n' {archive} > {letsencrypt}/renewal/{name}.conf
"""


def write_lineage(
    letsencrypt: Path,
    name: str,
    *,
    domain: str = DOMAIN,
    names: Sequence[str] | None = None,
    renewal: str | None = None,
) -> Path:
    """Store a lineage under `name`, exactly as Certbot would.

    `names` overrides the SAN so a lineage can be well formed and still not
    be *ours*; `renewal` overrides the config body so the zero-byte orphan a
    failed attempt leaves behind can be reproduced.
    """
    archive = letsencrypt / "archive" / name
    live = letsencrypt / "live" / name
    archive.mkdir(parents=True, exist_ok=True)
    live.mkdir(parents=True, exist_ok=True)
    san = ",".join(f"DNS:{n}" for n in (names if names is not None else certificate_names(domain)))
    subprocess.run(
        [
            OPENSSL,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "90",
            "-subj",
            f"/CN={domain}",
            "-addext",
            f"subjectAltName={san}",
            "-keyout",
            str(archive / "privkey1.pem"),
            "-out",
            str(archive / "fullchain1.pem"),
        ],
        check=True,
        capture_output=True,
    )
    shutil.copy(archive / "fullchain1.pem", archive / "chain1.pem")
    shutil.copy(archive / "fullchain1.pem", archive / "cert1.pem")
    for kind in ("fullchain", "privkey", "chain", "cert"):
        link = live / f"{kind}.pem"
        link.unlink(missing_ok=True)
        link.symlink_to(Path("..") / ".." / "archive" / name / f"{kind}1.pem")
    body = f"archive_dir = {archive}\n" if renewal is None else renewal
    (letsencrypt / "renewal" / f"{name}.conf").write_text(body)
    return live


def _rewrite(
    script: Path, letsencrypt: Path, destination: Path, swaps: dict[str, str] | None = None
) -> Path:
    """Copy a shipped script with its hard-coded paths pointed at a fake root."""
    body = script.read_text(encoding="utf-8").replace("/etc/letsencrypt", str(letsencrypt))
    for original, replacement in (swaps or {}).items():
        body = body.replace(original, replacement)
    destination.write_text(body)
    return destination


def _sh_env(letsencrypt: Path, extra_path: Path | None = None, **overrides: str) -> dict[str, str]:
    binaries = str(extra_path) + ":" if extra_path else ""
    env = {
        "PATH": f"{binaries}{Path(OPENSSL).parent}:/usr/bin:/bin",
        "ARENA64_DOMAIN": DOMAIN,
        "ARENA64_ACME_EMAIL": f"ops@{DOMAIN}",
    }
    env.update(overrides)
    return env


def _lineage_for(letsencrypt: Path, tmp_path: Path) -> Path:
    return _rewrite(LINEAGE_SH, letsencrypt, tmp_path / "lineage.sh")


def discover(letsencrypt: Path, tmp_path: Path, *, domain: str = DOMAIN) -> tuple[int, str, str]:
    """Run the shipped `arena64_discover_lineage` and return (status, path, log)."""
    lineage = _lineage_for(letsencrypt, tmp_path)
    probe = tmp_path / "discover.sh"
    probe.write_text(f'. {lineage}\narena64_discover_lineage "$ARENA64_DOMAIN"\n')
    result = subprocess.run(
        ["sh", str(probe)],
        env=_sh_env(letsencrypt, ARENA64_DOMAIN=domain),
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.returncode, result.stdout.strip(), result.stderr


def _run_issue(
    letsencrypt: Path,
    tmp_path: Path,
    *,
    certbot_exits: int,
    creates_lineage: bool = False,
    lineage_name: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the real `issue.sh` with a stub `certbot` and a fake root.

    The stub stands in for the ACME client: it can succeed while creating a
    proper Certbot lineage — under the name Certbot actually chose, which is
    not always the one `--cert-name` asked for — succeed without creating
    one, or fail outright.
    """
    binaries = tmp_path / "bin"
    binaries.mkdir(exist_ok=True)

    lineage_script = ""
    if creates_lineage:
        lineage_script = _lineage_shell(letsencrypt, lineage_name or DOMAIN, DOMAIN)
    stub = binaries / "certbot"
    stub.write_text(
        f"#!/bin/sh\necho 'stub certbot invoked' >&2\n{lineage_script}\nexit {certbot_exits}\n"
    )
    stub.chmod(0o755)

    lineage = _lineage_for(letsencrypt, tmp_path)
    runnable = _rewrite(
        ISSUE_SH,
        letsencrypt,
        tmp_path / "issue.sh",
        {"/usr/local/bin/lineage.sh": str(lineage)},
    )
    return subprocess.run(
        ["sh", str(runnable)],
        env=_sh_env(letsencrypt, binaries),
        capture_output=True,
        text=True,
        timeout=120,
    )


def _run_renew(letsencrypt: Path, tmp_path: Path, *, timeout: float = 30.0) -> str:
    """Run `renew.sh` until it has chosen a mode, then kill it.

    The loop never returns, and the mode it picked is decided and logged
    before the first `sleep`. Both stubs record themselves, so a test can
    assert the stronger thing: that no ACME request was made at all.
    """
    binaries = tmp_path / "bin"
    binaries.mkdir(exist_ok=True)
    stub = binaries / "certbot"
    stub.write_text("#!/bin/sh\necho 'stub certbot invoked' >&2\nexit 0\n")
    stub.chmod(0o755)

    fake_issue = tmp_path / "fake-issue.sh"
    fake_issue.write_text("#!/bin/sh\necho 'stub issue.sh invoked' >&2\nexit 0\n")

    lineage = _lineage_for(letsencrypt, tmp_path)
    runnable = _rewrite(
        RENEW_SH,
        letsencrypt,
        tmp_path / "renew.sh",
        {
            "/usr/local/bin/lineage.sh": str(lineage),
            "/usr/local/bin/issue.sh": str(fake_issue),
        },
    )

    transcript = tmp_path / "renew.log"
    settled = ("HELD", "no certificate lineage yet", "renewing from")
    with transcript.open("w") as sink:
        process = subprocess.Popen(
            ["sh", str(runnable)],
            env=_sh_env(letsencrypt, binaries),
            stdout=sink,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if any(marker in transcript.read_text() for marker in settled):
                    break
                time.sleep(0.05)
        finally:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            process.wait(timeout=10)
    return transcript.read_text()


class TestTheBootstrapStaysOutOfCertbotsNamespace:
    """The invariant the incident violated, asserted against a real run."""

    def test_it_does_not_create_certbots_live_directory(
        self, letsencrypt: Path, tmp_path: Path
    ) -> None:
        """The single assertion that would have prevented the incident."""
        _run_issue(letsencrypt, tmp_path, certbot_exits=1)

        assert not (letsencrypt / "live" / DOMAIN).exists(), (
            f"the bootstrap created live/{DOMAIN}. Certbot's new_lineage refuses to create a "
            "lineage over an existing live directory, so the first real issuance obtains a "
            "certificate from Let's Encrypt and then discards it — permanently, on every retry."
        )

    def test_it_touches_neither_archive_nor_renewal(
        self, letsencrypt: Path, tmp_path: Path
    ) -> None:
        _run_issue(letsencrypt, tmp_path, certbot_exits=1)
        assert not list((letsencrypt / "archive").iterdir())
        assert not list((letsencrypt / "renewal").iterdir())

    def test_the_stopgap_lands_in_arena64s_own_directory(
        self, letsencrypt: Path, tmp_path: Path
    ) -> None:
        _run_issue(letsencrypt, tmp_path, certbot_exits=1)
        stopgap = letsencrypt / "arena64" / "stopgap" / DOMAIN
        for name in ("fullchain.pem", "privkey.pem", "chain.pem"):
            assert (stopgap / name).is_file(), f"{name} missing from {stopgap}"

    def test_the_private_key_is_not_world_readable(self, letsencrypt: Path, tmp_path: Path) -> None:
        _run_issue(letsencrypt, tmp_path, certbot_exits=1)
        key = letsencrypt / "arena64" / "stopgap" / DOMAIN / "privkey.pem"
        mode = key.stat().st_mode & 0o777
        assert mode & 0o007 == 0, f"stopgap private key is {mode:o}"

    def test_nginx_has_something_to_start_on(self, letsencrypt: Path, tmp_path: Path) -> None:
        """The whole reason a stopgap exists: nginx will not start without
        the files its `ssl_certificate` directives name."""
        _run_issue(letsencrypt, tmp_path, certbot_exits=1)
        current = letsencrypt / "arena64" / "current" / DOMAIN
        assert current.is_symlink(), "the stable path nginx reads is not a symlink"
        for name in ("fullchain.pem", "privkey.pem", "chain.pem"):
            assert (current / name).is_file(), f"{name} does not resolve through the stable path"

    def test_a_failed_issuance_exits_zero_so_nginx_is_released(
        self, letsencrypt: Path, tmp_path: Path
    ) -> None:
        result = _run_issue(letsencrypt, tmp_path, certbot_exits=1)
        assert result.returncode == 0, result.stderr
        assert "FAILED" in result.stderr, "the failure was not reported"


class TestTheTransitionToARealLineage:
    def test_success_repoints_the_stable_path_at_certbots_lineage(
        self, letsencrypt: Path, tmp_path: Path
    ) -> None:
        result = _run_issue(letsencrypt, tmp_path, certbot_exits=0, creates_lineage=True)
        assert result.returncode == 0, result.stderr

        current = letsencrypt / "arena64" / "current" / DOMAIN
        assert current.is_symlink()
        assert os.path.realpath(current) == os.path.realpath(letsencrypt / "live" / DOMAIN), (
            "the stable path still points at the stopgap after a successful issuance"
        )
        leaf = (current / "fullchain.pem").read_text()
        assert leaf.startswith("-----BEGIN CERTIFICATE-----"), "the stable path is not a PEM"

    def test_the_real_lineage_uses_certbots_symlink_layout(
        self, letsencrypt: Path, tmp_path: Path
    ) -> None:
        _run_issue(letsencrypt, tmp_path, certbot_exits=0, creates_lineage=True)
        live = letsencrypt / "live" / DOMAIN / "fullchain.pem"
        assert live.is_symlink()
        assert os.path.realpath(live).startswith(str(letsencrypt / "archive" / DOMAIN))

    def test_a_second_run_short_circuits(self, letsencrypt: Path, tmp_path: Path) -> None:
        """Re-running a deploy must not spend a rate-limit slot."""
        _run_issue(letsencrypt, tmp_path, certbot_exits=0, creates_lineage=True)
        again = _run_issue(letsencrypt, tmp_path, certbot_exits=1)
        assert again.returncode == 0
        assert "nothing to do" in again.stdout
        assert "stub certbot invoked" not in again.stderr, "certbot was called despite a lineage"

    def test_certbot_success_without_a_lineage_is_not_believed(
        self, letsencrypt: Path, tmp_path: Path
    ) -> None:
        """The mirror of the incident: a zero exit with nothing on disk must
        not flip the stable path at a lineage that is not there."""
        result = _run_issue(letsencrypt, tmp_path, certbot_exits=0, creates_lineage=False)
        current = letsencrypt / "arena64" / "current" / DOMAIN
        assert os.path.realpath(current) == os.path.realpath(
            letsencrypt / "arena64" / "stopgap" / DOMAIN
        )
        assert "discovery reports NONE" in result.stderr


class TestRestartConverges:
    """Every intermediate state must reach the right place on a re-run."""

    def test_from_nothing(self, letsencrypt: Path, tmp_path: Path) -> None:
        _run_issue(letsencrypt, tmp_path, certbot_exits=1)
        assert (letsencrypt / "arena64" / "current" / DOMAIN).is_symlink()

    def test_from_stopgap_only(self, letsencrypt: Path, tmp_path: Path) -> None:
        _run_issue(letsencrypt, tmp_path, certbot_exits=1)
        _run_issue(letsencrypt, tmp_path, certbot_exits=1)
        current = letsencrypt / "arena64" / "current" / DOMAIN
        assert os.path.realpath(current) == os.path.realpath(
            letsencrypt / "arena64" / "stopgap" / DOMAIN
        )

    def test_from_a_valid_lineage(self, letsencrypt: Path, tmp_path: Path) -> None:
        _run_issue(letsencrypt, tmp_path, certbot_exits=0, creates_lineage=True)
        _run_issue(letsencrypt, tmp_path, certbot_exits=1)
        current = letsencrypt / "arena64" / "current" / DOMAIN
        assert os.path.realpath(current) == os.path.realpath(letsencrypt / "live" / DOMAIN)

    def test_a_dangling_stable_link_is_repaired(self, letsencrypt: Path, tmp_path: Path) -> None:
        _run_issue(letsencrypt, tmp_path, certbot_exits=1)
        current = letsencrypt / "arena64" / "current" / DOMAIN
        current.unlink()
        current.symlink_to(letsencrypt / "nowhere")
        _run_issue(letsencrypt, tmp_path, certbot_exits=1)
        assert (current / "fullchain.pem").is_file()


def _run_recover(letsencrypt: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    lineage = _lineage_for(letsencrypt, tmp_path)
    runnable = _rewrite(
        RECOVER_SH,
        letsencrypt,
        tmp_path / "recover.sh",
        {"/usr/local/bin/lineage.sh": str(lineage)},
    )
    return subprocess.run(
        ["sh", str(runnable)],
        env=_sh_env(letsencrypt),
        capture_output=True,
        text=True,
        timeout=60,
    )


def _legacy_state(letsencrypt: Path, *, marker: bool = True, orphan: bool = True) -> Path:
    """Exactly what the failed production attempt left behind."""
    live = letsencrypt / "live" / DOMAIN
    live.mkdir(parents=True)
    for name in ("fullchain.pem", "privkey.pem", "chain.pem"):
        (live / name).write_text("SENTINEL-KEYMATERIAL-DO-NOT-PRINT\n")
    if marker:
        (live / ".self-signed").touch()
    if orphan:
        (letsencrypt / "renewal" / f"{DOMAIN}.conf").write_text("")
    return live


class TestLegacyRecovery:
    """The guarded, one-time cleanup for a host that ran the old bootstrap."""

    def test_it_quarantines_the_legacy_stopgap_and_the_orphan_renewal(
        self, letsencrypt: Path, tmp_path: Path
    ) -> None:
        _legacy_state(letsencrypt)
        result = _run_recover(letsencrypt, tmp_path)

        assert result.returncode == 0, result.stderr
        assert not (letsencrypt / "live" / DOMAIN).exists(), "live/ is still occupied"
        assert not (letsencrypt / "renewal" / f"{DOMAIN}.conf").exists(), (
            "the orphan renewal config survived. Certbot's unique_lineage_name would then "
            f"allocate {DOMAIN}-0001 and create a lineage under a name the edge does not read."
        )
        quarantine = list((letsencrypt / "arena64" / "quarantine").iterdir())
        assert quarantine, "nothing was quarantined"
        kept = list(quarantine[0].iterdir())
        assert any(p.name.startswith("live-") for p in kept), "the stopgap was not preserved"
        assert any(p.name.startswith("renewal-") for p in kept), (
            "the renewal conf was not preserved"
        )

    def test_nothing_is_deleted(self, letsencrypt: Path, tmp_path: Path) -> None:
        _legacy_state(letsencrypt)
        _run_recover(letsencrypt, tmp_path)
        moved = letsencrypt / "arena64" / "quarantine"
        contents = [p for p in moved.rglob("*.pem")]
        assert len(contents) == 3, f"expected the three stopgap files preserved, found {contents}"

    def test_it_refuses_a_real_certbot_lineage(self, letsencrypt: Path, tmp_path: Path) -> None:
        """The most important refusal: never destroy a working certificate."""
        live = letsencrypt / "live" / DOMAIN
        archive = letsencrypt / "archive" / DOMAIN
        live.mkdir(parents=True)
        archive.mkdir(parents=True)
        for kind in ("fullchain", "privkey", "chain"):
            (archive / f"{kind}1.pem").write_text("real\n")
            (live / f"{kind}.pem").symlink_to(archive / f"{kind}1.pem")

        result = _run_recover(letsencrypt, tmp_path)

        assert result.returncode != 0, "recovery ran against a real lineage"
        assert "REFUSING" in result.stderr
        assert (live / "fullchain.pem").exists(), "a real lineage was disturbed"

    def test_it_refuses_without_the_legacy_fingerprint(
        self, letsencrypt: Path, tmp_path: Path
    ) -> None:
        """A directory of unknown provenance is never moved."""
        _legacy_state(letsencrypt, marker=False)
        result = _run_recover(letsencrypt, tmp_path)
        assert result.returncode != 0
        assert "no .self-signed marker" in result.stderr
        assert (letsencrypt / "live" / DOMAIN).exists()

    def test_it_refuses_when_an_archive_lineage_exists(
        self, letsencrypt: Path, tmp_path: Path
    ) -> None:
        _legacy_state(letsencrypt)
        archive = letsencrypt / "archive" / DOMAIN
        archive.mkdir(parents=True)
        (archive / "fullchain1.pem").write_text("real\n")

        result = _run_recover(letsencrypt, tmp_path)

        assert result.returncode != 0
        assert "is not empty" in result.stderr
        assert (letsencrypt / "live" / DOMAIN).exists()

    def test_it_is_safe_to_run_twice(self, letsencrypt: Path, tmp_path: Path) -> None:
        _legacy_state(letsencrypt)
        first = _run_recover(letsencrypt, tmp_path)
        second = _run_recover(letsencrypt, tmp_path)
        assert first.returncode == 0
        assert second.returncode == 0, second.stderr
        assert "nothing to do" in second.stdout

    def test_it_prints_no_key_material(self, letsencrypt: Path, tmp_path: Path) -> None:
        _legacy_state(letsencrypt)
        result = _run_recover(letsencrypt, tmp_path)
        assert "PRIVATE KEY" not in result.stdout + result.stderr
        assert "SENTINEL-KEYMATERIAL-DO-NOT-PRINT" not in result.stdout + result.stderr


@needs_docker
class TestCertbotStorageContract:
    """The guard being relied on, exercised inside the pinned Certbot image.

    These assert Certbot's behaviour rather than Arena64's, because the whole
    fix rests on two facts about it: that `live/<name>` blocks lineage
    creation, and that a leftover renewal config silently renames the
    lineage. Both were verified by hand during the incident; this keeps them
    verified.
    """

    def _probe(self, script: str) -> dict[str, Any]:
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--entrypoint",
                "python",
                _certbot_image(),
                "-c",
                script,
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert result.returncode == 0, result.stderr[-2000:]
        parsed: dict[str, Any] = json.loads(result.stdout.strip().splitlines()[-1])
        return parsed

    def test_an_occupied_live_directory_blocks_lineage_creation(self) -> None:
        """The incident, reproduced against the real storage module."""
        outcome = self._probe(
            "import json,os,tempfile\n"
            "from certbot._internal import storage\n"
            "from certbot import errors\n"
            "root=tempfile.mkdtemp()\n"
            "[os.makedirs(os.path.join(root,d)) for d in ('live','archive','renewal')]\n"
            "live=os.path.join(root,'live','d.test'); os.makedirs(live)\n"
            "open(os.path.join(live,'fullchain.pem'),'w').write('stopgap')\n"
            "class C:\n"
            "  def __init__(s,r):\n"
            "    s.live_dir=os.path.join(r,'live')\n"
            "    s.default_archive_dir=os.path.join(r,'archive')\n"
            "    s.renewal_configs_dir=os.path.join(r,'renewal'); s.namespace=s\n"
            "  def __getattr__(s,n): return None\n"
            "try:\n"
            "  storage.RenewableCert.new_lineage('d.test',b'c',b'k',b'ch',C(root))\n"
            "  print(json.dumps({'blocked':False,'error':None}))\n"
            "except errors.CertStorageError as e:\n"
            "  print(json.dumps({'blocked':True,'error':str(e)}))\n"
            "except Exception as e:\n"
            "  print(json.dumps({'blocked':False,'error':type(e).__name__}))\n"
        )
        assert outcome["blocked"] is True, (
            "Certbot no longer refuses an occupied live directory. If that is genuinely true "
            "of this pinned version, the bootstrap's constraint has changed — re-read "
            "certbot/lineage.sh before relaxing anything."
        )
        assert "live directory exists" in outcome["error"]

    def test_a_leftover_renewal_config_renames_the_lineage(self) -> None:
        """Why recovery must quarantine `renewal/<domain>.conf` too.

        `util.unique_lineage_name` creates the file before the live-directory
        guard runs and does not unlink it when the guard raises. On the next
        attempt it finds the name taken and allocates `<name>-0001`, so
        Certbot creates a lineage the edge does not read — quietly.
        """
        outcome = self._probe(
            "import json,os,tempfile\n"
            "from certbot import util\n"
            "root=tempfile.mkdtemp(); os.makedirs(os.path.join(root,'renewal'))\n"
            "d=os.path.join(root,'renewal')\n"
            "f1,n1=util.unique_lineage_name(d,'d.test'); f1.close()\n"
            "f2,n2=util.unique_lineage_name(d,'d.test'); f2.close()\n"
            "print(json.dumps({'first':os.path.basename(n1),'second':os.path.basename(n2)}))\n"
        )
        assert outcome["first"] == "d.test.conf"
        assert outcome["second"] == "d.test-0001.conf", (
            "a leftover renewal config no longer renames the lineage; recovery's second "
            "quarantine step may no longer be needed"
        )


class TestTheDeployedContract:
    """What the compose file and nginx templates must agree on."""

    def _compose(self) -> dict[str, Any]:
        document: dict[str, Any] = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
        return document

    def test_nginx_reads_the_arena64_owned_path(self) -> None:
        for template in sorted((NGINX / "templates").glob("*.template")):
            body = template.read_text(encoding="utf-8")
            for line in body.splitlines():
                stripped = line.strip()
                if stripped.startswith(("ssl_certificate", "ssl_trusted_certificate")):
                    assert "/etc/letsencrypt/arena64/current/" in stripped, (
                        f"{template.name} reads {stripped!r}. nginx must read the stable "
                        "Arena64-owned symlink, never Certbot's live directory."
                    )

    def test_no_repository_file_writes_into_certbots_namespace(self) -> None:
        """The rule, enforced over every script that touches the volume."""
        for script in sorted(CERTBOT.glob("*.sh")):
            body = script.read_text(encoding="utf-8")
            for line in body.splitlines():
                stripped = line.strip()
                if stripped.startswith("#") or not stripped:
                    continue
                for verb in ("mkdir -p /etc/letsencrypt/live", "mkdir /etc/letsencrypt/live"):
                    assert verb not in stripped, (
                        f"{script.name} creates Certbot's live dir: {stripped}"
                    )

    def test_both_certbot_roles_mount_the_shared_boundary(self) -> None:
        services = self._compose()["services"]
        for name in ("certbot-init", "certbot"):
            mounts = [str(v) for v in services[name]["volumes"]]
            assert any("lineage.sh" in m for m in mounts), (
                f"{name} does not mount lineage.sh, so it cannot answer 'is there a real "
                "certificate?' the same way as the other scripts"
            )

    def test_recovery_is_available_but_never_automatic(self) -> None:
        services = self._compose()["services"]
        mounts = [str(v) for v in services["certbot"]["volumes"]]
        assert any("recover-legacy-stopgap.sh" in m for m in mounts)
        assert "recover-legacy-stopgap" not in str(services["certbot"]["entrypoint"]), (
            "recovery must be run deliberately by an operator, never on container start"
        )

    def test_nginx_reloads_when_the_certificate_changes(self) -> None:
        """A first issuance that succeeds must not keep serving a browser
        warning until the next blind reload."""
        nginx = self._compose()["services"]["nginx"]
        command = " ".join(str(part) for part in nginx["command"])
        assert "readlink -f" in command, "nginx no longer watches the certificate symlink"
        assert "nginx -s reload" in command


class TestTheSymlinkFlipReallyFlips:
    """The bug the first draft of this fix shipped, kept fixed.

    `arena64/current/<domain>` is a symlink pointing at a **directory**, and
    replacing it with `mv -f new old` does not replace it: `mv` follows the
    symlink and moves `new` *inside* the directory `old` resolves to, leaves
    `old` pointing where it always did, and exits zero.

    The visible result was `issue.sh` logging "issuance complete" while nginx
    carried on serving the stopgap — the same shape as the incident this
    whole change is about, one layer down. `mv -T` is the fix; these tests
    are why it stays.
    """

    def test_flipping_an_existing_link_changes_where_it_points(
        self, letsencrypt: Path, tmp_path: Path
    ) -> None:
        _run_issue(letsencrypt, tmp_path, certbot_exits=1)
        current = letsencrypt / "arena64" / "current" / DOMAIN
        stopgap = letsencrypt / "arena64" / "stopgap" / DOMAIN
        assert os.path.realpath(current) == os.path.realpath(stopgap)

        _run_issue(letsencrypt, tmp_path, certbot_exits=0, creates_lineage=True)

        assert os.path.realpath(current) == os.path.realpath(letsencrypt / "live" / DOMAIN), (
            "the stable path still resolves to the stopgap after a successful issuance"
        )

    def test_the_flip_leaves_no_debris_in_the_stopgap(
        self, letsencrypt: Path, tmp_path: Path
    ) -> None:
        """The tell-tale of the `mv -f` bug: a stray `.next` link that ended
        up inside the directory instead of replacing the symlink."""
        _run_issue(letsencrypt, tmp_path, certbot_exits=1)
        _run_issue(letsencrypt, tmp_path, certbot_exits=0, creates_lineage=True)

        stopgap = letsencrypt / "arena64" / "stopgap" / DOMAIN
        strays = [p.name for p in stopgap.iterdir() if p.name.endswith(".next")]
        assert not strays, f"the flip moved links into the stopgap directory: {strays}"

        current_dir = letsencrypt / "arena64" / "current"
        leftovers = [p.name for p in current_dir.iterdir() if p.name.endswith(".next")]
        assert not leftovers, f"a temporary link survived the flip: {leftovers}"

    def test_the_stable_path_is_never_missing_a_certificate(
        self, letsencrypt: Path, tmp_path: Path
    ) -> None:
        """Whatever it points at, nginx must always find three readable files
        through it — that is the whole contract nginx depends on."""
        for stage in ("bootstrap", "issued"):
            if stage == "bootstrap":
                _run_issue(letsencrypt, tmp_path, certbot_exits=1)
            else:
                _run_issue(letsencrypt, tmp_path, certbot_exits=0, creates_lineage=True)
            current = letsencrypt / "arena64" / "current" / DOMAIN
            for name in ("fullchain.pem", "privkey.pem", "chain.pem"):
                assert (current / name).is_file(), f"{name} unreadable during {stage}"


class TestLineageDiscovery:
    """`arena64_discover_lineage` over every shape a host can be in.

    The second incident was not a bug in any of these scripts' logic. It was
    a question — *does `live/$ARENA64_DOMAIN` exist?* — that Certbot had
    never agreed to answer yes to. These tests are the answer to the right
    question, one row per way of getting it wrong.
    """

    def test_a_clean_host_has_no_lineage(self, letsencrypt: Path, tmp_path: Path) -> None:
        status, path, _ = discover(letsencrypt, tmp_path)
        assert status == NONE
        assert path == ""

    @pytest.mark.parametrize("suffix", ["", "-0001", "-0002"])
    def test_it_finds_the_lineage_whatever_certbot_named_it(
        self, letsencrypt: Path, tmp_path: Path, suffix: str
    ) -> None:
        """`--cert-name` is a request. `-0001` is an ordinary healthy answer."""
        name = f"{DOMAIN}{suffix}"
        write_lineage(letsencrypt, name)

        status, path, _ = discover(letsencrypt, tmp_path)

        assert status == FOUND, f"a valid lineage called {name} was not found"
        assert path == str(letsencrypt / "live" / name)

    def test_a_numbered_directory_of_regular_files_is_malformed(
        self, letsencrypt: Path, tmp_path: Path
    ) -> None:
        live = letsencrypt / "live" / f"{DOMAIN}-0001"
        live.mkdir(parents=True)
        for name in ("fullchain.pem", "privkey.pem", "chain.pem"):
            (live / name).write_text("SENTINEL-KEYMATERIAL-DO-NOT-PRINT\n")

        status, path, log = discover(letsencrypt, tmp_path)

        assert status == MALFORMED
        assert path == ""
        assert f"{DOMAIN}-0001" in log

    def test_an_orphan_renewal_config_alone_is_malformed(
        self, letsencrypt: Path, tmp_path: Path
    ) -> None:
        """The zero-byte file a failed attempt leaves, and the reason the
        next attempt gets called `-0001`. Discovery must see it, not skip it."""
        (letsencrypt / "renewal" / f"{DOMAIN}.conf").write_text("")

        status, _, log = discover(letsencrypt, tmp_path)

        assert status == MALFORMED
        assert "not a usable lineage" in log

    def test_an_empty_renewal_config_beside_a_lineage_is_malformed(
        self, letsencrypt: Path, tmp_path: Path
    ) -> None:
        write_lineage(letsencrypt, DOMAIN, renewal="")

        status, _, _ = discover(letsencrypt, tmp_path)

        assert status == MALFORMED, "a zero-byte renewal config is the orphan fingerprint"

    def test_a_dangling_live_symlink_is_malformed(self, letsencrypt: Path, tmp_path: Path) -> None:
        write_lineage(letsencrypt, DOMAIN)
        target = letsencrypt / "archive" / DOMAIN / "fullchain1.pem"
        target.unlink()

        status, _, _ = discover(letsencrypt, tmp_path)

        assert status == MALFORMED

    def test_a_lineage_pointing_outside_its_archive_is_malformed(
        self, letsencrypt: Path, tmp_path: Path
    ) -> None:
        """Certbot's layout is the evidence; a link somewhere else is not it."""
        write_lineage(letsencrypt, DOMAIN)
        write_lineage(letsencrypt, f"{DOMAIN}-0001")
        stray = letsencrypt / "live" / DOMAIN / "fullchain.pem"
        stray.unlink()
        stray.symlink_to(letsencrypt / "archive" / f"{DOMAIN}-0001" / "fullchain1.pem")

        status, _, _ = discover(letsencrypt, tmp_path)

        assert status == MALFORMED

    def test_two_valid_lineages_are_ambiguous(self, letsencrypt: Path, tmp_path: Path) -> None:
        write_lineage(letsencrypt, DOMAIN)
        write_lineage(letsencrypt, f"{DOMAIN}-0001")

        status, path, log = discover(letsencrypt, tmp_path)

        assert status == AMBIGUOUS, "discovery picked one of two equally valid lineages"
        assert path == ""
        assert "refusing to choose" in log

    def test_a_lineage_for_other_names_is_not_ours(self, letsencrypt: Path, tmp_path: Path) -> None:
        """Well formed, right name, wrong certificate.

        Serving it would fail hostname validation on two of three hosts, and
        `includeSubDomains; preload` makes that unclickable.
        """
        write_lineage(letsencrypt, DOMAIN, names=["something-else.example"])

        status, _, log = discover(letsencrypt, tmp_path)

        assert status == MALFORMED
        assert "does not cover" in log

    def test_a_lineage_missing_only_www_is_not_ours(
        self, letsencrypt: Path, tmp_path: Path
    ) -> None:
        write_lineage(letsencrypt, DOMAIN, names=[DOMAIN, f"admin.{DOMAIN}"])

        status, _, _ = discover(letsencrypt, tmp_path)

        assert status == MALFORMED

    def test_the_legacy_stopgap_pollution_is_malformed(
        self, letsencrypt: Path, tmp_path: Path
    ) -> None:
        """The first incident's shape, seen through the second incident's eyes."""
        _legacy_state(letsencrypt)

        status, _, _ = discover(letsencrypt, tmp_path)

        assert status == MALFORMED

    def test_wreckage_outranks_a_valid_sibling(self, letsencrypt: Path, tmp_path: Path) -> None:
        """The exact state production was in between the two incidents.

        A good `-0001` and legacy pollution under the base name. Nobody can
        say which is canonical without looking, so the lifecycle stops rather
        than renewing the wrong one or buying a third.
        """
        write_lineage(letsencrypt, f"{DOMAIN}-0001")
        _legacy_state(letsencrypt)

        status, _, _ = discover(letsencrypt, tmp_path)

        assert status == MALFORMED

    def test_an_unrelated_lineage_is_ignored(self, letsencrypt: Path, tmp_path: Path) -> None:
        """`other.example` is not ours to reason about, numbered or not."""
        write_lineage(letsencrypt, "other.example", domain="other.example")

        status, _, _ = discover(letsencrypt, tmp_path)

        assert status == NONE

    def test_a_non_numeric_suffix_is_not_a_candidate(
        self, letsencrypt: Path, tmp_path: Path
    ) -> None:
        """`unique_lineage_name` counts; it does not invent words."""
        write_lineage(letsencrypt, f"{DOMAIN}-backup", domain=f"{DOMAIN}-backup")

        status, _, _ = discover(letsencrypt, tmp_path)

        assert status == NONE


class TestTheProductionShape:
    """Exactly what the production host holds today — A64-030.3.

    live/arena64.gg-0001, archive/arena64.gg-0001, renewal/arena64.gg-0001.conf,
    with `arena64/current/arena64.gg` already pointing at the lineage. The
    whole point of this change is that this host is *ordinary*.
    """

    @pytest.fixture
    def production(self, letsencrypt: Path, tmp_path: Path) -> Path:
        live = write_lineage(letsencrypt, f"{DOMAIN}-0001")
        current = letsencrypt / "arena64" / "current"
        current.mkdir(parents=True)
        (current / DOMAIN).symlink_to(live)
        return letsencrypt

    def test_discovery_finds_the_numbered_lineage(self, production: Path, tmp_path: Path) -> None:
        status, path, _ = discover(production, tmp_path)
        assert status == FOUND
        assert path == str(production / "live" / f"{DOMAIN}-0001")

    def test_issue_requests_nothing(self, production: Path, tmp_path: Path) -> None:
        result = _run_issue(production, tmp_path, certbot_exits=0)

        assert result.returncode == 0, result.stderr
        assert "stub certbot invoked" not in result.stderr, (
            "a host with a valid certificate asked Let's Encrypt for another one"
        )
        assert "nothing to do" in result.stdout

    def test_the_stable_path_still_serves_the_lineage(
        self, production: Path, tmp_path: Path
    ) -> None:
        _run_issue(production, tmp_path, certbot_exits=0)
        current = production / "arena64" / "current" / DOMAIN
        assert os.path.realpath(current) == os.path.realpath(production / "live" / f"{DOMAIN}-0001")
        for name in ("fullchain.pem", "privkey.pem", "chain.pem"):
            assert (current / name).is_file()

    def test_the_renewal_loop_renews_rather_than_reissues(
        self, production: Path, tmp_path: Path
    ) -> None:
        """The failure this whole change exists to prevent: `renew.sh`
        reading a valid certificate as 'no lineage' and buying a duplicate
        every five minutes."""
        transcript = _run_renew(production, tmp_path)

        assert "renewing from" in transcript, transcript
        assert "no certificate lineage yet" not in transcript
        assert "stub issue.sh invoked" not in transcript

    def test_recovery_finds_nothing_to_do_and_says_what_it_kept(
        self, production: Path, tmp_path: Path
    ) -> None:
        result = _run_recover(production, tmp_path)

        assert result.returncode == 0, result.stderr
        assert "nothing to do" in result.stdout
        assert f"leaving the genuine Certbot lineage {DOMAIN}-0001 untouched" in result.stdout
        assert (production / "live" / f"{DOMAIN}-0001" / "fullchain.pem").is_symlink()


class TestIssuanceFollowsTheLineageCertbotActuallyMade:
    def test_a_numbered_lineage_is_adopted_after_issuance(
        self, letsencrypt: Path, tmp_path: Path
    ) -> None:
        """`--cert-name arena64.gg` and Certbot stores `arena64.gg-0001`.

        The exact production sequence. Before A64-030.3 the script pointed
        the edge at a directory that did not exist.
        """
        result = _run_issue(
            letsencrypt,
            tmp_path,
            certbot_exits=0,
            creates_lineage=True,
            lineage_name=f"{DOMAIN}-0001",
        )

        assert result.returncode == 0, result.stderr
        current = letsencrypt / "arena64" / "current" / DOMAIN
        assert os.path.realpath(current) == os.path.realpath(
            letsencrypt / "live" / f"{DOMAIN}-0001"
        )
        assert "issuance complete" in result.stdout

    def test_a_malformed_host_is_not_charged_a_certificate(
        self, letsencrypt: Path, tmp_path: Path
    ) -> None:
        _legacy_state(letsencrypt)

        result = _run_issue(letsencrypt, tmp_path, certbot_exits=0, creates_lineage=True)

        assert result.returncode == 0, "nginx must still be released"
        assert "stub certbot invoked" not in result.stderr, (
            "issuance ran against a state nobody had resolved"
        )
        assert "NOT requesting a certificate" in result.stderr

    def test_an_ambiguous_host_is_not_charged_a_certificate(
        self, letsencrypt: Path, tmp_path: Path
    ) -> None:
        write_lineage(letsencrypt, DOMAIN)
        write_lineage(letsencrypt, f"{DOMAIN}-0001")

        result = _run_issue(letsencrypt, tmp_path, certbot_exits=0, creates_lineage=True)

        assert "stub certbot invoked" not in result.stderr
        assert "AMBIGUOUS" in result.stdout + result.stderr

    def test_holding_still_leaves_nginx_something_to_start_on(
        self, letsencrypt: Path, tmp_path: Path
    ) -> None:
        _legacy_state(letsencrypt)

        _run_issue(letsencrypt, tmp_path, certbot_exits=0)

        current = letsencrypt / "arena64" / "current" / DOMAIN
        for name in ("fullchain.pem", "privkey.pem", "chain.pem"):
            assert (current / name).is_file(), f"{name} unreadable while held"

    def test_holding_does_not_downgrade_a_working_edge(
        self, letsencrypt: Path, tmp_path: Path
    ) -> None:
        """A host serving a real certificate keeps serving it while an
        operator sorts out the wreckage under the sibling name."""
        live = write_lineage(letsencrypt, f"{DOMAIN}-0001")
        current = letsencrypt / "arena64" / "current"
        current.mkdir(parents=True)
        (current / DOMAIN).symlink_to(live)
        _legacy_state(letsencrypt)

        _run_issue(letsencrypt, tmp_path, certbot_exits=0)

        assert os.path.realpath(current / DOMAIN) == os.path.realpath(live), (
            "a held host replaced a real certificate with a self-signed stopgap"
        )


class TestTheRenewalLoopChoosesAMode:
    def test_no_lineage_means_first_issuance(self, letsencrypt: Path, tmp_path: Path) -> None:
        transcript = _run_renew(letsencrypt, tmp_path)

        assert "no certificate lineage yet" in transcript, transcript
        assert "stub issue.sh invoked" in transcript

    def test_a_valid_lineage_means_normal_renewal(self, letsencrypt: Path, tmp_path: Path) -> None:
        write_lineage(letsencrypt, DOMAIN)

        transcript = _run_renew(letsencrypt, tmp_path)

        assert "renewing from" in transcript, transcript
        assert "stub issue.sh invoked" not in transcript

    @pytest.mark.parametrize("shape", ["malformed", "ambiguous"])
    def test_a_state_nobody_can_read_holds_and_asks_for_nothing(
        self, letsencrypt: Path, tmp_path: Path, shape: str
    ) -> None:
        if shape == "malformed":
            _legacy_state(letsencrypt)
        else:
            write_lineage(letsencrypt, DOMAIN)
            write_lineage(letsencrypt, f"{DOMAIN}-0001")

        transcript = _run_renew(letsencrypt, tmp_path)

        assert "HELD" in transcript, transcript
        assert "stub issue.sh invoked" not in transcript, (
            "the loop would have bought a duplicate certificate every five minutes"
        )
        assert "stub certbot invoked" not in transcript


class TestRecoveryLeavesGenuineLineagesAlone:
    def test_it_quarantines_the_legacy_state_beside_a_numbered_lineage(
        self, letsencrypt: Path, tmp_path: Path
    ) -> None:
        """What production actually needed: clear the wreckage, keep the
        certificate that the accidental issuance produced."""
        write_lineage(letsencrypt, f"{DOMAIN}-0001")
        _legacy_state(letsencrypt)

        result = _run_recover(letsencrypt, tmp_path)

        assert result.returncode == 0, result.stderr
        assert not (letsencrypt / "live" / DOMAIN).exists()
        assert not (letsencrypt / "renewal" / f"{DOMAIN}.conf").exists()
        assert (letsencrypt / "live" / f"{DOMAIN}-0001" / "fullchain.pem").is_symlink()
        assert (letsencrypt / "renewal" / f"{DOMAIN}-0001.conf").is_file()

    def test_the_kept_lineage_is_reported(self, letsencrypt: Path, tmp_path: Path) -> None:
        write_lineage(letsencrypt, f"{DOMAIN}-0001")
        _legacy_state(letsencrypt)

        result = _run_recover(letsencrypt, tmp_path)

        assert f"leaving the genuine Certbot lineage {DOMAIN}-0001 untouched" in result.stdout

    def test_recovery_then_discovery_finds_the_numbered_lineage(
        self, letsencrypt: Path, tmp_path: Path
    ) -> None:
        """The end-to-end shape of the production fix, without a network."""
        write_lineage(letsencrypt, f"{DOMAIN}-0001")
        _legacy_state(letsencrypt)
        _run_recover(letsencrypt, tmp_path)

        status, path, _ = discover(letsencrypt, tmp_path)

        assert status == FOUND
        assert path == str(letsencrypt / "live" / f"{DOMAIN}-0001")

    def test_it_refuses_a_renewal_config_that_is_not_the_orphan(
        self, letsencrypt: Path, tmp_path: Path
    ) -> None:
        _legacy_state(letsencrypt, orphan=False)
        (letsencrypt / "renewal" / f"{DOMAIN}.conf").write_text("archive_dir = somewhere\n")

        result = _run_recover(letsencrypt, tmp_path)

        assert result.returncode != 0
        assert "is not empty" in result.stderr
        assert (letsencrypt / "live" / DOMAIN).exists(), "it moved something after refusing"

    def test_it_refuses_when_two_valid_lineages_exist(
        self, letsencrypt: Path, tmp_path: Path
    ) -> None:
        write_lineage(letsencrypt, f"{DOMAIN}-0001")
        write_lineage(letsencrypt, f"{DOMAIN}-0002")

        result = _run_recover(letsencrypt, tmp_path)

        assert result.returncode != 0
        assert "more than one valid Certbot lineage" in result.stderr

    def test_it_never_names_a_numbered_path(self) -> None:
        """A static reading of the two paths it moves.

        Both are built from `${ARENA64_DOMAIN}` alone, so no numbered
        lineage can be constructed — the guarantee is structural, not a
        check that could be edited out.
        """
        body = RECOVER_SH.read_text(encoding="utf-8")
        moved = [line.strip() for line in body.splitlines() if line.strip().startswith("mv ")]
        assert moved, "recovery no longer moves anything"
        for line in moved:
            assert "-000" not in line, f"recovery moves a numbered lineage: {line}"
            assert '"${LIVE}"' in line or '"${RENEWAL}"' in line, (
                f"recovery moves something other than the two legacy paths: {line}"
            )


DEPLOYMENT = _REPO / "docs" / "01-architecture" / "deployment.md"
RUNBOOKS = _REPO / "docs" / "01-architecture" / "runbooks.md"


def _shell_commands(document: Path) -> list[str]:
    """Every fenced shell command in a document, continuations joined."""
    blocks = re.findall(r"```(?:bash|sh|console)\n(.*?)```", document.read_text("utf-8"), re.S)
    commands: list[str] = []
    for block in blocks:
        joined = block.replace("\\\n", " ")
        commands.extend(line.strip() for line in joined.splitlines() if line.strip())
    return commands


class TestTheOperationalContract:
    """`--no-deps`, and why a documented command is part of the software.

    The second incident was not caused by any line of code in this
    repository. It was caused by a command in `deployment.md` that omitted
    `--no-deps`, so Compose honoured `certbot`'s `depends_on: certbot-init`
    and ran a real Let's Encrypt request before the recovery the operator
    had actually asked for. A runbook command is executable; it gets tested.
    """

    def test_the_hazard_these_tests_guard_still_exists(self) -> None:
        """If `certbot` ever stops depending on `certbot-init`, the rule
        below becomes cosmetic — and this test is where that is noticed."""
        services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
        assert "certbot-init" in services["certbot"]["depends_on"], (
            "certbot no longer depends on certbot-init; revisit the --no-deps rule"
        )

    @pytest.mark.parametrize("document", [DEPLOYMENT, RUNBOOKS], ids=lambda p: p.name)
    def test_every_one_shot_certbot_command_passes_no_deps(self, document: Path) -> None:
        for command in _shell_commands(document):
            if "docker compose" not in command or "certbot" not in command:
                continue
            if " run " not in command:
                continue
            assert "--no-deps" in command, (
                f"{document.name} documents a one-shot certbot command without --no-deps, "
                f"which starts certbot-init and asks Let's Encrypt for a certificate: {command}"
            )

    def test_the_recovery_script_documents_why(self) -> None:
        body = RECOVER_SH.read_text(encoding="utf-8")
        assert "--no-deps" in body
        assert "depends_on" in body, "the header states the rule without stating the hazard"

    def test_the_expiry_metric_reads_a_path_its_own_uid_can_open(self) -> None:
        """A gauge pointed at `live/<domain>` reads nothing on a host whose
        lineage is numbered — silently, which is the worst way for a
        certificate-expiry alert to fail. Pointing it at
        `arena64/current/<domain>` fixed the name and not the permission:
        that path is a symlink into Certbot's `archive/`, which is `0700
        root:root`, and the worker is uid 10001. Only the Arena64-owned
        public projection is readable by the process that publishes the
        metric — A64-030.4B.1."""
        services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
        path = services["worker"]["environment"]["OPS_CERTIFICATE_PATH"]
        assert path.startswith("/etc/letsencrypt/arena64/observability/"), path
        assert "privkey" not in path


def project_public_certificate(
    letsencrypt: Path, tmp_path: Path, *, domain: str = DOMAIN
) -> subprocess.CompletedProcess[str]:
    """Run the shipped `arena64_project_public_certificate` against a fake root."""
    lineage = _lineage_for(letsencrypt, tmp_path)
    probe = tmp_path / "project.sh"
    probe.write_text(f'. {lineage}\narena64_project_public_certificate "$ARENA64_DOMAIN"\n')
    return subprocess.run(
        ["sh", str(probe)],
        env=_sh_env(letsencrypt, ARENA64_DOMAIN=domain),
        capture_output=True,
        text=True,
        timeout=60,
    )


def projected_certificate(letsencrypt: Path, *, domain: str = DOMAIN) -> Path:
    return letsencrypt / "arena64" / "observability" / domain / "fullchain.pem"


class TestThePublicCertificateProjection:
    """The expiry metric could not read a certificate — A64-030.4B.1 (B-1).

    Certbot's `archive/` is `0700 root:root` because it holds private keys,
    and every path to a certificate resolves through it: `live/<name>/*.pem`
    and `arena64/current/<domain>/*.pem` are both symlinks into that
    directory. The worker runs as uid 10001, so
    `arena64_certificate_expiry_timestamp_seconds` was never published and
    `CertificateMissing`, `CertificateExpiringSoon` and `CertificateExpired`
    could not fire — three page-severity alerts, blind, on the one subsystem
    whose failure takes the whole site off the internet.

    The fix publishes the **public** half into Arena64's own namespace.
    These assert that it is published, that it is public, and that it is
    never replaced by something worse than what was already there.
    """

    def test_a_base_lineage_is_projected(self, letsencrypt: Path, tmp_path: Path) -> None:
        _run_issue(letsencrypt, tmp_path, certbot_exits=0, creates_lineage=True)

        projected = projected_certificate(letsencrypt)

        assert projected.is_file()
        assert "-----BEGIN CERTIFICATE-----" in projected.read_text()

    def test_a_numbered_lineage_is_projected(self, letsencrypt: Path, tmp_path: Path) -> None:
        """The shape production is actually in."""
        _run_issue(
            letsencrypt,
            tmp_path,
            certbot_exits=0,
            creates_lineage=True,
            lineage_name=f"{DOMAIN}-0001",
        )

        assert projected_certificate(letsencrypt).is_file()

    def test_it_is_byte_for_byte_what_nginx_serves(self, letsencrypt: Path, tmp_path: Path) -> None:
        """Sourced through `arena64/current/<domain>`, so the gauge and the
        edge cannot disagree about which certificate is live."""
        _run_issue(
            letsencrypt,
            tmp_path,
            certbot_exits=0,
            creates_lineage=True,
            lineage_name=f"{DOMAIN}-0001",
        )

        served = letsencrypt / "arena64" / "current" / DOMAIN / "fullchain.pem"

        assert projected_certificate(letsencrypt).read_bytes() == served.read_bytes()

    def test_the_stopgap_is_projected_too(self, letsencrypt: Path, tmp_path: Path) -> None:
        """A host serving a three-day self-signed certificate should have an
        expiry metric that says so, rather than no metric at all."""
        _run_issue(letsencrypt, tmp_path, certbot_exits=1)

        assert projected_certificate(letsencrypt).is_file()

    def test_it_carries_no_private_key(self, letsencrypt: Path, tmp_path: Path) -> None:
        """The whole security argument: the reader gets the certificate every
        client already receives in the handshake, and nothing else."""
        _run_issue(letsencrypt, tmp_path, certbot_exits=0, creates_lineage=True)

        observability = letsencrypt / "arena64" / "observability"
        published = sorted(p for p in observability.rglob("*") if p.is_file())

        assert [p.name for p in published] == ["fullchain.pem"]
        for path in published:
            assert "PRIVATE KEY" not in path.read_text()

    def test_it_is_world_readable(self, letsencrypt: Path, tmp_path: Path) -> None:
        """0444, and the directories traversable: a mode that only root can
        read is the defect this exists to fix, written a second time."""
        _run_issue(letsencrypt, tmp_path, certbot_exits=0, creates_lineage=True)

        projected = projected_certificate(letsencrypt)
        assert projected.stat().st_mode & 0o444 == 0o444
        assert projected.stat().st_mode & 0o200 == 0, "the projection is writable"
        for directory in (projected.parent, projected.parent.parent):
            assert directory.stat().st_mode & 0o005 == 0o005, f"{directory} is not traversable"

    def test_a_renewal_refreshes_it(self, letsencrypt: Path, tmp_path: Path) -> None:
        """A renewal replaces `fullchainN.pem` with `fullchainN+1.pem`;
        without this the gauge would report the date of the certificate the
        renewal just replaced."""
        _run_issue(letsencrypt, tmp_path, certbot_exits=0, creates_lineage=True)
        first = projected_certificate(letsencrypt).read_bytes()

        # Certbot renewing in place: same lineage, new key material.
        write_lineage(letsencrypt, DOMAIN)
        result = project_public_certificate(letsencrypt, tmp_path)

        assert result.returncode == 0, result.stderr
        assert projected_certificate(letsencrypt).read_bytes() != first

    def test_a_broken_source_leaves_the_last_known_good(
        self, letsencrypt: Path, tmp_path: Path
    ) -> None:
        """A stale certificate is a metric that is wrong about the date; a
        truncated one is a metric nobody can read, and the alert that matters
        most is the one for a certificate nobody can see."""
        _run_issue(letsencrypt, tmp_path, certbot_exits=0, creates_lineage=True)
        good = projected_certificate(letsencrypt).read_bytes()

        (letsencrypt / "arena64" / "current" / DOMAIN).unlink()
        result = project_public_certificate(letsencrypt, tmp_path)

        assert result.returncode != 0
        assert "left as it was" in result.stderr
        assert projected_certificate(letsencrypt).read_bytes() == good

    def test_it_refuses_to_publish_a_private_key(self, letsencrypt: Path, tmp_path: Path) -> None:
        """Belt and braces, for the shape that would otherwise slip past.

        A file holding *only* a key is caught by the certificate check
        above. The one this guard exists for is a combined PEM — a
        certificate with a key appended, which is what somebody pointing
        `current` at a bundle from another tool would produce — because that
        satisfies every check about being a certificate and would publish a
        private key world-readable.
        """
        _run_issue(letsencrypt, tmp_path, certbot_exits=0, creates_lineage=True)
        good = projected_certificate(letsencrypt).read_bytes()

        served = letsencrypt / "arena64" / "current" / DOMAIN / "fullchain.pem"
        target = Path(os.path.realpath(served))
        target.write_text(
            target.read_text() + "-----BEGIN PRIVATE KEY-----\nnope\n-----END PRIVATE KEY-----\n"
        )

        result = project_public_certificate(letsencrypt, tmp_path)

        assert result.returncode != 0
        assert "private key material" in result.stderr
        assert projected_certificate(letsencrypt).read_bytes() == good

    def test_it_leaves_no_partial_file_behind(self, letsencrypt: Path, tmp_path: Path) -> None:
        """Temporary name, then rename: a reader sees the previous
        certificate or the new one, never half of either."""
        _run_issue(letsencrypt, tmp_path, certbot_exits=0, creates_lineage=True)

        observability = letsencrypt / "arena64" / "observability"
        strays = [p.name for p in observability.rglob("*") if p.name.endswith(".next")]

        assert not strays, f"a temporary projection survived: {strays}"

    def test_a_missing_projection_does_not_buy_a_certificate(
        self, letsencrypt: Path, tmp_path: Path
    ) -> None:
        """Observability is never a reason to spend a rate-limit slot."""
        _run_issue(letsencrypt, tmp_path, certbot_exits=0, creates_lineage=True)
        projected_certificate(letsencrypt).unlink()

        again = _run_issue(letsencrypt, tmp_path, certbot_exits=1)

        assert "stub certbot invoked" not in again.stderr
        assert "nothing to do" in again.stdout

    def test_the_expiry_metric_reads_it(self, letsencrypt: Path, tmp_path: Path) -> None:
        """The end of the chain: the module that publishes
        `arena64_certificate_expiry_timestamp_seconds` reads the projection."""
        from app.operator import certificate_status

        _run_issue(
            letsencrypt,
            tmp_path,
            certbot_exits=0,
            creates_lineage=True,
            lineage_name=f"{DOMAIN}-0001",
        )

        remaining = certificate_status.days_remaining(projected_certificate(letsencrypt))

        assert remaining is not None, "the expiry metric would be absent, and CertificateMissing"
        assert remaining > 0


#: Builds the exact shape production is in, with Certbot's own permissions,
#: and asks the two questions that matter from the uid the worker runs as.
_PRODUCTION_SHAPE = r"""
set -eu
D=arena64.gg
LE=/etc/letsencrypt
mkdir -p "$LE/live" "$LE/archive" "$LE/renewal"

# The lineage Certbot actually created on the production host.
mkdir -p "$LE/archive/$D-0001" "$LE/live/$D-0001"
openssl req -x509 -newkey rsa:2048 -nodes -days 90 -subj "/CN=$D" \
  -addext "subjectAltName=DNS:$D,DNS:www.$D,DNS:admin.$D" \
  -keyout "$LE/archive/$D-0001/privkey1.pem" \
  -out "$LE/archive/$D-0001/fullchain1.pem" 2>/dev/null
cp "$LE/archive/$D-0001/fullchain1.pem" "$LE/archive/$D-0001/chain1.pem"
cp "$LE/archive/$D-0001/fullchain1.pem" "$LE/archive/$D-0001/cert1.pem"
for kind in fullchain privkey chain cert; do
  ln -sf "../../archive/$D-0001/${kind}1.pem" "$LE/live/$D-0001/${kind}.pem"
done
printf 'archive_dir = %s\n' "$LE/archive/$D-0001" > "$LE/renewal/$D-0001.conf"

# Certbot's permissions, which are the whole reason this test exists.
chmod 0700 "$LE/archive"
chmod 0600 "$LE/archive/$D-0001/privkey1.pem"
chmod 0644 "$LE/archive/$D-0001/fullchain1.pem"

. /usr/local/bin/lineage.sh
LINEAGE="$(arena64_discover_lineage "$D")" && STATUS=0 || STATUS=$?
echo "DISCOVERY=$(arena64_lineage_status_name "$STATUS")"
echo "LINEAGE=$LINEAGE"
arena64_point_current_at "$D" "$LINEAGE"
if arena64_project_public_certificate "$D"; then echo "PROJECT=ok"; else echo "PROJECT=failed"; fi

# The uid the worker runs as, asking for exactly what it needs and one
# thing it must never get.
adduser -D -u 10001 -H arena64 >/dev/null 2>&1 || true
P="$LE/arena64/observability/$D/fullchain.pem"
ask() {
  if su arena64 -s /bin/sh -c "$2" >/dev/null 2>&1
  then echo "$1=yes"
  else echo "$1=no"
  fi
}
ask WORKER_READS_PROJECTION "cat $P"
ask WORKER_READS_PRIVKEY "cat $LE/live/$D-0001/privkey.pem"
ask WORKER_READS_ARCHIVE_KEY "cat $LE/archive/$D-0001/privkey1.pem"
ask WORKER_TRAVERSES_ARCHIVE "ls $LE/archive"
ask WORKER_READS_SERVED_CERT "cat $LE/arena64/current/$D/fullchain.pem"

echo "LIVE_LINEAGES=$(ls "$LE/live" | tr '\n' ' ')"
if cmp -s "$P" "$LE/arena64/current/$D/fullchain.pem"; then
  echo "PROJECTION_IS_WHAT_NGINX_SERVES=yes"
else
  echo "PROJECTION_IS_WHAT_NGINX_SERVES=no"
fi
if grep -q 'PRIVATE KEY' "$P"
then echo "PROJECTION_HAS_KEY=yes"
else echo "PROJECTION_HAS_KEY=no"
fi
"""


@needs_docker
class TestTheProductionShapeUnderCertbotPermissions:
    """The whole of B-1, against Certbot's real permissions — A64-030.4B.1.

    Every other test in this file runs as one uid on a directory tree it
    owns, which is precisely the condition under which this defect is
    invisible: the projection and the private key are equally readable, so
    nothing distinguishes them. Here `archive/` is `0700` as Certbot makes
    it, the reader is uid 10001 as the worker is, and the questions are the
    two that were wrong in production — can it read the certificate, and can
    it read anything it should not.

    `--network none`: nothing here may reach an ACME server.
    """

    @pytest.fixture(scope="class")
    def facts(self) -> dict[str, str]:
        # `lineage.sh` is carried in the command rather than bind-mounted:
        # the Docker daemon resolves a mount source on the *host*, and this
        # test may itself be running inside a container where the repository
        # sits at a different path. The shipped file is still the one under
        # test — it is read from disk here and written unchanged in there.
        script = (
            "cat > /usr/local/bin/lineage.sh <<'ARENA64_LINEAGE_EOF'\n"
            + LINEAGE_SH.read_text(encoding="utf-8")
            + "\nARENA64_LINEAGE_EOF\n"
            + _PRODUCTION_SHAPE
        )
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--entrypoint",
                "sh",
                _certbot_image(),
                "-c",
                script,
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert result.returncode == 0, result.stderr
        return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)

    def test_discovery_finds_the_numbered_lineage(self, facts: dict[str, str]) -> None:
        assert facts["DISCOVERY"] == "FOUND"
        assert facts["LINEAGE"] == "/etc/letsencrypt/live/arena64.gg-0001"

    def test_the_projection_is_written(self, facts: dict[str, str]) -> None:
        assert facts["PROJECT"] == "ok"

    def test_the_worker_uid_can_read_the_projection(self, facts: dict[str, str]) -> None:
        """The defect, inverted: this was `no`, and three page-severity
        certificate alerts were blind because of it."""
        assert facts["WORKER_READS_PROJECTION"] == "yes"

    @pytest.mark.parametrize(
        "fact",
        ["WORKER_READS_PRIVKEY", "WORKER_READS_ARCHIVE_KEY", "WORKER_TRAVERSES_ARCHIVE"],
    )
    def test_the_worker_uid_still_cannot_reach_private_material(
        self, facts: dict[str, str], fact: str
    ) -> None:
        """Certbot's ownership model is untouched: the fix publishes a public
        copy, it does not open the private one."""
        assert facts[fact] == "no"

    def test_the_worker_uid_still_cannot_read_the_path_nginx_serves(
        self, facts: dict[str, str]
    ) -> None:
        """Which is why the projection has to exist at all."""
        assert facts["WORKER_READS_SERVED_CERT"] == "no"

    def test_the_projection_is_the_certificate_nginx_serves(self, facts: dict[str, str]) -> None:
        assert facts["PROJECTION_IS_WHAT_NGINX_SERVES"] == "yes"

    def test_the_projection_carries_no_private_key(self, facts: dict[str, str]) -> None:
        assert facts["PROJECTION_HAS_KEY"] == "no"

    def test_no_second_lineage_was_created(self, facts: dict[str, str]) -> None:
        """Observability must never be a reason to ask for a certificate."""
        assert facts["LIVE_LINEAGES"].split() == ["arena64.gg-0001"]
