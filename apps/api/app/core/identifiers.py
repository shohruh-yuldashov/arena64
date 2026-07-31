"""UUIDv7 generation — database.md DB-07: "Application-generated UUIDv7
identifiers", generated in Python rather than by a database default.

**Why generated here, in application code, rather than by PostgreSQL:**
the service must know an identifier *before* it commits — `game.SubmitMove`
writes the outbox payload containing the match and move identifiers in the
same transaction as the state change (AD-16), and a database-generated
default would need a round trip to discover the id, inside a latency
budget that does not have one to spare.

**Why hand-rolled rather than `uuid.uuid7()`:** that function exists in
the stdlib only from Python 3.14 (`https://docs.python.org/3/library/uuid.html`),
and this project targets 3.13 (`pyproject.toml`'s `requires-python`) —
using it would work on whichever interpreter happens to be installed
wherever this is run, and silently fail to import on the version the
project actually promises to support. database.md DB-07 anticipated
exactly this gap and called the fix "nine lines of Python"; this is that
function. When the project's minimum Python version is raised to 3.14,
`generate_uuid7` becomes a one-line wrapper around the stdlib
implementation, and every caller is unaffected.

**Why time-ordered matters, concretely:** a random (v4) key inserts at a
random point in a B-tree index. On `game.move` at ~5,000 inserts/second
(system-design.md §10) that means every insert dirties a different leaf
page, the working set never fits in cache, and WAL volume inflates with
full-page writes. A v7 key is time-ordered, so inserts land at the growing
edge of the index — the access pattern a sequence gives, without a
sequence's coordination.
"""

import os
import time
import uuid

_VERSION_7 = 0x7
_VARIANT_RFC_9562 = 0b10


def generate_uuid7() -> uuid.UUID:
    """RFC 9562 UUID version 7: a 48-bit big-endian Unix millisecond
    timestamp, a 4-bit version, 74 random bits, and a 2-bit variant —
    time-ordered at millisecond granularity, unguessable within a
    millisecond.
    """
    unix_ts_ms = int(time.time() * 1000)
    rand = os.urandom(10)

    time_bytes = unix_ts_ms.to_bytes(6, byteorder="big")

    rand_a = int.from_bytes(rand[0:2], byteorder="big") & 0x0FFF
    time_hi_and_version = (_VERSION_7 << 12) | rand_a

    rand_b = int.from_bytes(rand[2:10], byteorder="big") & 0x3FFF_FFFF_FFFF_FFFF
    clock_seq_and_node = (_VARIANT_RFC_9562 << 62) | rand_b

    uuid_bytes = (
        time_bytes
        + time_hi_and_version.to_bytes(2, byteorder="big")
        + clock_seq_and_node.to_bytes(8, byteorder="big")
    )
    return uuid.UUID(bytes=uuid_bytes)
