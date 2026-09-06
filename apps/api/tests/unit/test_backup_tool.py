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
from app.operator import backup_status

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
