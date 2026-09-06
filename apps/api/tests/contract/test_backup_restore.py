"""Backup and restore, against real PostgreSQL — A64-028.3, P0-4.

A64-028.1's finding was not "the backup script is weak"; it was that there
was no backup of anything, and that "a named volume is not a backup, and an
untested restore is not one either". So the test that matters here is not a
unit test of `app.operator.backup` — it is a **drill**: seed a disposable
database with data that has real relationships in it, back it up, restore it
into a second, empty database, and count.

## Why this creates its own databases

`contract_engine` gives one database that every other test shares, and a
restore is a whole-database operation. Both databases here are created and
dropped by the fixtures, named `a64_drill_*`, and neither is the developer's.

## Why it skips rather than fails without `pg_dump`

The client tools are a deployment concern (the API image installs them; a CI
runner may not). A missing binary is not a broken backup, and a suite that
went red on somebody's laptop for it would teach them to ignore it.
"""

import json
import logging
import os
import shutil
import subprocess
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.common.logging import _extras
from app.config.settings import get_settings
from app.modules.game.infrastructure.repositories.move_log_repository import (
    SqlAlchemyMoveLogRepository,
)
from app.operator import backup as backup_tool
from app.operator import backup_crypto
from tests.contract.conftest import _TEST_DSN

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        shutil.which("pg_dump") is None or shutil.which("pg_restore") is None,
        reason="needs the PostgreSQL client tools on PATH (see backup-restore.md)",
    ),
]

SOURCE_DB = "a64_drill_source"
TARGET_DB = "a64_drill_restored"

#: Deterministic **TEST DATA**, not production. Every count below is a
#: literal a test can assert, and the relationships are the ones a restore
#: has to preserve: a match that names two accounts, a rating adjustment that
#: names the match, a tournament registration that names the account.
PLAYERS = 4
MATCHES = 3
MOVES_PER_MATCH = 6
TOURNAMENTS = 2
REGISTRATIONS = 4
NOTIFICATIONS = 5
OUTBOX_ROWS = 3


def _admin_dsn() -> str:
    return _TEST_DSN.rsplit("/", 1)[0] + "/postgres"


def _dsn_for(database: str) -> str:
    return _TEST_DSN.rsplit("/", 1)[0] + "/" + database


def _libpq(database: str) -> str:
    return _dsn_for(database).replace("postgresql+asyncpg://", "postgresql://")


