"""`deleted_at` — single responsibility: *is this row still live*."""

from datetime import datetime

from sqlalchemy.orm import Mapped, mapped_column

from app.database.types import UtcDateTime


class SoftDeleteMixin:
    """A generic "deleted" marker — the narrow exception, not the default.

    database.md DB-20 is direct about this: **"No relation in Arena64 uses
    a generic `deleted_at`."** Every case that looks like soft delete
    turns out to be a named domain state with its own meaning and its own
    reversibility — `revoked_at` on a session, `ended_at` on a friendship,
    `redacted_at` on a chat message, `lifted_at` on a sanction. Reaching
    for this mixin instead of naming the state is exactly the shortcut
    DB-20 warns produces "a filter that, forgotten in one place, produces
    two irreconcilable views" of what a table contains.

    Use this only where a table's deletion genuinely has no more specific
    meaning than "gone" — and document *why* at the call site, the way
    DB-20 requires of every soft-delete-shaped column. It is provided
    because the pattern is common enough across unrelated small platform
    tables (a future admin utility list, a cache-adjacent lookup table)
    that reimplementing it each time would be its own inconsistency; it is
    not a default reached for out of convenience on a real aggregate.

    Deliberately does **not** install a global "exclude deleted rows"
    query filter. A blanket filter would silently hide soft-deleted rows
    from a future moderation or audit view that specifically needs to see
    them — filtering is a decision each repository makes explicitly (e.g.
    `.where(Model.deleted_at.is_(None))`), not one this mixin makes for
    every caller.
    """

    deleted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True, default=None)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None
