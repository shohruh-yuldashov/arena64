"""`BaseRepository` — a shared, database-only CRUD helper a module's
concrete repository implementation may compose with or extend.

**This is not the pattern repositories.md §10 calls an anti-pattern.**
That warning is about a *module's public port* — the `Protocol` declared
in `application/` that the rest of the module depends on — being generic
(`get`/`list`/`filter`/`save`), because that lets any caller construct any
query at the call site, and "no query has an owner." `BaseRepository` is a
different layer entirely: private `infrastructure/` plumbing implementing
the mechanical half of a concrete repository (the `INSERT`/`SELECT`/
`DELETE` boilerplate), never a module's exposed contract. A module's real
port stays named for its use case — `find_pairable_opponents`,
`list_recent_matches_for_player` (repositories.md §3) — and its concrete
implementation *may* use `BaseRepository` internally to avoid rewriting
"get by id" and "add" in every module. `application/` never imports this
class; only a module's `infrastructure/` does.

Returns ORM model instances, not domain entities — correctly, at this
layer: `BaseRepository` has no domain to map into. `app.core.repository`'s
`Repository` marker is the contract a real module's port satisfies,
domain-entity mapping included; a concrete repository built on top of this
one adds that mapping step, which is exactly the layer below it that this
class occupies.

Never opens, commits, or rolls back a transaction — repositories.md §4:
that is the unit of work's job (`app.core.unit_of_work`), always owned by
the calling service, never by a repository.
"""

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.base import Base


class BaseRepository[ModelT: Base, IdT]:
    """Generic CRUD mechanics for one SQLAlchemy model, over one session.

    Constructed per use case, the same as any concrete repository
    (repositories.md §5.1) — never held longer than the unit of work whose
    session it wraps.
    """

    def __init__(self, session: AsyncSession, model: type[ModelT]) -> None:
        self._session = session
        self.model = model

    async def get_by_id(self, id_: IdT) -> ModelT | None:
        """`Session.get()` handles both simple and composite primary keys
        (a composite key is passed as a tuple) — no reason to hand-write
        `select(self.model).where(self.model.id == id_)` when the ORM
        already does this, including its own identity-map check."""
        return await self._session.get(self.model, id_)

    async def add(self, entity: ModelT) -> ModelT:
        """Stages the entity and flushes — never commits. Flushing (not
        committing) is what repositories.md §5.1 permits: it is how a
        caller obtains a generated primary key within the still-open unit
        of work; only that unit of work commits.
        """
        self._session.add(entity)
        await self._session.flush()
        return entity

    async def delete(self, entity: ModelT) -> None:
        await self._session.delete(entity)
        await self._session.flush()

    def select(self) -> Select[tuple[ModelT]]:
        """The base `SELECT` a concrete repository composes `.where()`,
        `.order_by()`, and pagination (`app.repositories.pagination`) onto
        for a *named* query. Not a generic fetch-everything method meant
        to be called bare from application code — see this module's
        docstring on where the line against repositories.md §10 sits.
        """
        return select(self.model)

    async def count(self, statement: Select[tuple[ModelT]] | None = None) -> int:
        """Counts rows for `statement` (or the whole table if omitted).
        Used by `app.repositories.pagination.paginate_offset` — never call
        this per row or in a loop; it is one query, not free.
        """
        base = statement if statement is not None else self.select()
        result = await self._session.scalar(select(func.count()).select_from(base.subquery()))
        return result or 0
