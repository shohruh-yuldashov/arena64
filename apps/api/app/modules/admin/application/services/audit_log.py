"""`AuditLog` — reading the trail — A64-024.8.

Separate from `AuditRecorder` because the two have nothing in common but a
table. Recording happens inside somebody else's transaction and takes an
actor the guard resolved; reading happens on its own and takes filters a
console supplied. One class doing both would hand every reader a `record`
method it has no business holding.

**Read-only, and structurally.** There is no method here that writes, and
the port beneath offers no update or delete — so a route holding this
object cannot alter the record of what administrators have done, which is
the one table where that matters most.
"""

from app.modules.admin.application.ports import (
    AuditEntryFilters,
    AuditEntryPage,
    AuditEntryRepository,
)


class AuditLog:
    """Reads the audit trail for the console."""

    def __init__(self, *, entries: AuditEntryRepository) -> None:
        self._entries = entries

    async def page(
        self, *, filters: AuditEntryFilters, limit: int, cursor: str | None
    ) -> AuditEntryPage:
        """One page, newest first.

        Bounded by `limit` always — there is no unbounded form, because
        "export the whole trail" is a governed operation and not something
        a console route should be able to do by omitting a parameter.
        """
        return await self._entries.page(filters=filters, limit=limit, cursor=cursor)


__all__ = ["AuditLog"]
