"""The ORM registry root — database.md §2, §17 R-1.

No models are declared here or anywhere in this bootstrap — explicitly out
of scope (the task's "Do NOT implement: Database models"). What must exist
*before* the first model does is the naming-convention metadata:
database.md §17 R-1 is explicit that configuring this after the first
migration leaves every existing constraint with a machine-generated name
that no alert, migration, or runbook can reference. This is that
configuration, and nothing else.
"""

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# database.md §2 naming conventions, encoded once so Alembic's autogenerate
# and every future model agree without anyone restating them by hand.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s__%(column_0_N_name)s",
    "uq": "uq_%(table_name)s__%(column_0_N_name)s",
    "ck": "ck_%(table_name)s__%(constraint_name)s",
    "fk": "fk_%(table_name)s__%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Every future module's ORM models subclass this, so every table on
    the platform shares one naming convention and one Alembic target
    (alembic/env.py). database.md §3's schema-per-module ownership (DB-03)
    is expressed as a `schema=` argument on each module's own tables, not
    as a separate metadata registry per module — one registry keeps
    Alembic's view of "everything that exists" complete.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
