"""The primary key — single responsibility: *identity*."""

import uuid

from sqlalchemy import Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.identifiers import generate_uuid7


class UUIDPrimaryKeyMixin:
    """A UUIDv7 primary key — database.md DB-07.

    `default=generate_uuid7` is a Python-side default: SQLAlchemy's unit of
    work calls it during `flush()`, before the `INSERT` reaches the
    database, and writes the result back onto the instance — so `widget.id`
    is populated the moment `session.flush()` returns, not only after
    `commit()`. That ordering is what AD-16 actually needs: a service
    writes an outbox row referencing the entity it just created *in the
    same transaction*, which means after this entity's own flush, not
    necessarily before one. It is **not** populated at bare `__init__()` —
    `Widget(name="x").id` is `None` until the owning session flushes;
    reaching for the id before that point is reaching for it before the
    unit of work that was going to assign it has run.

    The database's own default is a safety net only, for the rare row
    inserted by something other than this application (DB-07's own
    caveat) — since PostgreSQL 17 has no native `uuid_generate_v7()`,
    that safety net is deliberately left unset here rather than approximated
    with `gen_random_uuid()` (v4), which would produce a *different kind*
    of identifier than every row the application inserts itself.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=generate_uuid7
    )
