"""`created_at` / `updated_at` — single responsibility: *when*."""

from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.types import UtcDateTime


class TimestampMixin:
    """`created_at` and `updated_at`, for C2 (durable mutable) relations —
    database.md §2.1's durability classification.

    **Not for C1 (permanent record) relations.** database.md DB-02/§11.1 is
    explicit that an append-only relation carries `created_at` alone and
    never `updated_at`: a mutable-looking column on an immutable relation
    is an invitation the runtime database role's missing `UPDATE` grant
    (DB-09) then has to refuse. A C1 model composes a narrower mixin, or
    declares `created_at` directly — deliberately not provided here, so
    reaching for `TimestampMixin` on a C1 table is a visible choice, not
    an accident of convenience.

    The `server_default`/`onupdate` values are a **backstop, not the
    primary mechanism** — database.md DB-19: "`created_at` and `updated_at`
    are set by the application from the injected clock... database defaults
    [exist only] as a backstop" for rows created outside the application
    (a seed migration, a manual repair). Application code sets these
    explicitly from the injected clock port (AD-07) on the normal path;
    an explicit value always wins over `server_default`/`onupdate`.
    """

    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime, nullable=True, onupdate=func.now()
    )
