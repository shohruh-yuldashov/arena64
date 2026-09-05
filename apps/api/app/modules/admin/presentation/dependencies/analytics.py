"""The three analytics services, over one request's session — A64-027.6.

Read-only, so they share the request session rather than opening their own:
nothing here writes, and three sessions for one page would hold three
connections for the duration of the slowest query.
"""

from typing import Annotated

from fastapi import Depends

from app.api.deps import ClockDep, DbSessionDep
from app.modules.analytics.application.services.engagement import EngagementService
from app.modules.analytics.application.services.funnels import FunnelService
from app.modules.analytics.application.services.matchmaking import MatchmakingService
from app.modules.analytics.infrastructure.repositories.engagement_repository import (
    SqlAlchemyEngagementReader,
)
from app.modules.analytics.infrastructure.repositories.funnel_repository import (
    SqlAlchemyFunnelReader,
)
from app.modules.analytics.infrastructure.repositories.matchmaking_repository import (
    SqlAlchemyMatchmakingReader,
)


def get_funnel_service(session: DbSessionDep, clock: ClockDep) -> FunnelService:
    return FunnelService(reader=SqlAlchemyFunnelReader(session), clock=clock)


def get_engagement_service(session: DbSessionDep, clock: ClockDep) -> EngagementService:
    return EngagementService(reader=SqlAlchemyEngagementReader(session), clock=clock)


def get_matchmaking_service(session: DbSessionDep, clock: ClockDep) -> MatchmakingService:
    return MatchmakingService(reader=SqlAlchemyMatchmakingReader(session), clock=clock)


FunnelServiceDep = Annotated[FunnelService, Depends(get_funnel_service)]
EngagementServiceDep = Annotated[EngagementService, Depends(get_engagement_service)]
MatchmakingServiceDep = Annotated[MatchmakingService, Depends(get_matchmaking_service)]
