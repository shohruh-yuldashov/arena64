# Backup and Restore

> **Status:** current. Opened by A64-028.3, closing A64-028.1's **P0-4**.
> **Owner:** platform engineering.
> **Scope:** PostgreSQL. Redis and object storage are in
> [`data-reliability.md`](./data-reliability.md).

---

## 1. What this closes

A64-028.1: *"No `pg_dump`, backup script, cron, retention policy or restore
procedure exists anywhere… A named volume is not a backup, and an untested
restore is not one either."*

Everything durable Arena64 owns is in one PostgreSQL database — accounts,
the move log a game in progress rebuilds from, ratings, tournaments, the
append-only audit trail. There is one host and one volume.

---

## 2. The strategy, and why

| Option | Verdict |
| --- | --- |
| `pg_dump -Fc` (logical, custom format) | **Chosen** |
| Physical / base backup | Rejected for now — ties the backup to the server's major version and page layout, and buys nothing at this size |
| WAL archiving / PITR | **Deferred** — §7 |
| Provider snapshots | Not available: one host, one Docker volume |

The custom format is one compressed file whose table of contents
`pg_restore --list` can read **without a server**, which is what makes
`verify` a real check rather than a file-exists test.

---

## 3. Targets

**There is no product SLO for data loss.** Inventing one would be worse than
having none, so these are labelled for what they are.

| | |
| --- | --- |
| Official product SLO | **not defined** |
| Proposed backup RPO | **24 hours** — one scheduled backup a day. Everything between the last backup and a loss is gone |
| Proposed restore RTO | **under 1 hour** at present size, dominated by fetching the file from off-host storage rather than by `pg_restore` |

The RPO is a consequence of the PITR decision, not an independent choice: a
daily logical dump *is* a 24-hour RPO. Anything tighter needs §7.

---

## 4. Where a backup has to live

**A backup on the production host is not a backup.** One host means one
failure domain for the database, its volume and anything written beside it.

| | Local | Off-host |
| --- | --- | --- |
| What it protects against | A bad migration, a dropped table, an operator error | All of the above, plus losing the host, the disk or the provider account |
| Sufficient for production? | **No** | Yes |

`create --into <dir>` writes locally. Shipping that directory somewhere else
— an S3-compatible bucket in a different failure domain, an encrypted rsync
to different hardware — is a deployment decision and belongs to
**A64-028.6**, which owns where things run.

**The MinIO in `infrastructure/staging/compose.yml` is on the same host as
the database.** Writing backups there would be a copy, not a backup, and
this document exists partly so that nobody records it as one.

---

## 5. Taking a backup

```bash
python -m app.operator.backup create --into /var/backups/arena64 --keep 7
```

Reads `POSTGRES_DSN` from the environment, like everything else. Needs
`pg_dump` and `psql` on `PATH`; the API image carries the client because it
is already the image that runs migrations.

What it guarantees:

| Property | How |
| --- | --- |
| A partial backup never looks finished | `pg_dump` writes `<name>.dump.partial`; the rename to `<name>.dump` happens only after a zero exit *and* a recorded checksum |
| Failure is visible | Any failure raises and the command exits non-zero. `pg_dump` exiting zero having written nothing is also a failure |
| The password is never in `ps` | Passed to the subprocess in `PGPASSWORD`, never in `argv` |
| No secret is logged | Log lines carry the database name and the file name. Never the DSN |
| Backups do not accumulate | `--keep` prunes the oldest by timestamp, and refuses `--keep 0` |

### Metadata

A `<name>.dump.json` sits beside every backup, **and beside the off-host copy
too** — A64-030.4C.3. `verify` reads the checksum and the revision out of it
and `restore` calls `verify`, so a bucket holding only the ciphertext is a
file nobody can check and nothing that says which schema is in it. That is
what A64-030.4C's drill found, in the only way it can be found: by restoring
from off-host and watching the tooling refuse.

An off-host recovery therefore needs three things and nothing from the
machine that is gone:

```
<prefix>/<name>.dump        the encrypted archive
<prefix>/<name>.dump.json   this file
the encryption key          stored where neither of those is
```

Download the pair into one directory and the commands below work unchanged.

```json
{
  "created_at": "…Z", "environment": "production", "database": "arena64",
  "format": "custom", "alembic_revision": "c7a91d4e60b2",
  "sha256": "…", "bytes": 132379, "pg_dump": "pg_dump (PostgreSQL) 17.5"
}
```

The revision is the field that matters most: a dump restored against code
expecting a different schema is the failure a drill is meant to catch, and
only the revision says which schema is in the file.

**No DSN, no password, no token.** A test asserts the field names.

---

## 6. Restoring

```bash
# 1. a clean, empty database — never an existing one
createdb arena64_restored

# 2. restore, and mean it
python -m app.operator.backup restore /var/backups/arena64/<name>.dump \
    --target postgresql://user@host:5432/arena64_restored \
    --i-understand-this-overwrites

# 3. check what arrived
python -m app.operator.backup verify /var/backups/arena64/<name>.dump
```

Two locks against doing this by accident:

- **The target is always explicit.** There is deliberately no "restore into
  the configured database", because the configured database is production
  and a tool that can be aimed at it by forgetting an argument will be.
- **`--i-understand-this-overwrites` is required**, and the backup is
  verified *before* anything is written.

`pg_restore` runs `--single-transaction --exit-on-error`, so a restore
either completes or leaves the target as it found it.

### Restoring production

1. Stop the API. A half-restored database serving traffic is worse than an
   outage.
2. Restore into a **new** database, not over the live one.
3. Verify: counts, `alembic_version`, the integrity queries in
   [`data-reliability.md`](./data-reliability.md) §8.
