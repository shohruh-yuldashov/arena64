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
"""

import json
import os
import shutil
import subprocess
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


def _run_issue(
    letsencrypt: Path, tmp_path: Path, *, certbot_exits: int, creates_lineage: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run the real `issue.sh` with a stub `certbot` and a fake root.

    The stub stands in for the ACME client: it can succeed while creating a
    proper Certbot lineage, succeed without creating one — the shape that
    would have hidden the incident — or fail outright.
    """
    binaries = tmp_path / "bin"
    binaries.mkdir(exist_ok=True)

    lineage_script = ""
    if creates_lineage:
        # Certbot's real layout: archive holds the files, live holds symlinks.
        lineage_script = f"""
mkdir -p {letsencrypt}/archive/{DOMAIN} {letsencrypt}/live/{DOMAIN}
for kind in fullchain privkey chain cert; do
  echo "real-$kind" > {letsencrypt}/archive/{DOMAIN}/${{kind}}1.pem
  ln -sf ../../archive/{DOMAIN}/${{kind}}1.pem {letsencrypt}/live/{DOMAIN}/${{kind}}.pem
done
echo "renewed" > {letsencrypt}/renewal/{DOMAIN}.conf
"""
    stub = binaries / "certbot"
    stub.write_text(
        f"#!/bin/sh\necho 'stub certbot invoked' >&2\n{lineage_script}\nexit {certbot_exits}\n"
    )
    stub.chmod(0o755)

    # The scripts hard-code /etc/letsencrypt; rewrite to the temporary root.
    runnable = tmp_path / "issue.sh"
    runnable.write_text(
        ISSUE_SH.read_text(encoding="utf-8").replace("/etc/letsencrypt", str(letsencrypt))
    )
    lineage = tmp_path / "lineage.sh"
    lineage.write_text(
        LINEAGE_SH.read_text(encoding="utf-8").replace("/etc/letsencrypt", str(letsencrypt))
    )
    runnable.write_text(runnable.read_text().replace("/usr/local/bin/lineage.sh", str(lineage)))

    openssl = shutil.which("openssl")
    assert openssl is not None, "openssl is needed to write the stopgap"
    return subprocess.run(
        ["sh", str(runnable)],
        env={
            "PATH": f"{binaries}:{Path(openssl).parent}:/usr/bin:/bin",
            "ARENA64_DOMAIN": DOMAIN,
            "ARENA64_ACME_EMAIL": f"ops@{DOMAIN}",
        },
        capture_output=True,
        text=True,
        timeout=90,
    )


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
        assert (current / "fullchain.pem").read_text().strip() == "real-fullchain"

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
        assert "no lineage appeared" in result.stderr


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
    lineage = tmp_path / "lineage.sh"
    lineage.write_text(
        LINEAGE_SH.read_text(encoding="utf-8").replace("/etc/letsencrypt", str(letsencrypt))
    )
    runnable = tmp_path / "recover.sh"
    body = RECOVER_SH.read_text(encoding="utf-8").replace("/etc/letsencrypt", str(letsencrypt))
    runnable.write_text(body.replace("/usr/local/bin/lineage.sh", str(lineage)))
    return subprocess.run(
        ["sh", str(runnable)],
        env={"PATH": "/usr/bin:/bin", "ARENA64_DOMAIN": DOMAIN},
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
