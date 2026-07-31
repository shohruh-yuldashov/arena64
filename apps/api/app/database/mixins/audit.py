"""`version` / `created_by` / `updated_by` — three single-responsibility
mixins, kept separate because each answers a different question and a
model should be able to pick any subset (this task's explicit instruction:
"every mixin must have a single responsibility")."""

import uuid

from sqlalchemy import Integer, Uuid
from sqlalchemy.orm import Mapped, mapped_column


class AuditMixin:
    """`version` — single responsibility: detecting a concurrent write.

    repositories.md §3/§8.4: "Enforce optimistic concurrency: check and
    increment the aggregate's version." This column is the mechanism; it
    does not, by itself, turn on SQLAlchemy's automatic version check —
    that requires the concrete model to opt in explicitly:

        class MyModel(Base, AuditMixin):
            __tablename__ = "my_model"
            __mapper_args__ = {"version_id_col": version}

    Deliberately not wired automatically via a mixin-level `__mapper_args__`:
    SQLAlchemy does not merge `__mapper_args__` contributed by more than
    one source, so a mixin that set it would silently conflict with — or
    be silently overridden by — whatever a concrete model needs
    `__mapper_args__` for on its own (polymorphic identity, a different
    mapper option). One explicit line at the model is safer than magic
    that works only until a second `__mapper_args__` need appears.

    Named for the property it audits — that a row has not changed since it
    was read — not to be confused with an activity log
    (`admin.audit_entry` in database.md: a real future table with its own
    actor/action/subject shape, unrelated to this column).
    """

    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class CreatedByMixin:
    """`created_by` — single responsibility: *who created this row*.

    An opaque, player-id-shaped UUID — never a foreign key (DB-03: no
    cross-module referential integrity). database.md §11.3 is explicit
    this is not a default addition: add it "only where the acting party is
    not already identifiable from the row" — a match doesn't need it (the
    participants are the actors, recorded as such); a moderation case does
    (the moderator is not otherwise on the row).
    """

    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class UpdatedByMixin:
    """`updated_by` — single responsibility: *who last changed this row*.
    Same opacity and the same "not by default" rule as `CreatedByMixin` —
    see its docstring.
    """

    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
