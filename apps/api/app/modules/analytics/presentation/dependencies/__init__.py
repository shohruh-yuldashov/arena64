"""The collector's one dependency — A64-027.2 §9.

Everything holds the request's session, so a batch is one transaction: the
subject resolution and the event inserts commit together or not at all.
"""

from typing import Annotated

from fastapi import Depends

from app.api.deps import ClockDep, DbSessionDep
from app.config.environment import current_environment
from app.modules.analytics.application.services.collector import ClientEventCollector
from app.modules.analytics.infrastructure.repositories.analytics_repository import (
    SqlAlchemyAnalyticsEventStore,
    SqlAlchemySubjectDirectory,
)


def get_client_event_collector(
    session: DbSessionDep,
    clock: ClockDep,
) -> ClientEventCollector:
    """The collector over one request's session.

    `current_environment()` rather than a request field or a setting a
    client could influence: the environment is a property of the process,
    and it is what keeps a laptop's events out of production's numbers
    (§45).
    """
    return ClientEventCollector(
        store=SqlAlchemyAnalyticsEventStore(session),
        subjects=SqlAlchemySubjectDirectory(session),
        clock=clock,
        environment=current_environment(),
    )


ClientEventCollectorDep = Annotated[ClientEventCollector, Depends(get_client_event_collector)]
