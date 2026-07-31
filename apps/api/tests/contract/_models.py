"""A test-only model exercising every mixin — never a real migration
target. Registers onto the real `app.database.base.Base` (proving the
mixins work with exactly what a future module will use), but only within
the pytest process: `alembic revision --autogenerate` is a separate
process invocation that never imports `tests/`, so this table can never
appear in a real generated migration.
"""

from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.mixins import (
    AuditMixin,
    CreatedByMixin,
    SoftDeleteMixin,
    TimestampMixin,
    UpdatedByMixin,
    UUIDPrimaryKeyMixin,
)


class ContractWidget(
    Base,
    UUIDPrimaryKeyMixin,
    TimestampMixin,
    SoftDeleteMixin,
    AuditMixin,
    CreatedByMixin,
    UpdatedByMixin,
):
    __tablename__ = "contract_widget"
    __mapper_args__ = {"version_id_col": AuditMixin.version}

    name: Mapped[str] = mapped_column(nullable=False)
    rank: Mapped[int] = mapped_column(nullable=False, default=0)