4. Point `POSTGRES_DSN` at the restored database and start the API.
5. Keep the damaged database until somebody has looked at it.

---

## 7. Point-in-time recovery — deferred, with the risk stated

**Does Arena64 need to recover to a moment between backups?** Today: no.
Nobody has launched, there is no money in the system, and the loss of a
day's play is recoverable in the sense that matters — accounts, ratings and
history older than the last backup survive.

**What deferring costs:** up to 24 hours of everything. Games played,
ratings earned, accounts registered, tournaments run. That is the RPO in §3
and it is the whole of the residual risk.

**What implementing it needs**, when the answer changes:

- `archive_mode = on` and an `archive_command` that ships WAL segments
  off-host — a *server* configuration this repository does not own;
- a base backup, taken and refreshed on a schedule;
- somewhere durable, off-host, with retention;
- a restore drill that recovers to a chosen timestamp, not just to a dump.

All four are deployment decisions. **A64-028.6** owns them. This document is
the interface: the day PITR exists, §3's RPO changes and §6 gains a
recovery-target procedure.

**PITR is not implemented and this document does not claim it is.**

---

## 8. Scheduling

**Not in the API process.** A backup inside the application would run once
per replica, compete with request handling for the event loop, and stop
when the process does.

It is a command, and something outside runs it: a cron entry, a systemd
timer, or the deployment's scheduler. In production that is the `backup`
service in `infrastructure/production/compose.yml` — a container whose whole
job is a sleep loop around this command, so it keeps running when the
application does not.

**Running one by hand, against the production tier:**

```bash
docker compose --env-file production.env run --rm --entrypoint python backup \
    -m app.operator.backup create --into /var/backups/arena64
```

`--entrypoint python` is required, because `docker compose run` replaces the
command and that service replaces the entrypoint. Without it the daily loop
runs in the foreground and the arguments are read by nothing — A64-030.2
(B-1b).

**A failure is a log line, never silence.** The loop reports a non-zero exit
and retries at the next interval rather than discarding the status; it used
to end in `|| true`, and it was also calling a flag the CLI does not define,
so it had taken no backup at all and said nothing about it (A64-030.2, B-1).
Nothing about a failure moves `arena64_backup_last_success_timestamp_seconds`,
so `BackupStale` and `BackupNeverSucceeded` remain the authority on whether
a backup exists.

---

## 9. The drill

`tests/contract/test_backup_restore.py`, against real PostgreSQL. It seeds a
disposable database with **TEST DATA** whose rows reference each other,
backs it up, restores into a second empty database, and counts.

```
SOURCE TEST DB (TEST DATA, not production)
  users.user                        4     game.move                    18
  rating.player_rating              4     tournaments.tournament        2
  game.match                        3     tournaments.registration      4
  notifications.notification        5     platform.outbox               3
  alembic revision  c7a91d4e60b2

BACKUP
  arena64-local-20260905T171108Z.dump   verified
  132379 bytes · 298 objects · sha256 4684cd21db9ec22f…
  revision c7a91d4e60b2 · pg_dump (PostgreSQL) 17.5

RESTORED CLEAN DB
  every count above, identical           alembic revision  c7a91d4e60b2  OK
  orphan moves                        0  OK
  matches with both seats resolved    3  OK
  foreign keys 12 · check constraints 98 · unique constraints 7
  application read (SqlAlchemyMoveLogRepository.for_replay)  PASS
```

The last line is the one that matters most: `for_replay` is what
`LiveMoveService._rebuild` calls, so "the restored database can serve it"
and "a game can be recovered from this backup" are the same statement.

`tests/unit/test_backup_tool.py` covers the other half — an unreachable
database, `pg_dump` exiting zero with no file, a missing client, a checksum
mismatch, a dump with no metadata, an archive `pg_restore` cannot read,
retention keeping the wrong generation, `--keep 0`, restore without
confirmation, and the password appearing in a log or in `argv`.

---

## 10. Security

| | |
| --- | --- |
| Database password | `PGPASSWORD` to the subprocess. Never `argv`, never a log line, never metadata |
| Backup credentials | Environment, like every other secret. Nothing in this repository |
| Encryption at rest | **Implemented — A64-028.7, closing P2-8.** `app/operator/backup_crypto.py` seals the archive as `pg_dump` streams it, so a plaintext dump never touches a disk. The key is `OPS_BACKUP_ENCRYPTION_KEY` and a production-like tier refuses to start without it (`Settings._guard_production_backup`). It is **not** stored with the archive and must live in a secret manager off this host: losing it loses every backup taken with it |
| Off-host copy | **Implemented — A64-028.7, completed A64-030.4C.3.** `app/operator/backup_offsite.py` uploads over the S3 REST API with SigV4, to any S3-compatible provider named by `OPS_BACKUP_OFFSITE_ENDPOINT`. All four `OPS_BACKUP_OFFSITE_*` values are required together; a half-configured target is refused rather than half-used. **The archive and its manifest are one unit**: both are uploaded, the manifest second so an interrupted copy leaves a dump the restore path refuses rather than a manifest promising a dump that is not there, and a manifest that does not arrive is recorded as an off-host *failure* — the local archive is still there, so it is a partial success reported as one |
| Test data | The drill's corpus is generated. **No production data is in this repository** |
| Backup artefacts | Never world-readable. `0600`, on a path only the operator account can read |

---

## Related Documents

- [`data-reliability.md`](./data-reliability.md) — Redis, retention, the failure matrix and the runbook
- [`../01-architecture/production-hardening.md`](../01-architecture/production-hardening.md) — the risk register this closes P0-4 in
- [`../01-architecture/deployment.md`](../01-architecture/deployment.md) — where things run
