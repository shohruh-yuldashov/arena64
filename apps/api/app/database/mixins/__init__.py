"""Reusable declarative mixins — building blocks a future module's ORM
model composes from, never a model in its own right. Each has exactly one
responsibility (this task's explicit requirement), so a model picks only
the ones its aggregate actually needs:

    class Account(Base, UUIDPrimaryKeyMixin, TimestampMixin):
        __tablename__ = "account"
        ...

None of these appear on every table by default — database.md §11.1 is
explicit that the audit-field set is a function of an entity's durability
class (C1/C2/C3), not a blanket policy. `SoftDeleteMixin` in particular is
the narrow exception, not the default — read its own docstring before
reaching for it.
"""

from app.database.mixins.audit import AuditMixin, CreatedByMixin, UpdatedByMixin
from app.database.mixins.soft_delete import SoftDeleteMixin
from app.database.mixins.timestamp import TimestampMixin
from app.database.mixins.uuid_pk import UUIDPrimaryKeyMixin

__all__ = [
    "AuditMixin",
    "CreatedByMixin",
    "SoftDeleteMixin",
    "TimestampMixin",
    "UUIDPrimaryKeyMixin",
    "UpdatedByMixin",
]
