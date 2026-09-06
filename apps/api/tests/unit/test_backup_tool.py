"""What the backup command does when things go wrong — A64-028.3 §10, §11.

The drill in `tests/contract/test_backup_restore.py` proves a backup can be
restored. These prove the other half, which is the half that actually bites:
a backup that failed and said nothing is worse than no backup at all,
because somebody stops worrying about it.
"""

import hashlib
import json
import logging
import shutil
import subprocess
from pathlib import Path

import pytest

from app.common.logging import _extras
from app.config.settings import get_settings
from app.operator import backup as tool
from app.operator import backup_offsite, backup_status

UNREACHABLE = "postgresql+asyncpg://arena64:hunter2@127.0.0.1:1/arena64"

#: Two tests below need a real `pg_dump` to fail *for the right reason*.
#: Everything else here runs anywhere, which is the point of testing the
#: wrapper rather than the tool.
needs_client = pytest.mark.skipif(
    shutil.which("pg_dump") is None, reason="needs the PostgreSQL client tools on PATH"
)


@pytest.fixture
def destination(tmp_path: Path) -> Path:
    return tmp_path / "backups"


@pytest.fixture
def unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_DSN", UNREACHABLE)
    get_settings.cache_clear()


class TestABackupThatFailsSaysSo:
    @needs_client
    def test_an_unreachable_database_raises(self, unreachable: None, destination: Path) -> None:
        # Port 1: nothing listens there, on any machine.
        with pytest.raises(tool.BackupError, match="pg_dump failed"):
            tool.create(destination)

    @needs_client
    def test_a_failed_backup_leaves_nothing_that_looks_finished(
        self, unreachable: None, destination: Path
    ) -> None:
        # The property the whole design turns on: a restore run tomorrow
        # must not find this.
        with pytest.raises(tool.BackupError):
            tool.create(destination)

        assert list(destination.glob("*.dump")) == []
        # The metadata sidecar specifically — `metadata_path` writes
        # `<name>.dump.json`, and `verify` refuses a dump without one. A
        # sidecar with no dump beside it is the shape that would let a
        # partial backup pass for a finished one.
        #
        # Not every `*.json`: A64-028.6 §20 added `arena64-backup-status.json`
        # to the same directory, and it is written *because* this attempt
        # failed. Excluding it is not a loosening — the assertion below is
        # stronger than the glob it replaces, because "no status file" and
        # "a status file that says the backup failed" are the same to a
        # blanket glob and opposite to an operator.
        assert list(destination.glob("*.dump.json")) == []

        status = backup_status.read(destination)
        assert status.last_outcome == "failed"
        assert status.failed_at is not None
        assert status.succeeded_at is None

    def test_pg_dump_exiting_zero_with_no_file_is_still_a_failure(
        self, monkeypatch: pytest.MonkeyPatch, destination: Path
    ) -> None:
        # A disk that filled at exactly the wrong moment, or a `pg_dump`
        # wrapper that swallowed an error. Exit code alone is not proof.
        monkeypatch.setattr(tool, "_run", lambda *a, **k: None)
        monkeypatch.setattr(tool, "_require", lambda name: f"/usr/bin/{name}")

        with pytest.raises(tool.BackupError, match="wrote nothing"):
            tool.create(destination)

    def test_a_missing_client_names_the_tool_rather_than_crashing(
        self, monkeypatch: pytest.MonkeyPatch, destination: Path
    ) -> None:
        monkeypatch.setattr("app.operator.backup.shutil.which", lambda _: None)

        with pytest.raises(tool.BackupError, match="pg_dump is not on PATH"):
            tool.create(destination)