async def _recreate(database: str) -> None:
    engine = create_async_engine(_admin_dsn(), isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
            await connection.execute(text(f'CREATE DATABASE "{database}"'))
    finally:
        await engine.dispose()


async def _drop(database: str) -> None:
    engine = create_async_engine(_admin_dsn(), isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
    finally:
        await engine.dispose()


def _alembic(database: str, *args: str) -> None:
    environment = dict(os.environ, POSTGRES_DSN=_dsn_for(database))
    completed = subprocess.run(  # noqa: S603
        ["uv", "run", "alembic", *args],  # noqa: S607 — the project's own runner
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        cwd=Path(__file__).resolve().parents[2],
    )
    assert completed.returncode == 0, completed.stderr


@pytest_asyncio.fixture
async def source() -> AsyncIterator[AsyncEngine]:
    """A disposable database at `head`, holding the test corpus."""
    await _recreate(SOURCE_DB)
    _alembic(SOURCE_DB, "upgrade", "head")
    engine = create_async_engine(_dsn_for(SOURCE_DB))
    try:
        yield engine
    finally:
        await engine.dispose()
        await _drop(SOURCE_DB)


@pytest_asyncio.fixture
async def target() -> AsyncIterator[str]:
    """An empty database for the restore to write into."""
    await _recreate(TARGET_DB)
    try:
        yield TARGET_DB
    finally:
        await _drop(TARGET_DB)


@pytest.fixture
def destination(tmp_path: Path) -> Iterator[Path]:
    yield tmp_path / "backups"


@pytest.fixture
def pointed_at_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """`create` reads the configured database, so the drill points it at the
    disposable one — never at whatever the developer's `.env` names."""
    monkeypatch.setenv("POSTGRES_DSN", _dsn_for(SOURCE_DB))
    get_settings.cache_clear()


async def _seed(engine: AsyncEngine) -> dict[str, int]:
    """Writes the corpus and returns what it wrote, by table."""
    players = [uuid4() for _ in range(PLAYERS)]
    matches = [uuid4() for _ in range(MATCHES)]

    async with engine.begin() as connection:
        for index, player in enumerate(players):
            await connection.execute(
                text(
                    "INSERT INTO users.user (id, email, username, password_hash) "
                    "VALUES (:i, :e, :u, 'x')"
                ),
                {"i": player, "e": f"drill{index}@example.test", "u": f"drill{index}"},
            )
            await connection.execute(
                text(
                    "INSERT INTO rating.player_rating (player_id, variant, speed_class, "
                    "rating_value, rating_deviation, rating_volatility, games_played) VALUES "
                    "(:p, 'russian_8x8', 'blitz', 1500, 350, 0.06, 0)"
                ),
                {"p": player},
            )

        for index, match in enumerate(matches):
            light, dark = players[index % PLAYERS], players[(index + 1) % PLAYERS]
            await connection.execute(
                text(
                    # `ck_match__active_iff_both_accepted` is a real check
                    # constraint: an active match must have both seats
                    # accepted. Satisfying it here is the seed obeying the
                    # same invariants the application does — §19.
                    "INSERT INTO game.match (id, pairing_id, variant, rated, engine_version, "
                    "light_player_id, light_accepted_at, dark_player_id, dark_accepted_at, "
                    "status, settled_at, ply_number, created_at, acceptance_deadline) VALUES "
                    "(:i, :pair, 'russian_8x8', true, 2, :l, now(), :d, now(), 'active', "
                    "now(), :ply, now(), now() + interval '1 hour')"
                ),
                {"i": match, "pair": uuid4(), "l": light, "d": dark, "ply": MOVES_PER_MATCH},
            )
            for ply in range(1, MOVES_PER_MATCH + 1):
                await connection.execute(
                    text(
                        "INSERT INTO game.move (id, match_id, ply_number, seat, path, "
                        "position_hash, engine_version, created_at) VALUES "
                        "(:i, :m, :ply, :seat, :path, :hash, 2, now())"
                    ),
                    {
                        "i": uuid4(),
                        "m": match,
                        "ply": ply,
                        "seat": "light" if ply % 2 else "dark",
                        "path": ["c3", "d4"],
                        "hash": f"russian_8x8/ply-{ply}",
                    },
                )

        for index in range(TOURNAMENTS):
            tournament = uuid4()
            await connection.execute(
                text(
                    "INSERT INTO tournaments.tournament (id, name, format, variant, "
                    "speed_class, status, rated, capacity) VALUES (:i, :n, "
                    "'single_elimination', 'russian_8x8', 'blitz', 'registration_open', "
                    "true, 8)"
                ),
                {"i": tournament, "n": f"Drill cup {index}"},
            )
            for seat in range(REGISTRATIONS // TOURNAMENTS):
                await connection.execute(
                    text(
                        "INSERT INTO tournaments.registration (tournament_id, player_id, "
                        "status, registered_at) VALUES (:t, :p, 'registered', now())"
                    ),
                    {"t": tournament, "p": players[(index * 2 + seat) % PLAYERS]},
                )

        for index in range(NOTIFICATIONS):
            await connection.execute(
                text(
                    "INSERT INTO notifications.notification (id, recipient_id, type, category, "
                    "payload, target_type, source_event_id, created_at) VALUES "
                    "(:i, :r, 'game_finished', 'game', '{}'::jsonb, 'match', :s, now())"
                ),
                {"i": uuid4(), "r": players[index % PLAYERS], "s": uuid4()},
            )

        for index in range(OUTBOX_ROWS):
            await connection.execute(
                text(
                    "INSERT INTO platform.outbox (id, aggregate_type, aggregate_id, "
                    "event_type, event_version, payload) VALUES "
                    "(:i, 'match', :a, 'drill.event', 1, '{}'::jsonb)"
                ),
                {"i": uuid4(), "a": matches[index % MATCHES]},
            )

    return await _counts(engine)


COUNTED: tuple[str, ...] = (
    "users.user",
    "rating.player_rating",
    "game.match",
    "game.move",
    "tournaments.tournament",
    "tournaments.registration",
    "notifications.notification",
    "platform.outbox",
)


async def _counts(engine: AsyncEngine) -> dict[str, int]:
    async with engine.connect() as connection:
        return {
            table: (await connection.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
            for table in COUNTED
        }


async def _revision(engine: AsyncEngine) -> str | None:
    async with engine.connect() as connection:
        return (
            await connection.execute(text("SELECT version_num FROM public.alembic_version"))
        ).scalar_one_or_none()


# --- the drill ---------------------------------------------------------------


async def test_a_backup_restores_into_a_clean_database(
    source: AsyncEngine, target: str, destination: Path, pointed_at_source: None
) -> None:
    """§9. The whole point of A64-028.3, in one test."""
    expected = await _seed(source)
    assert expected["users.user"] == PLAYERS
    assert expected["game.move"] == MATCHES * MOVES_PER_MATCH
    source_revision = await _revision(source)

    dump = backup_tool.create(destination, keep=3)
    metadata = backup_tool.verify(dump)

    assert metadata["alembic_revision"] == source_revision
    assert metadata["bytes"] > 0
    assert metadata["format"] == "custom"

    backup_tool.restore(dump, target=_libpq(target), confirmed=True)

    restored = create_async_engine(_dsn_for(target))
    try:
        assert await _counts(restored) == expected
        assert await _revision(restored) == source_revision

        async with restored.connect() as connection:
            # Relationships, not just totals: a restore that dropped foreign
            # keys would count the same and mean nothing.
            orphan_moves = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM game.move m "
                        "LEFT JOIN game.match x ON x.id = m.match_id WHERE x.id IS NULL"
                    )
                )
            ).scalar_one()
            assert orphan_moves == 0

            seated = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM game.match m "
                        "JOIN users.user l ON l.id = m.light_player_id "
                        "JOIN users.user d ON d.id = m.dark_player_id"
                    )
                )
            ).scalar_one()
            assert seated == MATCHES

            constraints = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM pg_constraint c JOIN pg_namespace n "
                        "ON n.oid = c.connamespace WHERE c.contype = 'f' "
                        "AND n.nspname NOT IN ('pg_catalog','information_schema')"
                    )
                )
            ).scalar_one()
            assert constraints > 0, "the restore brought no foreign keys with it"
    finally:
        await restored.dispose()


