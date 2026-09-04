"""Defence in depth over the property schemas — analytics.md §23.

**The schemas are the defence.** Every property that reaches the store has
passed a model with `extra="forbid"`, so an unknown key is already a
validation error and a denylist adds nothing on that path.

What this adds is the path that does not exist yet. A64-027.3 will write
queries, A64-027.4 will build aggregates, and somebody will eventually add a
property to a schema in good faith — `username`, so a funnel can be read
during an incident; `error_message`, so a failure can be diagnosed. Neither
is caught by `extra="forbid"`, because both would be *declared*.

So this checks the schemas themselves, at import time in a test rather than
per request: a denylisted name appearing as a **field** of any event schema
fails the suite. That is where the decision gets made, and where somebody
has to argue for it.

## What this is not

Key-name scanning is not a security control. A property called `label` can
hold an email; nothing here would notice. The control is that every field is
typed — an enum, a bounded integer, or one of three patterned strings — and
none of those types can hold prose.
"""

from collections.abc import Iterable

from app.modules.analytics.domain.schemas import SCHEMAS, PropertySchema
from app.platform.analytics import DENIED_PROPERTY_NAMES


def denied_fields(schema: type[PropertySchema]) -> frozenset[str]:
    """Which of one schema's declared fields are on the denylist."""
    return frozenset(schema.model_fields) & DENIED_PROPERTY_NAMES


def denied_fields_across_taxonomy() -> dict[str, frozenset[str]]:
    """Every schema that declares a denied field, keyed by event name.

    Empty is the only acceptable result, and a test says so.
    """
    offenders = {
        name.value: denied for name, schema in SCHEMAS.items() if (denied := denied_fields(schema))
    }
    return offenders


def rejected_keys(keys: Iterable[str]) -> frozenset[str]:
    """The denylisted names in an arbitrary key set.

    For the ingestion path's last check before persistence. Cheap, and it
    catches a projection built by hand rather than through a schema — which
    is the one way a raw mapping can reach the store.
    """
    return frozenset(keys) & DENIED_PROPERTY_NAMES