class TestVerifyRefusesWhatIsNotABackup:
    def _planted(self, destination: Path, *, content: bytes, checksum: str) -> Path:
        destination.mkdir(parents=True, exist_ok=True)
        dump = destination / "arena64-test-20260101T000000Z.dump"
        dump.write_bytes(content)
        tool.metadata_path(dump).write_text(
            json.dumps({"sha256": checksum, "bytes": len(content), "format": "custom"})
        )
        return dump

    def test_a_corrupted_file_fails_its_checksum(self, destination: Path) -> None:
        # Silent corruption on the way to or from off-host storage is the
        # case this exists for, and it is invisible without the digest.
        dump = self._planted(destination, content=b"not the bytes", checksum="0" * 64)

        with pytest.raises(tool.BackupError, match="Checksum mismatch"):
            tool.verify(dump)

    def test_a_dump_without_metadata_is_refused(self, destination: Path) -> None:
        destination.mkdir(parents=True)
        orphan = destination / "arena64-test-20260101T000000Z.dump"
        orphan.write_bytes(b"x")

        with pytest.raises(tool.BackupError, match="No metadata"):
            tool.verify(orphan)

    def test_a_missing_file_is_refused(self, destination: Path) -> None:
        with pytest.raises(tool.BackupError, match="No such backup"):
            tool.verify(destination / "nothing.dump")

    def test_a_file_pg_restore_cannot_read_is_refused(
        self, destination: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Right checksum, wrong contents: a truncated transfer that happened
        # to be recorded. Only `pg_restore --list` catches this.
        content = b"this is not a pg_dump archive"
        dump = self._planted(
            destination, content=content, checksum=hashlib.sha256(content).hexdigest()
        )
        monkeypatch.setattr(tool, "_require", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(
            "app.operator.backup.subprocess.run",
            lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", "not an archive"),
        )

        with pytest.raises(tool.BackupError, match="could not read"):
            tool.verify(dump)


class TestRetention:
    def _plant(self, destination: Path, stamps: list[str]) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        for stamp in stamps:
            dump = destination / f"arena64-local-{stamp}.dump"
            dump.write_bytes(b"x")
            tool.metadata_path(dump).write_text("{}")

    def test_it_keeps_the_newest_and_removes_the_rest(self, destination: Path) -> None:
        self._plant(
            destination,
            ["20260101T000000Z", "20260102T000000Z", "20260103T000000Z", "20260104T000000Z"],
        )

        removed = tool.prune(destination, keep=2)

        assert [path.name for path in removed] == [
            "arena64-local-20260101T000000Z.dump",
            "arena64-local-20260102T000000Z.dump",
        ]
        assert sorted(path.name for path in destination.glob("*.dump")) == [
            "arena64-local-20260103T000000Z.dump",
            "arena64-local-20260104T000000Z.dump",
        ]
        # And the metadata goes with its dump rather than accumulating.
        assert len(list(destination.glob("*.json"))) == 2

    def test_it_refuses_to_keep_nothing(self, destination: Path) -> None:
        # `--keep 0` is a typo, not an instruction to delete every backup.
        self._plant(destination, ["20260101T000000Z"])

        with pytest.raises(tool.BackupError, match="at least 1"):
            tool.prune(destination, keep=0)

        assert len(list(destination.glob("*.dump"))) == 1


class TestRestoreIsHardToDoByAccident:
    def test_it_refuses_without_the_confirmation_flag(self, destination: Path) -> None:
        with pytest.raises(tool.BackupError, match="--i-understand-this-overwrites"):
            tool.restore(destination / "any.dump", target=UNREACHABLE, confirmed=False)

    def test_it_verifies_before_it_writes_anything(self, destination: Path) -> None:
        # A corrupt backup must fail at the check, not halfway through
        # writing over whatever the target held.
        with pytest.raises(tool.BackupError, match="No such backup"):
            tool.restore(destination / "missing.dump", target=UNREACHABLE, confirmed=True)


class TestTheCredentialStaysOutOfSight:
    def test_the_password_travels_in_the_environment_and_not_in_argv(self) -> None:
        # `ps` is readable by every account on the host.
        arguments, environment, database = tool._libpq(
            "postgresql+asyncpg://arena64:s3cr3t@db.internal:5432/arena64"
        )

        assert "s3cr3t" not in " ".join(arguments)
        assert environment["PGPASSWORD"] == "s3cr3t"
        assert database == "arena64"
        assert "--host" in arguments and "db.internal" in arguments

    def test_a_failure_never_logs_the_password(
        self, unreachable: None, destination: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG), pytest.raises(tool.BackupError) as raised:
            tool.create(destination)

        # Including every `extra` field, which is where this codebase puts
        # its values and which `caplog.text` does not render.
        rendered = (
            " ".join(record.message for record in caplog.records)
            + str(raised.value)
            + "".join(
                f"{key}={value}"
                for record in caplog.records
                for key, value in _extras(record).items()
            )
        ).lower()

        assert "hunter2" not in rendered
        # The DSN's *scheme*, not the product's name: "PostgreSQL client
        # tools" is a legitimate sentence in a legitimate error, and a check
        # for the bare word failed on a machine without `pg_dump` — which is
        # the machine most likely to hit that error.
        for scheme in ("postgresql://", "postgresql+", "postgres://"):
            assert scheme not in rendered


class _Remote:
    """A stand-in for the off-host store, holding what actually arrived.

    Small enough to read, and it records the two things the failure
    semantics turn on: which keys landed, and in what order.
    """

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.objects: dict[str, bytes] = {}
        self.checksums: dict[str, str] = {}
        self.order: list[str] = []
        self._fail_on = fail_on

    def upload(self, archive: Path, *, target: object, sha256: str, **_: object) -> str:
        key = f"production/{archive.name}"
        if self._fail_on and archive.name.endswith(self._fail_on):
            raise backup_offsite.OffsiteUploadError(f"refused {key}")
        self.objects[key] = archive.read_bytes()
        self.checksums[key] = sha256
        self.order.append(key)
        return key


def _offsite_backup(destination: Path, monkeypatch: pytest.MonkeyPatch, remote: _Remote) -> Path:
    """Drive `create` with a stub `pg_dump` and a stub off-host store.

    The dump is bytes rather than a real archive: every assertion here is
    about which objects leave the host and how a partial copy is recorded,
    and none of them is about PostgreSQL's file format.
    """
    destination.mkdir(parents=True, exist_ok=True)

    def fake_run(command: list[str], environment: dict[str, str], *, what: str) -> None:
        target = Path(command[command.index("--file") + 1])
        target.write_bytes(b"ARENA64\x01not-a-real-archive-but-not-plaintext-either")

    monkeypatch.setattr(tool, "_run", fake_run)
    monkeypatch.setattr(tool, "_require", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(tool, "_tool_version", lambda _: "pg_dump (PostgreSQL) 17.11")
    monkeypatch.setattr(tool, "_alembic_head", lambda *a, **k: "c7a91d4e60b2")
    monkeypatch.setattr(backup_offsite, "upload", remote.upload)
    target = backup_offsite.OffsiteTarget(
        endpoint="https://example.invalid",
        bucket="arena64-production-backups",
        region="auto",
        access_key_id="unused-in-this-stub",
        secret_access_key="unused-in-this-stub",
        prefix="production",
    )
    return tool.create(destination=destination, offsite=target)


class TestTheOffHostCopyIsSelfContained:
    """A64-030.4C's drill restored from R2 and the tooling refused.

    `verify` reads the checksum and the revision out of `<name>.dump.json`,
    and `restore` calls `verify` — so the encrypted dump alone is a file
    nobody can check and nothing says which schema is in it. The manifest was
    staying on the host that the off-host copy exists to survive, which is
    the one machine a disaster removes.
    """

    def test_both_objects_are_uploaded(
        self, destination: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        remote = _Remote()

        dump = _offsite_backup(destination, monkeypatch, remote)

        assert sorted(remote.objects) == sorted(
            [f"production/{dump.name}", f"production/{dump.name}.json"]
        )

    def test_the_manifest_arrives_after_the_dump(
        self, destination: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An interrupted copy must leave a dump with no manifest — which the
        restore path refuses — rather than a manifest promising a dump that
        is not there."""
        remote = _Remote()

        dump = _offsite_backup(destination, monkeypatch, remote)

        assert remote.order == [f"production/{dump.name}", f"production/{dump.name}.json"]

    def test_the_manifest_describes_the_dump_that_was_uploaded(
        self, destination: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        remote = _Remote()

        dump = _offsite_backup(destination, monkeypatch, remote)

        stored_dump = remote.objects[f"production/{dump.name}"]
        manifest = json.loads(remote.objects[f"production/{dump.name}.json"])
        assert manifest["sha256"] == hashlib.sha256(stored_dump).hexdigest()
        assert manifest["bytes"] == len(stored_dump)
        assert manifest["alembic_revision"] == "c7a91d4e60b2"
        # The checksum the store was given for each object is the object's
        # own, not the other one's: SigV4 signs the payload hash.
        assert remote.checksums[f"production/{dump.name}"] == manifest["sha256"]
        assert (
            remote.checksums[f"production/{dump.name}.json"]
            == hashlib.sha256(remote.objects[f"production/{dump.name}.json"]).hexdigest()
        )

    def test_the_manifest_carries_nothing_secret(
        self, destination: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """It is now a second object in a bucket, so what it holds matters
        more than it did when it never left the host."""
        remote = _Remote()

        dump = _offsite_backup(destination, monkeypatch, remote)

        body = remote.objects[f"production/{dump.name}.json"].decode()
        manifest = json.loads(body)
        assert set(manifest) == {
            "created_at",
            "environment",
            "database",
            "format",
            "alembic_revision",
            "sha256",
            "bytes",
            "pg_dump",
            "encrypted",
        }
        for forbidden in ("password", "secret", "token", "key", "dsn", "postgresql://"):
            assert forbidden not in body.lower(), f"the manifest carries a {forbidden}"

    def test_a_manifest_that_does_not_arrive_is_not_a_copy(
        self, destination: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pair is one recoverable unit. A dump that reached the store
        without its manifest is not an off-host backup, and recording it as
        one is how an operator stops worrying about a backup they cannot
        restore."""
        remote = _Remote(fail_on=".json")

        with pytest.raises(backup_offsite.OffsiteUploadError):
            _offsite_backup(destination, monkeypatch, remote)

        status = json.loads((destination / "arena64-backup-status.json").read_text())
        assert status["offsite_outcome"] == "failed"
        assert "offsite_failed_at" in status
        # No off-host copy is claimed: `offsite_at` is what
        # `arena64_backup_last_offsite_timestamp_seconds` reads, and a dump
        # that arrived without its manifest must not move it.
        assert status.get("offsite_at") is None
        assert status.get("offsite_key") is None
        # The local archive survives: this is a partial success reported as a
        # failure, not a loss.
        assert list(destination.glob("*.dump"))

    def test_the_local_backup_is_still_recorded_as_successful(
        self, destination: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed copy must not make the backup itself look like it failed;
        `BackupStale` and `BackupNotCopiedOffHost` are separate alerts because
        they are separate facts."""
        remote = _Remote(fail_on=".json")

        with pytest.raises(backup_offsite.OffsiteUploadError):
            _offsite_backup(destination, monkeypatch, remote)

        status = json.loads((destination / "arena64-backup-status.json").read_text())
        assert status["last_outcome"] == "succeeded"
        assert status["succeeded_at"] is not None

    def test_a_disaster_restore_needs_only_what_is_off_host(
        self, destination: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The whole point, and the assertion the drill could not make.

        The host is gone: this rebuilds a recovery directory from the two
        stored objects alone and asks the shipped `verify` to accept it. No
        file from `destination` is copied, and the test deletes it first so
        that it cannot be.
        """
        remote = _Remote()
        dump = _offsite_backup(destination, monkeypatch, remote)
        stored = {name: body for name, body in remote.objects.items()}

        # Total host loss.
        shutil.rmtree(destination)
        assert not destination.exists()

        recovery = tmp_path / "recovery"
        recovery.mkdir()
        for key, body in stored.items():
            (recovery / key.split("/")[-1]).write_bytes(body)
        assert sorted(p.name for p in recovery.iterdir()) == sorted(
            [dump.name, f"{dump.name}.json"]
        )

        # `verify` shells out to `pg_restore --list`; the archive here is a
        # few bytes rather than a real one, so the listing is stubbed. What
        # is *not* stubbed is everything this test is about: finding the
        # manifest, reading the revision out of it, and checking the
        # download against the checksum it records.
        monkeypatch.setattr(
            "app.operator.backup.subprocess.run",
            lambda *a, **k: subprocess.CompletedProcess(
                args=a[0] if a else [], returncode=0, stdout="1; 2 TABLE public one\n", stderr=""
            ),
        )
        metadata = tool.verify(recovery / dump.name, key=None)

        assert metadata["alembic_revision"] == "c7a91d4e60b2"
        assert metadata["sha256"] == hashlib.sha256((recovery / dump.name).read_bytes()).hexdigest()