async def test_the_restored_database_answers_an_application_read(
    source: AsyncEngine, target: str, destination: Path, pointed_at_source: None
) -> None:
    """§8 step 6. Counts prove rows arrived; this proves the schema is one
    the application's own repository can query — and it reads the move log
    on purpose.

    A64-016.4 made the durable log the source of truth for a game in
    progress: Redis holds a cache of a replay, and `_rebuild` replays the
    log. So "can the restored database serve `for_replay`" is the same
    question as "can a game be recovered from this backup".
    """
    await _seed(source)
    backup_tool.restore(backup_tool.create(destination), target=_libpq(target), confirmed=True)

    restored = create_async_engine(_dsn_for(target))
    try:
        async with restored.connect() as connection:
            match_id = (
                await connection.execute(text("SELECT id FROM game.match LIMIT 1"))
            ).scalar_one()
        async with AsyncSession(restored) as session:
            moves = await SqlAlchemyMoveLogRepository(session).for_replay(UUID(str(match_id)))

        assert len(moves) == MOVES_PER_MATCH
        assert [move.ply_number for move in moves] == list(range(1, MOVES_PER_MATCH + 1))
    finally:
        await restored.dispose()


async def test_the_metadata_beside_a_backup_carries_no_credential(
    source: AsyncEngine, destination: Path, pointed_at_source: None
) -> None:
    """A backup is a file somebody copies to another machine, and its
    sidecar goes with it — A64-028.3 §7, §44.

    Asserted against the metadata the tool **actually writes**, not against a
    list of field names kept beside it. The first version of this test
    compared one hardcoded set to another and passed happily while the tool
    wrote the DSN, which is the failure mode a test about secrets can least
    afford.
    """
    dump = backup_tool.create(destination)
    raw = backup_tool.metadata_path(dump).read_text()
    metadata = json.loads(raw)

    # The password out of the DSN this backup was taken with, verbatim.
    password = urlsplit(
        _dsn_for(SOURCE_DB).replace("postgresql+asyncpg://", "postgresql://")
    ).password
    if password:
        assert password not in raw

    lowered = raw.lower()
    for forbidden in ("postgresql://", "postgres://", "password", "secret", "token", "dsn"):
        assert forbidden not in lowered, f"{forbidden!r} reached a backup's metadata"

    # And what it *does* carry is what a restore needs to trust it.
    assert set(metadata) == {
        "alembic_revision",
        "bytes",
        "created_at",
        "database",
        "environment",
        "format",
        "pg_dump",
        "sha256",
    }


