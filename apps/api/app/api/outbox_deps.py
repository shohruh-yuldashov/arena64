"""The platform's `EventPublisher` dependency — A64-013.7.

In `app/api/` rather than in a module's `presentation/dependencies/`, for the
same reason `get_db_session` and `get_rate_limiter` are: the outbox belongs
to no bounded context (see `app/platform/__init__.py`), and putting its
factory inside `friends` would make every future producer resolve a
dependency owned by a module it has nothing to do with.

Kept in its own file rather than added to `app/api/deps.py` because that
module is the *infrastructure* deps — session, settings, Redis pools, the
rate limiter — and every one of them is a request-scoped resource. This is a
service built over one of them, which is a different kind of thing, and
`deps.py`'s docstring is explicit about what it holds.
"""

import logging
from typing import Annotated

from fastapi import Depends

from app.api.deps import DbSessionDep, SettingsDep
from app.platform.outbox import (
    EventPublisher,
    NoEventPublisher,
    OutboxEventPublisher,
    SqlAlchemyOutboxRepository,
)

logger = logging.getLogger(__name__)


def get_event_publisher(session: DbSessionDep, settings: SettingsDep) -> EventPublisher:
    """The publisher every producing service is handed.

    Built over the **request's** session, which is the entire point: a
    service that publishes inside its own unit of work is publishing into
    the same transaction as its state change, and that only works if the
    publisher's repository and the service's repositories share a session
    (AD-16).

    `NoEventPublisher` is the fallback, wired by `OUTBOX_ENABLED=false`. It
    is the one fallback on this platform that loses information rather than
    degrading performance — a state change still commits and its
    consequences stop being recorded — so unlike the presence and cache kill
    switches it logs at `WARNING` per event rather than once at selection.
    Nothing here logs the selection itself for that reason: the discard is
    louder and more useful than the choice.
    """
    if not settings.outbox.enabled:
        return NoEventPublisher()

    return OutboxEventPublisher(SqlAlchemyOutboxRepository(session))


EventPublisherDep = Annotated[EventPublisher, Depends(get_event_publisher)]