async def test_a_successful_backup_logs_no_credential(
    source: AsyncEngine,
    destination: Path,
    pointed_at_source: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """§44, and the gap a mutation found.

    `test_a_failure_never_logs_the_password` covers the path where something
    went wrong. This covers the one that runs every night — and it is the
    one that matters more, because a failure is looked at once and a success
    is logged for ever.
    """
    with caplog.at_level(logging.DEBUG):
        backup_tool.create(destination)

    # **The `extra` fields, not `caplog.text`.** This platform's formatter
    # emits `extra={...}` as JSON fields and the rendered message does not
    # contain them, so a check against `caplog.text` cannot see the place
    # this codebase actually puts values — which a mutation proved by
    # putting the DSN in one and passing.
    # `_extras` is the platform's own formatter logic — the fields it would
    # write as JSON, and nothing else. Checking `vars(record)` instead pulls
    # in `pathname`, which contains the repository path and so matches a
    # development password by coincidence.
    rendered = "\n".join(
        [record.message for record in caplog.records]
        + [f"{key}={value}" for record in caplog.records for key, value in _extras(record).items()]
    ).lower()

    # The *structure* of a credential, not the literal value: the
    # development password is the word `arena64`, which is also in every
    # backup's filename, so a literal check here would fail on its own
    # vocabulary. `tests/unit/test_backup_tool.py` owns the literal check,
    # with a password nothing else could produce.
    for forbidden in ("postgresql", "postgres://", "dsn", "password"):
        assert forbidden not in rendered, f"{forbidden!r} reached a log line"

    messages = [record.message for record in caplog.records]
    assert "backup_started" in messages
    assert "backup_completed" in messages


# --- Encrypted backups — A64-028.7, closing half of P2-8 ---------------------


KEY = backup_crypto.parse_key(backup_crypto.generate_key())


async def test_an_encrypted_backup_restores_into_a_clean_database(
    source: AsyncEngine, target: str, destination: Path, pointed_at_source: None
) -> None:
    """The drill again, through encryption. The same assertions, because
    encryption must change nothing an operator can observe except that the
    file on disk is unreadable without the key."""
    expected = await _seed(source)
    source_revision = await _revision(source)

    dump = backup_tool.create(destination, keep=3, key=KEY)
    metadata = backup_tool.verify(dump, key=KEY)

    assert metadata["encrypted"] is True
    assert metadata["alembic_revision"] == source_revision

    backup_tool.restore(dump, target=_libpq(target), confirmed=True, key=KEY)

    restored = create_async_engine(_dsn_for(target))
    try:
        assert await _counts(restored) == expected
        assert await _revision(restored) == source_revision
    finally:
        await restored.dispose()


async def test_the_archive_on_disk_is_not_readable_as_a_dump(
    source: AsyncEngine, destination: Path, pointed_at_source: None
) -> None:
    """The property P2-8 is about. `pg_restore` reading the file would mean
    the encryption is decoration."""
    await _seed(source)
    dump = backup_tool.create(destination, keep=3, key=KEY)

    listing = subprocess.run(  # noqa: S603
        ["pg_restore", "--list", str(dump)], capture_output=True, text=True, check=False
    )

    assert listing.returncode != 0
    assert b"PGDMP" not in dump.read_bytes()[:64]


async def test_a_plaintext_dump_never_touches_the_disk(
    source: AsyncEngine, destination: Path, pointed_at_source: None
) -> None:
    """`pg_dump` writes to a pipe, not a file.

    The obvious implementation — dump, encrypt, delete — leaves every email
    address and password hash on disk for the length of the encryption, and
    leaves them there for good if the process dies in between. Asserted by
    what is in the destination afterwards: the archive, its metadata, the
    status file, and nothing else.
    """
    await _seed(source)
    backup_tool.create(destination, keep=3, key=KEY)

    stray = [
        entry.name
        for entry in destination.iterdir()
        if entry.suffix not in {".dump", ".json"} or entry.name.endswith(".partial")
    ]

    assert stray == [], f"the destination holds files a backup should not leave: {stray}"


async def test_the_wrong_key_refuses_rather_than_restoring_rubbish(
    source: AsyncEngine, target: str, destination: Path, pointed_at_source: None
) -> None:
    """Authenticated encryption's whole point: a backup that restores into
    garbage is worse than one that refuses."""
    await _seed(source)
    dump = backup_tool.create(destination, keep=3, key=KEY)
    other = backup_crypto.parse_key(backup_crypto.generate_key())

    with pytest.raises(backup_crypto.BackupDecryptionError):
        backup_tool.restore(dump, target=_libpq(target), confirmed=True, key=other)


async def test_a_corrupted_archive_refuses(
    source: AsyncEngine, target: str, destination: Path, pointed_at_source: None
) -> None:
    """A single flipped bit. The checksum catches it first, which is the
    cheaper check and the one that runs before anything is decrypted."""
    await _seed(source)
    dump = backup_tool.create(destination, keep=3, key=KEY)
    raw = bytearray(dump.read_bytes())
    raw[-1] ^= 0x01
    dump.write_bytes(bytes(raw))

    with pytest.raises(backup_tool.BackupError, match="Checksum mismatch"):
        backup_tool.verify(dump, key=KEY)


async def test_an_encrypted_archive_without_a_key_says_so(
    source: AsyncEngine, destination: Path, pointed_at_source: None
) -> None:
    """Rather than handing ciphertext to `pg_restore` and reporting a
    corrupt archive, which is the same message a real corruption gives."""
    await _seed(source)
    dump = backup_tool.create(destination, keep=3, key=KEY)

    with pytest.raises(backup_tool.BackupError, match="is encrypted"):
        backup_tool.verify(dump)
